"""app/services/trailing_tp_engine.py 边界条件测试（纯逻辑，无 IO / 无 DB / 无网络）。

覆盖边界：
  1.  profit_pct：做空/做多盈利计算与 entry_price=0 兜底
  2.  5 分钟激活窗口临界：now_ms = window_sec*1000 - 1 不超时；+1 超时切 expired
  3.  激活阈值临界：盈利恰等于 activate_threshold 激活；少 0.01% 不激活
  4.  激活优先于超时：armed 且已超时但盈利达阈值 → apply_tick 先检查激活
  5.  阶梯1 临界：peak_pct 恰 2.5 → 20；2.49 → 30
  6.  阶梯2 临界：peak_pct 恰 5.0 → 15；4.99 → 20
  7.  回撤触发平仓：peak 4%、当前 3%（回撤 25%）≥ 20 → should_close / apply_tick 返回 close
  8.  回撤未触发：回撤 12.5% < 20 → 不平仓
  9.  peak_pct=0 不触发平仓
  10. update_peak：价格创新低更新 peak，反弹不更新
  11. 开关关闭：enabled=False 返回 None 且 state 不变
  12. expired 状态：apply_tick 返回 None 不再处理
  13. current_drawdown_pct：(peak-cur)/peak*100
  14. remaining_window_sec：剩余秒数计算
  15. 模式切换链路：armed → activated → peak 更新 → 回撤 close
"""
import pytest

from app.services.trailing_tp_engine import (
    STATE_ARMED,
    STATE_ACTIVE,
    STATE_EXPIRED,
    TrailingTpParams,
    TrailingTpMemState,
    profit_pct,
    update_peak,
    is_window_expired,
    should_activate,
    current_drawdown_limit,
    current_drawdown_pct,
    should_close,
    remaining_window_sec,
    apply_tick,
)


# ---------- 辅助函数 ----------

def make_params(
    enabled: bool = True,
    window_sec: float = 300.0,
    activate_threshold_pct: float = 2.0,
    drawdown_base_pct: float = 30.0,
    drawdown_tier1_pct: float = 20.0,
    drawdown_tier2_pct: float = 15.0,
    tier1_threshold: float = 2.5,
    tier2_threshold: float = 5.0,
) -> TrailingTpParams:
    """默认参数：窗口 300s、激活阈值 2%、阶梯 30/20/15 @ 2.5/5.0。"""
    return TrailingTpParams(
        enabled=enabled,
        window_sec=window_sec,
        activate_threshold_pct=activate_threshold_pct,
        drawdown_base_pct=drawdown_base_pct,
        drawdown_tier1_pct=drawdown_tier1_pct,
        drawdown_tier2_pct=drawdown_tier2_pct,
        tier1_threshold=tier1_threshold,
        tier2_threshold=tier2_threshold,
    )


def make_mem(
    state: str = STATE_ARMED,
    peak_pct: float = 0.0,
    peak_price: float = 0.0,
    side: str = "short",
    entry_price: float = 100.0,
    opened_at_ms: int = 0,
    position_id: int = 1,
    symbol: str = "BTCUSDT",
) -> TrailingTpMemState:
    """默认 mem：做空 entry=100、opened_at_ms=0。"""
    return TrailingTpMemState(
        position_id=position_id,
        symbol=symbol,
        side=side,
        entry_price=entry_price,
        opened_at_ms=opened_at_ms,
        state=state,
        peak_pct=peak_pct,
        peak_price=peak_price,
    )


# ---------- 1. profit_pct ----------

def test_profit_pct_short_profit():
    assert profit_pct("short", 100.0, 98.0) == pytest.approx(2.0)


def test_profit_pct_long_profit():
    assert profit_pct("long", 100.0, 103.0) == pytest.approx(3.0)


def test_profit_pct_entry_zero_returns_zero():
    assert profit_pct("short", 0.0, 98.0) == 0.0
    assert profit_pct("long", 0.0, 103.0) == 0.0


# ---------- 2. 5 分钟窗口临界 ----------

def test_window_boundary_not_expired_one_ms_before():
    """now_ms = window_sec*1000 - 1（差 1ms）不超时。"""
    mem = make_mem(state=STATE_ARMED, opened_at_ms=0)
    params = make_params(window_sec=300.0)
    assert is_window_expired(mem, now_ms=299999, window_sec=300.0) is False


def test_window_boundary_expired_one_ms_after():
    """now_ms = window_sec*1000 + 1 超时 → apply_tick 返回 'window_expired' 且 state='expired'。"""
    mem = make_mem(state=STATE_ARMED, opened_at_ms=0)
    params = make_params(window_sec=300.0, activate_threshold_pct=2.0)
    # current=99 → 做空盈利 1%，未达阈值，避免激活优先
    result = apply_tick(mem, params, current_price=99.0, now_ms=300001)
    assert result == "window_expired"
    assert mem.state == STATE_EXPIRED


# ---------- 3. 激活阈值临界 ----------

def test_activate_threshold_boundary_exact():
    """armed、阈值 2.0、做空 entry=100 current=98.0（盈利恰 2.0%）→ 激活。"""
    mem = make_mem(state=STATE_ARMED, opened_at_ms=0)
    params = make_params(window_sec=300.0, activate_threshold_pct=2.0)
    result = apply_tick(mem, params, current_price=98.0, now_ms=1000)
    assert result == "activated"
    assert mem.state == STATE_ACTIVE


def test_activate_threshold_boundary_just_below():
    """current=98.01（盈利 1.99%）< 2.0 → 不激活，返回 None。"""
    mem = make_mem(state=STATE_ARMED, opened_at_ms=0)
    params = make_params(window_sec=300.0, activate_threshold_pct=2.0)
    result = apply_tick(mem, params, current_price=98.01, now_ms=1000)
    assert result is None
    assert mem.state == STATE_ARMED


# ---------- 4. 激活优先于超时 ----------

def test_activation_timeout_abandon_even_if_profit_hits():
    """armed、已超时（now_ms 超过窗口）但盈利恰好达阈值 → 按规格放弃，返回 'window_expired'。

    用户规格明确："开仓后超过5分钟仍未达到预设止盈阈值，则自动放弃移动止盈功能"。
    超时后窗口关闭，即使此刻盈利达阈值也不应激活（窗口已过）。
    should_activate 内置窗口守卫：超时返回 False → apply_tick 走 is_window_expired。
    """
    mem = make_mem(state=STATE_ARMED, opened_at_ms=0)
    params = make_params(window_sec=300.0, activate_threshold_pct=2.0)
    # now_ms=300001 已超时 1ms；current=98.0 做空盈利 2.0% 恰达阈值
    result = apply_tick(mem, params, current_price=98.0, now_ms=300001)
    assert result == "window_expired"
    assert mem.state == STATE_EXPIRED


# ---------- 5. 阶梯1 临界 ----------

def test_tier1_boundary_exact_at_2_5():
    """peak_pct 恰 2.5 → drawdown_limit=20。"""
    params = make_params()
    assert current_drawdown_limit(params, peak_pct=2.5) == pytest.approx(20.0)


def test_tier1_boundary_just_below_2_5():
    """peak_pct=2.49 → drawdown_limit=30。"""
    params = make_params()
    assert current_drawdown_limit(params, peak_pct=2.49) == pytest.approx(30.0)


# ---------- 6. 阶梯2 临界 ----------

def test_tier2_boundary_exact_at_5_0():
    """peak_pct 恰 5.0 → drawdown_limit=15。"""
    params = make_params()
    assert current_drawdown_limit(params, peak_pct=5.0) == pytest.approx(15.0)


def test_tier2_boundary_just_below_5_0():
    """peak_pct=4.99 → drawdown_limit=20。"""
    params = make_params()
    assert current_drawdown_limit(params, peak_pct=4.99) == pytest.approx(20.0)


# ---------- 7. 回撤触发平仓 ----------

def test_drawdown_triggers_close():
    """active 做空 entry=100，peak=4%（>tier1），peak_price=96；当前 97（盈利 3%，回撤 25%）≥ 20 → 平仓。"""
    params = make_params()
    mem = make_mem(state=STATE_ACTIVE, side="short", entry_price=100.0, peak_pct=4.0, peak_price=96.0)
    # should_close
    assert should_close(mem, params, current_price=97.0) is True
    # current_drawdown_pct = (4-3)/4*100 = 25
    assert current_drawdown_pct(mem, current_price=97.0) == pytest.approx(25.0)
    # apply_tick 返回 'close'
    result = apply_tick(mem, params, current_price=97.0, now_ms=1000)
    assert result == "close"


# ---------- 8. 回撤未触发 ----------

def test_drawdown_not_triggered():
    """active 做空 peak=4%，当前盈利 3.5%（回撤 12.5%）< 20 → 不平仓。"""
    params = make_params()
    mem = make_mem(state=STATE_ACTIVE, side="short", entry_price=100.0, peak_pct=4.0, peak_price=96.0)
    # 盈利 3.5% → current=96.5
    assert profit_pct("short", 100.0, 96.5) == pytest.approx(3.5)
    assert should_close(mem, params, current_price=96.5) is False
    result = apply_tick(mem, params, current_price=96.5, now_ms=1000)
    assert result is None


# ---------- 9. peak_pct=0 不触发平仓 ----------

def test_peak_zero_no_close():
    """active 但从未盈利（peak_pct=0）→ should_close False。"""
    params = make_params()
    mem = make_mem(state=STATE_ACTIVE, peak_pct=0.0, peak_price=0.0)
    assert should_close(mem, params, current_price=99.0) is False


# ---------- 10. update_peak ----------

def test_update_peak_tracks_new_low_then_holds_on_bounce():
    """做空 entry=100，peak_pct 初始 0；价跌到 96（盈利 4%）→ peak_pct=4、peak_price=96；
    反弹到 98 → peak_pct 仍=4、peak_price 仍=96。"""
    mem = make_mem(state=STATE_ACTIVE, side="short", entry_price=100.0, peak_pct=0.0, peak_price=0.0)
    # 价格跌到 96
    assert update_peak(mem, current_price=96.0) == pytest.approx(4.0)
    assert mem.peak_pct == pytest.approx(4.0)
    assert mem.peak_price == pytest.approx(96.0)
    # 反弹到 98（盈利 2% < 4%）
    assert update_peak(mem, current_price=98.0) == pytest.approx(4.0)
    assert mem.peak_pct == pytest.approx(4.0)
    assert mem.peak_price == pytest.approx(96.0)


# ---------- 11. 开关关闭 ----------

def test_disabled_returns_none_and_state_unchanged():
    """params.enabled=False → apply_tick 返回 None，state 不变。"""
    mem = make_mem(state=STATE_ARMED, opened_at_ms=0)
    params = make_params(enabled=False, window_sec=300.0, activate_threshold_pct=2.0)
    # 即便盈利达阈值、即便已超时，开关关闭都不处理
    result = apply_tick(mem, params, current_price=98.0, now_ms=300001)
    assert result is None
    assert mem.state == STATE_ARMED


# ---------- 12. expired 状态 ----------

def test_expired_state_returns_none():
    """expired 状态：apply_tick 返回 None（不再处理）。"""
    mem = make_mem(state=STATE_EXPIRED, peak_pct=4.0, peak_price=96.0)
    params = make_params()
    result = apply_tick(mem, params, current_price=97.0, now_ms=1000)
    assert result is None
    assert mem.state == STATE_EXPIRED


# ---------- 13. current_drawdown_pct ----------

def test_current_drawdown_pct_calculation():
    """peak_pct=4，cur=3 → (4-3)/4*100 = 25。"""
    mem = make_mem(state=STATE_ACTIVE, side="short", entry_price=100.0, peak_pct=4.0, peak_price=96.0)
    # cur profit=3 → current=97
    assert profit_pct("short", 100.0, 97.0) == pytest.approx(3.0)
    assert current_drawdown_pct(mem, current_price=97.0) == pytest.approx(25.0)


# ---------- 14. remaining_window_sec ----------

def test_remaining_window_sec_calculation():
    """armed、opened=0、now=100000、window=300 → 200.0。"""
    mem = make_mem(state=STATE_ARMED, opened_at_ms=0)
    assert remaining_window_sec(mem, now_ms=100000, window_sec=300.0) == pytest.approx(200.0)


# ---------- 15. 模式切换链路稳定性 ----------

def test_full_state_transition_chain():
    """armed → activated(切 active) → 后续 tick 更新 peak → 回撤触发 close。"""
    params = make_params(window_sec=300.0, activate_threshold_pct=2.0)
    mem = make_mem(state=STATE_ARMED, side="short", entry_price=100.0,
                   peak_pct=0.0, peak_price=0.0, opened_at_ms=0)

    # Step1: current=98（盈利 2% 达阈值）→ armed → activated
    r1 = apply_tick(mem, params, current_price=98.0, now_ms=1000)
    assert r1 == "activated"
    assert mem.state == STATE_ACTIVE
    assert mem.peak_pct == pytest.approx(2.0)
    assert mem.peak_price == pytest.approx(98.0)

    # Step2: 价格跌到 96（盈利 4%）→ 更新 peak=4、peak_price=96，未回撤不平仓
    r2 = apply_tick(mem, params, current_price=96.0, now_ms=2000)
    assert r2 is None
    assert mem.peak_pct == pytest.approx(4.0)
    assert mem.peak_price == pytest.approx(96.0)

    # Step3: 价格反弹到 97（盈利 3%，回撤 25% ≥ 20）→ close
    r3 = apply_tick(mem, params, current_price=97.0, now_ms=3000)
    assert r3 == "close"


def test_armed_updates_peak_during_window():
    """armed 期间必须持续更新 peak，记录窗口内历史峰值。

    场景：armed 内价格曾到 5% 盈利再回 2% 激活 → peak 应=5（历史峰值），
    而非 2（激活那一刻的当前值）。否则激活后回撤计算基线错误。
    这是修复 armed 分支不调 update_peak 的回归测试。
    """
    mem = make_mem(state=STATE_ARMED, opened_at_ms=0)
    params = make_params(window_sec=300.0, activate_threshold_pct=2.0)
    # tick1: price=95（盈利5%），armed 未达激活？不，5%>2% 会激活。
    # 用更高阈值避免激活，验证 armed 期间 peak 更新
    params = make_params(window_sec=300.0, activate_threshold_pct=10.0)
    # tick1: price=95（盈利5%），armed，peak 应更新到 5
    r1 = apply_tick(mem, params, current_price=95.0, now_ms=1000)
    assert r1 is None
    assert mem.state == STATE_ARMED
    assert mem.peak_pct == pytest.approx(5.0)
    assert mem.peak_price == pytest.approx(95.0)
    # tick2: price=99（盈利1%），armed，peak 不降仍=5
    r2 = apply_tick(mem, params, current_price=99.0, now_ms=2000)
    assert r2 is None
    assert mem.peak_pct == pytest.approx(5.0)
    assert mem.peak_price == pytest.approx(95.0)


def test_armed_peak_carried_into_activation():
    """armed 期间记录的峰值盈利，激活后作为回撤计算基线。

    场景：armed 内价格到 95（盈利5%，peak=5），再涨回 98（盈利2%）触发激活。
    激活后 peak 应=5（历史峰值）。回撤应基于 5% 算，而非激活时的 2%。
    """
    mem = make_mem(state=STATE_ARMED, opened_at_ms=0)
    params = make_params(window_sec=300.0, activate_threshold_pct=2.0)
    # tick1: price=95（盈利5%），armed，peak=5（未激活，因为... 5%>2% 会激活）
    # 改用：先到 3% 再回 2% 激活，验证 peak=3
    # tick1: price=97（盈利3%），armed，peak=3（3%>2% 会激活！）
    # 必须让 armed 期间盈利 < 激活阈值，才能留在 armed 更新 peak
    params = make_params(window_sec=300.0, activate_threshold_pct=5.0)
    # tick1: price=97（盈利3%），armed（3%<5% 未激活），peak=3
    r1 = apply_tick(mem, params, current_price=97.0, now_ms=1000)
    assert r1 is None
    assert mem.peak_pct == pytest.approx(3.0)
    # tick2: price=95（盈利5%），达阈值→激活，peak 应=5（当前也是5）
    r2 = apply_tick(mem, params, current_price=95.0, now_ms=2000)
    assert r2 == "activated"
    assert mem.state == STATE_ACTIVE
    assert mem.peak_pct == pytest.approx(5.0)
    # tick3: price=96（盈利4%，从峰值5%回撤20%）→ 应触发 close
    # 回撤 = (5-4)/5*100 = 20%，peak=5≥tier1(2.5)→limit=20，20>=20→close
    r3 = apply_tick(mem, params, current_price=96.0, now_ms=3000)
    assert r3 == "close"


def test_active_persists_beyond_window():
    """语义4验证：已激活移动止盈后超过5分钟，仍按移动止盈追踪，不切回限价。

    场景：窗口内激活→active；now 远超窗口（10分钟）；
    - 价格未回撤→返回 None（不返回 window_expired，不回退限价）
    - 价格回撤达限制→返回 'close'（仍由移动止盈平仓）
    """
    mem = make_mem(state=STATE_ACTIVE, opened_at_ms=0, peak_pct=4.0, peak_price=96.0)
    params = make_params(window_sec=300.0, activate_threshold_pct=2.0)
    # now_ms=600000（10分钟，远超5分钟窗口），价格未回撤（盈利维持4%）
    # peak=4≥tier1(2.5)→limit=20；当前盈利4%→回撤0%<20→不平仓
    r1 = apply_tick(mem, params, current_price=96.0, now_ms=600000)
    assert r1 is None
    assert mem.state == STATE_ACTIVE  # 仍 active，未切 expired
    # 价格回撤：peak=4，当前盈利3%（price=97），回撤25%≥20→close
    r2 = apply_tick(mem, params, current_price=97.0, now_ms=600001)
    assert r2 == "close"
    assert mem.state == STATE_ACTIVE  # close 事件不改 state（由调用方平仓）


def test_non_wick_spike_strategy_not_taken_over():
    """健壮性：非 wick_spike 策略即使 Position 有 armed，trailing_taken_over=False。

    防御非 wick_spike 策略误开 trailing_tp_enabled 导致裸奔。
    本测试验证引擎层 params.enabled=False 时完全静默；
    signal_source 守卫在 position_manager._manage_positions 层（已加）。
    """
    mem = make_mem(state=STATE_ARMED, opened_at_ms=0)
    params = make_params(enabled=False)
    # 开关关闭：apply_tick 完全不处理，state 不变
    r = apply_tick(mem, params, current_price=95.0, now_ms=1000)
    assert r is None
    assert mem.state == STATE_ARMED
    assert mem.peak_pct == 0.0  # 未更新

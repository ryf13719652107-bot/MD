"""毫秒接针状态机单元测试。"""

from app.services.strategy_engine import Signal
from app.services.wick_spike_engine import (
    WickBarSnapshot,
    WickSpikeParams,
    WickSymbolState,
    confirm_max_retrace_pct,
    effective_volume_mult,
    enrich_snap_with_trades,
    mark_bar_triggered,
    near_miss_diag,
    on_tick,
    pierce_vol_view,
    release_bar_trigger,
    spike_move_pct,
    spike_progress,
    take_diag_event,
    tip_gap_pct,
    wick_retrace_pct,
)


def _snap(
    *,
    open_=100.0,
    atr=1.0,
    vol_now=80.0,
    vol_sma=10.0,
    high=101.0,
    low=99.0,
    ts=1_000_000,
) -> WickBarSnapshot:
    return WickBarSnapshot(
        bar_open_ts=ts,
        bar_open=open_,
        atr=atr,
        vol_now=vol_now,
        vol_sma=vol_sma,
        kline_high=high,
        kline_low=low,
    )


def test_no_volume_no_trigger_even_if_pierced():
    state = WickSymbolState()
    params = WickSpikeParams(direction="long", volume_mult=8.0, atr_mult=5.0)
    # vol only 2x SMA — not hot; price already deep
    snap = _snap(vol_now=20.0, vol_sma=10.0, low=90.0)
    assert on_tick(state, params, snap, last_price=90.0, now_ms=1) is None


def test_volume_then_extreme_retroactive_long():
    """价先刺穿、量后到 — 放量瞬间用 bar_low 追认。"""
    state = WickSymbolState()
    params = WickSpikeParams(
        direction="long", volume_mult=8.0, atr_mult=5.0, max_retrace_pct=0
    )
    # First: no volume, track extreme (open-5*ATR = 95)
    snap_cold = _snap(vol_now=10.0, vol_sma=10.0, low=94.0)
    assert on_tick(state, params, snap_cold, last_price=94.0, now_ms=1) is None
    assert state.bar_low == 94.0

    # Volume becomes 8x; extreme already pierced
    snap_hot = _snap(vol_now=80.0, vol_sma=10.0, low=94.0)
    sig = on_tick(state, params, snap_hot, last_price=96.0, now_ms=2)
    assert sig == Signal.LONG


def test_volume_then_later_touch_long():
    state = WickSymbolState()
    params = WickSpikeParams(direction="long", volume_mult=8.0, atr_mult=5.0, max_retrace_pct=0)
    snap = _snap(vol_now=80.0, vol_sma=10.0, low=99.0, high=101.0)
    assert on_tick(state, params, snap, last_price=99.0, now_ms=1) is None
    sig = on_tick(state, params, snap, last_price=94.5, now_ms=2)
    assert sig == Signal.LONG


def test_same_bar_only_once():
    state = WickSymbolState()
    params = WickSpikeParams(direction="long", volume_mult=8.0, atr_mult=5.0)
    snap = _snap(vol_now=80.0, vol_sma=10.0, low=90.0)
    assert on_tick(state, params, snap, last_price=90.0, now_ms=1) == Signal.LONG
    assert on_tick(state, params, snap, last_price=89.0, now_ms=2) is None


def test_other_bar_can_trigger_again_with_cooldown_zero():
    state = WickSymbolState()
    params = WickSpikeParams(direction="long", volume_mult=8.0, atr_mult=5.0, cooldown_sec=0)
    snap1 = _snap(ts=1, vol_now=80.0, vol_sma=10.0, low=90.0)
    assert on_tick(state, params, snap1, last_price=90.0, now_ms=1) == Signal.LONG
    snap2 = _snap(ts=2, vol_now=80.0, vol_sma=10.0, low=90.0)
    assert on_tick(state, params, snap2, last_price=90.0, now_ms=100) == Signal.LONG


def test_short_symmetric():
    state = WickSymbolState()
    params = WickSpikeParams(direction="short", volume_mult=8.0, atr_mult=5.0)
    # open+5*ATR = 105
    snap = _snap(vol_now=80.0, vol_sma=10.0, high=106.0, low=100.0)
    assert on_tick(state, params, snap, last_price=106.0, now_ms=1) == Signal.SHORT


def test_cooldown_blocks_next_bar():
    state = WickSymbolState()
    params = WickSpikeParams(direction="long", volume_mult=8.0, atr_mult=5.0, cooldown_sec=60)
    snap1 = _snap(ts=1, vol_now=80.0, vol_sma=10.0, low=90.0)
    assert on_tick(state, params, snap1, last_price=90.0, now_ms=1_000) == Signal.LONG
    mark_bar_triggered(state, params, snap1.bar_open_ts, now_ms=1_000)
    snap2 = _snap(ts=2, vol_now=80.0, vol_sma=10.0, low=90.0)
    assert on_tick(state, params, snap2, last_price=90.0, now_ms=30_000) is None
    assert on_tick(state, params, snap2, last_price=90.0, now_ms=70_000) == Signal.LONG


def test_release_allows_retry_same_bar():
    state = WickSymbolState()
    params = WickSpikeParams(direction="long", volume_mult=8.0, atr_mult=5.0)
    snap = _snap(vol_now=80.0, vol_sma=10.0, low=90.0)
    assert on_tick(state, params, snap, last_price=90.0, now_ms=1) == Signal.LONG
    assert on_tick(state, params, snap, last_price=90.0, now_ms=2) is None
    release_bar_trigger(state)
    assert on_tick(state, params, snap, last_price=90.0, now_ms=3) == Signal.LONG


def test_new_bar_seeds_extremes_from_kline():
    """换 K 时用 K 线高低初始化，避免只靠 last_price 丢掉本根已走出的针。"""
    state = WickSymbolState()
    params = WickSpikeParams(
        direction="short", volume_mult=8.0, atr_mult=5.0, max_retrace_pct=0
    )
    snap = _snap(ts=1, vol_now=80.0, vol_sma=10.0, open_=100.0, high=106.0, low=99.0)
    assert on_tick(state, params, snap, last_price=101.0, now_ms=1) == Signal.SHORT
    assert state.bar_high == 106.0


def test_near_miss_diag_volume_half():
    state = WickSymbolState()
    params = WickSpikeParams(direction="short", volume_mult=8.0, atr_mult=5.0)
    # progress=0.6 >= 0.5 → 可诊断；vol 4x = half of need 8x
    snap = _snap(vol_now=40.0, vol_sma=10.0, open_=100.0, high=103.0, low=99.0)
    on_tick(state, params, snap, last_price=103.0, now_ms=1)
    diag = near_miss_diag(params, snap, state, 103.0)
    assert diag is not None
    assert "vol_hot=False" in diag


def test_near_miss_diag_silent_when_far():
    state = WickSymbolState()
    params = WickSpikeParams(direction="short", volume_mult=8.0, atr_mult=5.0)
    # progress≈0.02，未刺破且 <0.5；即使量到一半也不刷屏
    snap = _snap(vol_now=40.0, vol_sma=10.0, open_=100.0, high=100.1, low=99.5)
    on_tick(state, params, snap, last_price=100.05, now_ms=1)
    assert near_miss_diag(params, snap, state, 100.05) is None


def test_progress_below_start_keeps_strict_volume():
    """progress < 1 不放宽：vol 5× 不够。"""
    params = WickSpikeParams(direction="short", volume_mult=8.0, atr_mult=5.0)
    # high=102 → progress = 2/5 = 0.4
    assert abs(spike_progress("short", 100.0, 102.0, 5.0) - 0.4) < 1e-9
    assert effective_volume_mult(params, 0.4) == 8.0

    state = WickSymbolState()
    snap = _snap(open_=100.0, atr=1.0, vol_now=50.0, vol_sma=10.0, high=102.0, low=100.0)
    assert on_tick(state, params, snap, last_price=102.0, now_ms=1) is None


def test_progress_at_full_relaxes_volume_to_5x():
    """progress ≥ 1.5 时量能收到 5×。"""
    params = WickSpikeParams(direction="short", volume_mult=8.0, atr_mult=5.0)
    # high=107.5 → progress = 7.5/5 = 1.5
    assert abs(spike_progress("short", 100.0, 107.5, 5.0) - 1.5) < 1e-9
    assert effective_volume_mult(params, 1.5) == 5.0
    assert abs(effective_volume_mult(params, 1.25) - 6.5) < 1e-9  # 线性中点

    state = WickSymbolState()
    snap = _snap(open_=100.0, atr=1.0, vol_now=50.0, vol_sma=10.0, high=107.5, low=100.0)
    assert on_tick(state, params, snap, last_price=107.5, now_ms=1) == Signal.SHORT


def test_progress_relax_disabled_keeps_strict_volume():
    """关闭放宽：深针也不降量能门槛。"""
    state = WickSymbolState()
    params = WickSpikeParams(
        direction="short",
        volume_mult=8.0,
        atr_mult=5.0,
        vol_relax_enabled=False,
    )
    snap = _snap(open_=100.0, atr=1.0, vol_now=50.0, vol_sma=10.0, high=107.5, low=100.0)
    assert on_tick(state, params, snap, last_price=107.5, now_ms=1) is None
    snap_hot = _snap(open_=100.0, atr=1.0, vol_now=80.0, vol_sma=10.0, high=107.5, low=100.0)
    assert on_tick(state, params, snap_hot, last_price=107.5, now_ms=2) == Signal.SHORT


def test_enrich_snap_prefers_trade_volume_and_high():
    snap = _snap(vol_now=10.0, vol_sma=10.0, high=101.0, low=99.0)
    enriched = enrich_snap_with_trades(
        snap, trade_vol=80.0, trade_high=106.0, trade_low=98.5, trade_bar_open_ts=snap.bar_open_ts
    )
    assert enriched.vol_now == 80.0
    assert enriched.kline_high == 106.0
    assert enriched.kline_low == 98.5
    # 成交流量够热 + 刺破 → 立刻空头
    state = WickSymbolState()
    params = WickSpikeParams(
        direction="short", volume_mult=8.0, atr_mult=5.0, max_retrace_pct=0
    )
    assert on_tick(state, params, enriched, last_price=102.0, now_ms=1) == Signal.SHORT


def test_enrich_ignores_stale_trade_bar():
    """换根后成交聚合仍属上一根 → 不得污染新根量/高低。"""
    snap = _snap(ts=2_000_000, vol_now=1.0, vol_sma=10.0, high=100.5, low=99.5)
    enriched = enrich_snap_with_trades(
        snap,
        trade_vol=999.0,
        trade_high=120.0,
        trade_low=80.0,
        trade_bar_open_ts=1_000_000,  # 上一根
    )
    assert enriched is snap
    assert enriched.vol_now == 1.0
    assert enriched.kline_high == 100.5


def test_min_move_pct_blocks_shallow_atr_pierce():
    """ATR 已刺破但相对开盘涨幅不足最小门槛 → 不开仓。"""
    state = WickSymbolState()
    # atr=0.2 → N=1；开盘 100 刺破线 101；涨 1.5% < 门槛 2.4%
    params = WickSpikeParams(direction="short", volume_mult=8.0, atr_mult=5.0, min_move_pct=2.4)
    snap = _snap(open_=100.0, atr=0.2, vol_now=80.0, vol_sma=10.0, high=101.5, low=100.0)
    assert spike_move_pct("short", 100.0, 101.5) == 1.5
    assert on_tick(state, params, snap, last_price=101.5, now_ms=1) is None
    # 涨到 2.5% 后放行
    snap2 = _snap(open_=100.0, atr=0.2, vol_now=80.0, vol_sma=10.0, high=102.5, low=100.0)
    assert on_tick(state, params, snap2, last_price=102.5, now_ms=2) == Signal.SHORT


def test_min_move_pct_zero_disables():
    state = WickSymbolState()
    params = WickSpikeParams(direction="short", volume_mult=8.0, atr_mult=5.0, min_move_pct=0)
    snap = _snap(open_=100.0, atr=0.2, vol_now=80.0, vol_sma=10.0, high=101.5, low=100.0)
    assert on_tick(state, params, snap, last_price=101.5, now_ms=1) == Signal.SHORT


def test_max_retrace_blocks_half_recovery():
    """极值已刺破且量够，但从极值向开盘已回撤超过 50% → 不开。"""
    state = WickSymbolState()
    params = WickSpikeParams(
        direction="long",
        volume_mult=8.0,
        atr_mult=5.0,
        min_move_pct=0,
        max_retrace_pct=50.0,
    )
    # open=100, low=90 → 针长 10；现价 96 → 回撤 60% > 50%
    snap = _snap(vol_now=80.0, vol_sma=10.0, low=90.0, high=100.0)
    assert wick_retrace_pct("long", 100.0, 90.0, 96.0) == 60.0
    assert on_tick(state, params, snap, last_price=96.0, now_ms=1) is None
    # 现价 94 → 回撤 40% ≤ 50% → 放行
    release_bar_trigger(state)
    assert on_tick(state, params, snap, last_price=94.0, now_ms=2) == Signal.LONG


def test_max_retrace_zero_disables():
    state = WickSymbolState()
    params = WickSpikeParams(
        direction="long",
        volume_mult=8.0,
        atr_mult=5.0,
        min_move_pct=0,
        max_retrace_pct=0,
    )
    snap = _snap(vol_now=80.0, vol_sma=10.0, low=90.0, high=100.0)
    assert on_tick(state, params, snap, last_price=96.0, now_ms=1) == Signal.LONG


def test_pierce_vol_view_pierced_but_cold():
    """已刺破但量不够 → pierced=True, vol_hot=False（供短窗重试武装）。"""
    state = WickSymbolState()
    params = WickSpikeParams(direction="long", volume_mult=8.0, atr_mult=5.0)
    snap = _snap(vol_now=20.0, vol_sma=10.0, low=90.0)
    assert on_tick(state, params, snap, last_price=90.0, now_ms=1) is None
    view = pierce_vol_view(params, snap, state, 90.0)
    assert view is not None
    assert view.pierced is True
    assert view.vol_hot is False
    assert view.vol_ratio == 2.0


def test_pierce_vol_view_hot_when_ready():
    state = WickSymbolState()
    params = WickSpikeParams(direction="short", volume_mult=8.0, atr_mult=5.0)
    snap = _snap(vol_now=80.0, vol_sma=10.0, high=106.0, low=100.0)
    on_tick(state, params, snap, last_price=106.0, now_ms=1)
    # on_tick 已触发会锁本根；view 仍应反映刺破+量热
    view = pierce_vol_view(params, snap, state, 106.0)
    assert view is not None
    assert view.pierced is True
    assert view.vol_hot is True


def test_pierce_vol_view_not_pierced():
    state = WickSymbolState()
    params = WickSpikeParams(direction="short", volume_mult=8.0, atr_mult=5.0)
    snap = _snap(vol_now=80.0, vol_sma=10.0, high=101.0, low=100.0)
    on_tick(state, params, snap, last_price=101.0, now_ms=1)
    view = pierce_vol_view(params, snap, state, 101.0)
    assert view is not None
    assert view.pierced is False


def test_arm_then_volume_within_grace_ignores_retrace():
    """刺破时装量不够 → 武装；grace 内量到了即使回撤>50% 也开（须 tip_gap 合格）。"""
    state = WickSymbolState()
    params = WickSpikeParams(
        direction="long",
        volume_mult=8.0,
        atr_mult=5.0,
        min_move_pct=0,
        max_retrace_pct=50.0,
        arm_wait_sec=12.0,
        arm_retrace_grace_sec=3.0,
        arm_grace_max_tip_gap_pct=2.0,
    )
    # open=100, low=90 刺破；量不够
    snap_cold = _snap(vol_now=20.0, vol_sma=10.0, low=90.0, high=100.0)
    assert on_tick(state, params, snap_cold, last_price=90.0, now_ms=1_000) is None
    assert state.armed_bar_ts == snap_cold.bar_open_ts
    assert state.armed_awaiting_vol is True

    # 1s 后量够：现价 91.5 → 回撤 15% 本可过，但即使放到更高回撤，tip_gap=1.5%≤2
    # 用 91.8：回撤 18%，tip_gap=1.8%≤2，且若按 50% 回撤也过；改用人为更高回撤场景：
    # tip_gap 1.5%、回撤=(91.5-90)/10=15% — 为测 grace，把 max_retrace 调到 10%
    params.max_retrace_pct = 10.0
    snap_hot = _snap(vol_now=80.0, vol_sma=10.0, low=90.0, high=100.0)
    assert wick_retrace_pct("long", 100.0, 90.0, 91.5) == 15.0
    assert tip_gap_pct(100.0, 90.0, 91.5) == 1.5
    assert on_tick(state, params, snap_hot, last_price=91.5, now_ms=2_000) == Signal.LONG


def test_arm_grace_blocked_by_tip_gap():
    """grace 内量够但 tip_gap 超上限 → 不开。"""
    state = WickSymbolState()
    params = WickSpikeParams(
        direction="long",
        volume_mult=8.0,
        atr_mult=5.0,
        min_move_pct=0,
        max_retrace_pct=10.0,
        arm_wait_sec=12.0,
        arm_retrace_grace_sec=3.0,
        arm_grace_max_tip_gap_pct=2.0,
    )
    snap_cold = _snap(vol_now=20.0, vol_sma=10.0, low=90.0, high=100.0)
    assert on_tick(state, params, snap_cold, last_price=90.0, now_ms=1_000) is None
    snap_hot = _snap(vol_now=80.0, vol_sma=10.0, low=90.0, high=100.0)
    # tip_gap=6% > 2
    assert tip_gap_pct(100.0, 90.0, 96.0) == 6.0
    assert on_tick(state, params, snap_hot, last_price=96.0, now_ms=2_000) is None


def test_arm_volume_after_grace_still_needs_retrace():
    """grace 过后量才够，回撤超限 → 仍不开。"""
    state = WickSymbolState()
    params = WickSpikeParams(
        direction="long",
        volume_mult=8.0,
        atr_mult=5.0,
        min_move_pct=0,
        max_retrace_pct=50.0,
        arm_wait_sec=12.0,
        arm_retrace_grace_sec=3.0,
        arm_grace_max_tip_gap_pct=0,  # 本测只看回撤
    )
    snap_cold = _snap(vol_now=20.0, vol_sma=10.0, low=90.0, high=100.0)
    assert on_tick(state, params, snap_cold, last_price=90.0, now_ms=1_000) is None

    snap_hot = _snap(vol_now=80.0, vol_sma=10.0, low=90.0, high=100.0)
    # 4s 后超过 grace
    assert on_tick(state, params, snap_hot, last_price=96.0, now_ms=5_000) is None
    # 回撤合格仍可开
    assert on_tick(state, params, snap_hot, last_price=94.0, now_ms=5_100) == Signal.LONG


def test_arm_expires_then_same_depth_volume_arrives_triggers():
    """量滞后于价：武装过期后，同根仍刺破 + 量达标 → 应触发（修复 SKYAI 类场景）。"""
    state = WickSymbolState()
    params = WickSpikeParams(
        direction="long",
        volume_mult=8.0,
        atr_mult=5.0,
        min_move_pct=0,
        max_retrace_pct=50.0,
        arm_wait_sec=2.0,
        arm_retrace_grace_sec=3.0,
    )
    snap_cold = _snap(vol_now=20.0, vol_sma=10.0, low=90.0)
    assert on_tick(state, params, snap_cold, last_price=90.0, now_ms=1_000) is None
    # 超时作废
    assert on_tick(state, params, snap_cold, last_price=90.0, now_ms=4_000) is None
    assert state.armed_bar_ts is None
    assert state.armed_expired_bar_ts == snap_cold.bar_open_ts
    # 量后来够了，同深度仍刺破 → 再武装并触发（量滞后于价的合法场景）
    snap_hot = _snap(vol_now=80.0, vol_sma=10.0, low=90.0)
    assert on_tick(state, params, snap_hot, last_price=90.0, now_ms=4_100) == Signal.LONG


def test_arm_expires_then_deeper_progress_rearms():
    """超时作废后，更深针（progress 创新高）可再武装并开仓。"""
    state = WickSymbolState()
    params = WickSpikeParams(
        direction="long",
        volume_mult=8.0,
        atr_mult=5.0,
        min_move_pct=0,
        max_retrace_pct=0,
        arm_wait_sec=2.0,
        arm_retrace_grace_sec=3.0,
    )
    # atr=1 → N=5；low=94 → progress=(100-94)/5=1.2
    snap1 = _snap(vol_now=20.0, vol_sma=10.0, low=94.0, high=100.0)
    assert on_tick(state, params, snap1, last_price=94.0, now_ms=1_000) is None
    assert on_tick(state, params, snap1, last_price=94.0, now_ms=4_000) is None
    assert abs(state.armed_expired_progress - 1.2) < 1e-9

    # 更深 low=90 → progress=2.0 > 1.2 → 再武装；量够即开
    snap2 = _snap(vol_now=80.0, vol_sma=10.0, low=90.0, high=100.0)
    assert on_tick(state, params, snap2, last_price=90.0, now_ms=4_100) == Signal.LONG


def test_same_tick_hot_volume_still_respects_retrace():
    """同刻量已够时不走 grace，回撤超限直接拒绝（不误开）。"""
    state = WickSymbolState()
    params = WickSpikeParams(
        direction="long",
        volume_mult=8.0,
        atr_mult=5.0,
        min_move_pct=0,
        max_retrace_pct=50.0,
        arm_wait_sec=12.0,
        arm_retrace_grace_sec=3.0,
    )
    snap = _snap(vol_now=80.0, vol_sma=10.0, low=90.0, high=100.0)
    assert on_tick(state, params, snap, last_price=96.0, now_ms=1) is None
    assert state.armed_awaiting_vol is False
    assert on_tick(state, params, snap, last_price=94.0, now_ms=2) == Signal.LONG


def test_volume_flicker_does_not_enable_grace():
    """武装时量已够：随后量短暂掉下去再回来，不得靠 grace 绕过回撤。"""
    state = WickSymbolState()
    params = WickSpikeParams(
        direction="long",
        volume_mult=8.0,
        atr_mult=5.0,
        min_move_pct=0,
        max_retrace_pct=50.0,
        arm_wait_sec=12.0,
        arm_retrace_grace_sec=3.0,
    )
    snap_hot = _snap(vol_now=80.0, vol_sma=10.0, low=90.0, high=100.0)
    assert on_tick(state, params, snap_hot, last_price=96.0, now_ms=1_000) is None
    assert state.armed_awaiting_vol is False
    snap_cold = _snap(vol_now=20.0, vol_sma=10.0, low=90.0, high=100.0)
    assert on_tick(state, params, snap_cold, last_price=96.0, now_ms=1_500) is None
    assert state.armed_awaiting_vol is False
    # 量回来但仍在坏回撤 → 仍拒绝
    assert on_tick(state, params, snap_hot, last_price=96.0, now_ms=2_000) is None


def test_confirm_max_retrace_capped_by_abort_when_rebound_on():
    assert confirm_max_retrace_pct(
        WickSpikeParams(
            direction="long",
            rebound_enabled=True,
            max_retrace_pct=50.0,
            rebound_abort_pct=35.0,
            rebound_trigger_pct=20.0,
        )
    ) == 35.0
    assert confirm_max_retrace_pct(
        WickSpikeParams(direction="long", rebound_enabled=False, max_retrace_pct=50.0)
    ) == 50.0


def test_rebound_arm_wait_zero_still_waits_for_bounce():
    """arm_wait=0 且开启反弹：同刻达标进窗，不立刻 Signal。"""
    state = WickSymbolState()
    params = WickSpikeParams(
        direction="short",
        volume_mult=8.0,
        atr_mult=5.0,
        min_move_pct=0,
        max_retrace_pct=50.0,
        arm_wait_sec=0,
        rebound_enabled=True,
        rebound_trigger_pct=20.0,
        rebound_abort_pct=40.0,
        rebound_wait_sec=5.0,
    )
    # open=100, high=110, 现价贴尖 → confirm 进窗
    snap = _snap(open_=100.0, atr=1.0, vol_now=80.0, vol_sma=10.0, high=110.0, low=100.0)
    assert on_tick(state, params, snap, last_price=110.0, now_ms=1_000) is None
    assert state.rebound_bar_ts == snap.bar_open_ts
    assert take_diag_event(state) == "rebound_enter"
    # 反弹 20%：110 → 108
    assert on_tick(state, params, snap, last_price=108.0, now_ms=1_100) == Signal.SHORT
    assert state.rebound_extreme == 110.0  # 触发后保留，供失败重试
    assert take_diag_event(state) == "rebound_fire"


def test_rebound_release_retries_same_bar():
    """反弹触发后开仓失败 release → 同根仍可再触发。"""
    state = WickSymbolState()
    params = WickSpikeParams(
        direction="short",
        volume_mult=8.0,
        atr_mult=5.0,
        min_move_pct=0,
        arm_wait_sec=0,
        rebound_enabled=True,
        rebound_trigger_pct=20.0,
        rebound_abort_pct=40.0,
        rebound_wait_sec=5.0,
    )
    snap = _snap(open_=100.0, atr=1.0, vol_now=80.0, vol_sma=10.0, high=110.0, low=100.0)
    assert on_tick(state, params, snap, last_price=110.0, now_ms=1_000) is None
    assert on_tick(state, params, snap, last_price=108.0, now_ms=1_100) == Signal.SHORT
    release_bar_trigger(state)
    assert state.rebound_bar_ts == snap.bar_open_ts
    assert on_tick(state, params, snap, last_price=108.0, now_ms=1_200) == Signal.SHORT
    mark_bar_triggered(state, params, snap.bar_open_ts, 1_300)
    assert state.rebound_bar_ts is None
    assert on_tick(state, params, snap, last_price=108.0, now_ms=1_400) is None


def test_rebound_blocks_confirm_past_abort():
    """开启反弹时 confirm 回撤上限=abort，已过放弃线不会进窗。"""
    state = WickSymbolState()
    params = WickSpikeParams(
        direction="long",
        volume_mult=8.0,
        atr_mult=5.0,
        min_move_pct=0,
        max_retrace_pct=50.0,
        arm_wait_sec=0,
        rebound_enabled=True,
        rebound_trigger_pct=20.0,
        rebound_abort_pct=35.0,
        rebound_wait_sec=5.0,
    )
    # open=100, low=90；现价 95 → 回撤 50% > abort 35%
    snap = _snap(vol_now=80.0, vol_sma=10.0, low=90.0, high=100.0)
    assert on_tick(state, params, snap, last_price=95.0, now_ms=1) is None
    assert state.rebound_bar_ts is None
    assert state.triggered_bar_ts is None


def test_rebound_same_tick_fire_when_already_bounced():
    """confirm 时已反弹过 trigger 且未过 abort → 同刻市价。"""
    state = WickSymbolState()
    params = WickSpikeParams(
        direction="short",
        volume_mult=8.0,
        atr_mult=5.0,
        min_move_pct=0,
        max_retrace_pct=50.0,
        arm_wait_sec=0,
        rebound_enabled=True,
        rebound_trigger_pct=20.0,
        rebound_abort_pct=40.0,
        rebound_wait_sec=5.0,
    )
    # ext=110 记在 high；现价 108 → 回撤 20% = trigger
    snap = _snap(open_=100.0, atr=1.0, vol_now=80.0, vol_sma=10.0, high=110.0, low=100.0)
    assert on_tick(state, params, snap, last_price=108.0, now_ms=1) == Signal.SHORT


def test_rebound_trigger_zero_fires_immediately():
    state = WickSymbolState()
    params = WickSpikeParams(
        direction="short",
        volume_mult=8.0,
        atr_mult=5.0,
        min_move_pct=0,
        arm_wait_sec=0,
        rebound_enabled=True,
        rebound_trigger_pct=0,
        rebound_abort_pct=35.0,
    )
    snap = _snap(open_=100.0, atr=1.0, vol_now=80.0, vol_sma=10.0, high=110.0, low=100.0)
    assert on_tick(state, params, snap, last_price=110.0, now_ms=1) == Signal.SHORT
    assert take_diag_event(state) == "rebound_immediate"


def test_rebound_abort_locks_bar_no_rearm():
    """abort 后同根不得再武装/再 fire（消 fire↔abort 风暴）。"""
    state = WickSymbolState()
    params = WickSpikeParams(
        direction="short",
        volume_mult=8.0,
        atr_mult=5.0,
        min_move_pct=0,
        max_retrace_pct=50.0,
        arm_wait_sec=0,
        rebound_enabled=True,
        rebound_trigger_pct=20.0,
        rebound_abort_pct=35.0,
        rebound_wait_sec=5.0,
    )
    snap = _snap(open_=100.0, atr=1.0, vol_now=80.0, vol_sma=10.0, high=110.0, low=100.0)
    # 进窗
    assert on_tick(state, params, snap, last_price=110.0, now_ms=1_000) is None
    assert state.rebound_bar_ts == snap.bar_open_ts
    # 跌破放弃线（回撤 35% → px <= 110 - 10*0.35 = 106.5）
    assert on_tick(state, params, snap, last_price=106.4, now_ms=1_100) is None
    assert state.rebound_done_bar_ts == snap.bar_open_ts
    assert take_diag_event(state) == "rebound_abort"
    # 即使价格回到触发区，本根也不再进
    assert on_tick(state, params, snap, last_price=108.0, now_ms=1_200) is None
    assert state.rebound_bar_ts is None
    assert state.armed_bar_ts is None


def test_rebound_new_extreme_resets_wait_timer():
    """破新高重置超时：进窗后继续拉升不应按首次 enter 计时 timeout。"""
    state = WickSymbolState()
    params = WickSpikeParams(
        direction="short",
        volume_mult=8.0,
        atr_mult=5.0,
        min_move_pct=0,
        max_retrace_pct=50.0,
        arm_wait_sec=0,
        rebound_enabled=True,
        rebound_trigger_pct=20.0,
        rebound_abort_pct=35.0,
        rebound_wait_sec=5.0,
    )
    snap = _snap(open_=100.0, atr=1.0, vol_now=80.0, vol_sma=10.0, high=110.0, low=100.0)
    assert on_tick(state, params, snap, last_price=110.0, now_ms=1_000) is None
    assert state.rebound_at_ms == 1_000
    # 4.5s 后破新高：应重置计时，不得 timeout
    snap2 = _snap(open_=100.0, atr=1.0, vol_now=80.0, vol_sma=10.0, high=112.0, low=100.0)
    assert on_tick(state, params, snap2, last_price=112.0, now_ms=5_500) is None
    assert state.rebound_extreme == 112.0
    assert state.rebound_at_ms == 5_500
    assert state.rebound_done_bar_ts is None
    assert take_diag_event(state) == "rebound_extend"
    # 自新尖起未满 5s：仍等待
    assert on_tick(state, params, snap2, last_price=111.5, now_ms=10_000) is None
    assert state.rebound_done_bar_ts is None
    # 自新尖起超过 5s 且未反弹 → timeout
    assert on_tick(state, params, snap2, last_price=111.5, now_ms=10_600) is None
    assert state.rebound_done_bar_ts == snap2.bar_open_ts
    assert take_diag_event(state) == "rebound_timeout"


def test_rebound_no_new_extreme_still_times_out():
    """针尖不创新高时，仍按 rebound_wait 超时。"""
    state = WickSymbolState()
    params = WickSpikeParams(
        direction="short",
        volume_mult=8.0,
        atr_mult=5.0,
        min_move_pct=0,
        max_retrace_pct=50.0,
        arm_wait_sec=0,
        rebound_enabled=True,
        rebound_trigger_pct=20.0,
        rebound_abort_pct=35.0,
        rebound_wait_sec=5.0,
    )
    snap = _snap(open_=100.0, atr=1.0, vol_now=80.0, vol_sma=10.0, high=110.0, low=100.0)
    assert on_tick(state, params, snap, last_price=110.0, now_ms=1_000) is None
    # 横盘贴尖、未破新高、未到 trigger（108）→ 5s 后 timeout
    assert on_tick(state, params, snap, last_price=109.5, now_ms=6_100) is None
    assert state.rebound_done_bar_ts == snap.bar_open_ts
    assert take_diag_event(state) == "rebound_timeout"


def test_is_arm_active_rebound_wait_zero_stays_active():
    """等反弹超时=0 表示不超时，反弹窗内应保持强制重判（修 ACE 进窗后失访）。"""
    from app.services.wick_spike_engine import is_arm_active

    state = WickSymbolState()
    params = WickSpikeParams(
        direction="short",
        volume_mult=8.0,
        atr_mult=5.0,
        min_move_pct=0,
        max_retrace_pct=50.0,
        arm_wait_sec=0,
        rebound_enabled=True,
        rebound_trigger_pct=20.0,
        rebound_abort_pct=35.0,
        rebound_wait_sec=0.0,
    )
    snap = _snap(open_=100.0, atr=1.0, vol_now=80.0, vol_sma=10.0, high=110.0, low=100.0)
    assert on_tick(state, params, snap, last_price=110.0, now_ms=1_000) is None
    assert state.rebound_bar_ts == snap.bar_open_ts
    assert is_arm_active(state, params, snap.bar_open_ts, now_ms=1_000) is True
    # 数十秒后仍应 active（不因 wait=0 被当成「已过期」）
    assert is_arm_active(state, params, snap.bar_open_ts, now_ms=60_000) is True
    # 新高应仍能延尖
    snap2 = _snap(open_=100.0, atr=1.0, vol_now=80.0, vol_sma=10.0, high=112.0, low=100.0)
    assert on_tick(state, params, snap2, last_price=112.0, now_ms=2_000) is None
    assert state.rebound_extreme == 112.0
    assert take_diag_event(state) == "rebound_extend"


def test_rebound_bar_rollover_timeouts_and_locks_new_bar():
    """换根时仍在反弹窗 → timeout，且新根不得立刻再进窗开火（AIO 跨分钟开仓）。"""
    state = WickSymbolState()
    params = WickSpikeParams(
        direction="long",
        volume_mult=8.0,
        atr_mult=5.0,
        min_move_pct=0,
        max_retrace_pct=50.0,
        arm_wait_sec=0,
        rebound_enabled=True,
        rebound_trigger_pct=20.0,
        rebound_abort_pct=35.0,
        rebound_wait_sec=0.0,
    )
    # 01 分：刺破低点进反弹窗，一直延尖未反弹
    snap1 = _snap(
        open_=100.0,
        atr=1.0,
        vol_now=80.0,
        vol_sma=10.0,
        high=100.0,
        low=90.0,
        ts=1_000_000,
    )
    assert on_tick(state, params, snap1, last_price=90.0, now_ms=1_000) is None
    assert state.rebound_bar_ts == snap1.bar_open_ts
    assert take_diag_event(state) == "rebound_enter"
    # :59 仍在加深针尖
    snap1b = _snap(
        open_=100.0,
        atr=1.0,
        vol_now=80.0,
        vol_sma=10.0,
        high=100.0,
        low=88.0,
        ts=1_000_000,
    )
    assert on_tick(state, params, snap1b, last_price=88.0, now_ms=59_000) is None
    assert take_diag_event(state) == "rebound_extend"

    # 02 分：同价位继续深跌+放量，旧逻辑会立刻 rebound_enter→fire；现应 timeout 并锁新根
    snap2 = _snap(
        open_=89.0,
        atr=1.0,
        vol_now=80.0,
        vol_sma=10.0,
        high=89.5,
        low=87.0,
        ts=1_060_000,
    )
    assert on_tick(state, params, snap2, last_price=87.0, now_ms=60_000) is None
    assert take_diag_event(state) == "rebound_timeout"
    assert state.rebound_bar_ts is None
    assert state.rebound_done_bar_ts == snap2.bar_open_ts

    # 同根稍后即使刺破+反弹触及 trigger，也不得开火
    snap2b = _snap(
        open_=89.0,
        atr=1.0,
        vol_now=80.0,
        vol_sma=10.0,
        high=89.5,
        low=87.0,
        ts=1_060_000,
    )
    # 从 87 反弹到足够触发的价位（相对 tip87、open89 的 20%）
    assert on_tick(state, params, snap2b, last_price=88.5, now_ms=61_000) is None
    assert state.triggered_bar_ts != snap2.bar_open_ts

    # 再下一根（03 分）应解除锁定，可正常进反弹
    snap3 = _snap(
        open_=100.0,
        atr=1.0,
        vol_now=80.0,
        vol_sma=10.0,
        high=100.0,
        low=90.0,
        ts=1_120_000,
    )
    assert on_tick(state, params, snap3, last_price=90.0, now_ms=120_000) is None
    assert state.rebound_done_bar_ts is None
    assert state.rebound_bar_ts == snap3.bar_open_ts
    assert take_diag_event(state) == "rebound_enter"


def _snap_ema(*, open_, ema25, **kwargs) -> WickBarSnapshot:
    s = _snap(open_=open_, **kwargs)
    from dataclasses import replace

    return replace(s, ema25=ema25, ema_filter_open=open_)


def test_ema25_filter_blocks_short_below_ema():
    """做空：开盘 < EMA30 → 即使刺破+量够也不开。"""
    from app.services.wick_spike_engine import ema25_filter_blocks

    state = WickSymbolState()
    params = WickSpikeParams(
        direction="short",
        volume_mult=8.0,
        atr_mult=5.0,
        min_move_pct=0,
        max_retrace_pct=0,
        arm_wait_sec=0,
        ema25_filter_enabled=True,
    )
    snap = _snap_ema(
        open_=100.0, ema25=101.0, atr=1.0, vol_now=80.0, vol_sma=10.0, high=110.0, low=99.0
    )
    assert ema25_filter_blocks(params, snap) is True
    assert on_tick(state, params, snap, last_price=110.0, now_ms=1) is None


def test_ema25_filter_allows_short_above_ema():
    state = WickSymbolState()
    params = WickSpikeParams(
        direction="short",
        volume_mult=8.0,
        atr_mult=5.0,
        min_move_pct=0,
        max_retrace_pct=0,
        arm_wait_sec=0,
        ema25_filter_enabled=True,
    )
    snap = _snap_ema(
        open_=100.0, ema25=99.0, atr=1.0, vol_now=80.0, vol_sma=10.0, high=110.0, low=99.0
    )
    assert on_tick(state, params, snap, last_price=110.0, now_ms=1) == Signal.SHORT


def test_ema25_filter_blocks_long_above_ema():
    state = WickSymbolState()
    params = WickSpikeParams(
        direction="long",
        volume_mult=8.0,
        atr_mult=5.0,
        min_move_pct=0,
        max_retrace_pct=0,
        arm_wait_sec=0,
        ema25_filter_enabled=True,
    )
    snap = _snap_ema(
        open_=100.0, ema25=99.0, atr=1.0, vol_now=80.0, vol_sma=10.0, high=101.0, low=90.0
    )
    assert on_tick(state, params, snap, last_price=90.0, now_ms=1) is None


def test_ema25_filter_allows_long_below_ema():
    state = WickSymbolState()
    params = WickSpikeParams(
        direction="long",
        volume_mult=8.0,
        atr_mult=5.0,
        min_move_pct=0,
        max_retrace_pct=0,
        arm_wait_sec=0,
        ema25_filter_enabled=True,
    )
    snap = _snap_ema(
        open_=100.0, ema25=101.0, atr=1.0, vol_now=80.0, vol_sma=10.0, high=101.0, low=90.0
    )
    assert on_tick(state, params, snap, last_price=90.0, now_ms=1) == Signal.LONG


def test_ema25_filter_off_ignores_ema():
    state = WickSymbolState()
    params = WickSpikeParams(
        direction="short",
        volume_mult=8.0,
        atr_mult=5.0,
        min_move_pct=0,
        max_retrace_pct=0,
        arm_wait_sec=0,
        ema25_filter_enabled=False,
    )
    snap = _snap_ema(
        open_=100.0, ema25=101.0, atr=1.0, vol_now=80.0, vol_sma=10.0, high=110.0, low=99.0
    )
    assert on_tick(state, params, snap, last_price=110.0, now_ms=1) == Signal.SHORT


def test_ema25_nan_does_not_block():
    """缓冲不足算不出 EMA 时不过滤。"""
    state = WickSymbolState()
    params = WickSpikeParams(
        direction="short",
        volume_mult=8.0,
        atr_mult=5.0,
        min_move_pct=0,
        max_retrace_pct=0,
        arm_wait_sec=0,
        ema25_filter_enabled=True,
    )
    snap = _snap(open_=100.0, atr=1.0, vol_now=80.0, vol_sma=10.0, high=110.0, low=99.0)
    assert on_tick(state, params, snap, last_price=110.0, now_ms=1) == Signal.SHORT


def test_merge_synthetic_forming_bar_builds_new_root():
    from app.services.wick_spike_engine import merge_synthetic_forming_bar

    # 两根已收盘；forming 停在旧根
    klines = [
        [60_000, 100.0, 101.0, 99.0, 100.5, 10.0],
        [120_000, 100.5, 102.0, 100.0, 101.0, 12.0],
    ]
    out = merge_synthetic_forming_bar(
        klines,
        current_bar_ts=180_000,
        last_price=103.0,
        trade_high=104.0,
        trade_low=100.8,
        trade_vol=5.0,
    )
    assert out is not None
    assert len(out) == 3
    assert out[-1][0] == 180_000
    assert out[-1][1] == 101.0  # prev close as open
    assert out[-1][2] == 104.0
    assert out[-1][3] == 100.8
    assert out[-1][4] == 103.0
    assert out[-1][5] == 5.0


def test_merge_synthetic_noop_when_forming_current():
    from app.services.wick_spike_engine import merge_synthetic_forming_bar

    klines = [
        [60_000, 100.0, 101.0, 99.0, 100.5, 10.0],
        [120_000, 100.5, 102.0, 100.0, 101.0, 12.0],
    ]
    out = merge_synthetic_forming_bar(
        klines,
        current_bar_ts=120_000,
        last_price=101.5,
        trade_high=102.0,
        trade_low=100.0,
        trade_vol=1.0,
    )
    assert out is not None
    assert len(out) == 2
    assert out[-1][0] == 120_000


def test_merge_synthetic_uses_trade_open():
    """trade_open（第一笔成交价）优先于 prev_close 做开盘价。"""
    from app.services.wick_spike_engine import merge_synthetic_forming_bar

    klines = [
        [60_000, 100.0, 101.0, 99.0, 100.5, 10.0],
        [120_000, 100.5, 102.0, 100.0, 101.0, 12.0],
    ]
    out = merge_synthetic_forming_bar(
        klines,
        current_bar_ts=180_000,
        last_price=103.0,
        trade_high=104.0,
        trade_low=100.8,
        trade_vol=5.0,
        trade_open=102.5,  # 第一笔成交价 ≠ prev_close(101.0)
    )
    assert out is not None
    assert out[-1][1] == 102.5  # trade_open as open, not prev_close

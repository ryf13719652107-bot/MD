"""毫秒接针状态机（纯逻辑，无 I/O）。

流程（Arm–Confirm）：
  1) 刺破 + 最小涨跌幅 → 武装（记下极值/时刻；若当时量不够则 awaiting_vol）
  2) 武装窗内等量能达标 → 确认开仓
  3) 回撤：同刻量已够则照常检查；若武装时在等量，则 grace 秒内免回撤（吃量滞后）
  4) 超时未放量 → 本根作废，不再重武装

N = 上根 ATR × atr_mult。开盘价由调用方传入（须为 K 线官方 open）。

progress 量能放宽（可选，默认开）：
  progress = |极值-开盘| / N；
  progress < start(1.0) 不放宽；
  start→full(1.5) 时 need 从 volume_mult 线性降到 vol_relax_mult(5×)；
  progress ≥ full 时 need = vol_relax_mult。

min_move_pct（默认 3）：本根极值相对开盘的涨跌幅 % 须 ≥ 该值才允许武装；0=关闭。
max_retrace_pct（默认 50）：现价相对「开盘→极值」已回撤的比例 %；超过则跳过；0=关闭。
arm_wait_sec（默认 12）：刺破后最多等多久的量；0=关闭武装（恢复旧「同刻全条件」）。
arm_retrace_grace_sec（默认 3）：武装时量不够，则确认时前 N 秒免回撤门禁。
arm_grace_max_tip_gap_pct（默认 2）：grace 免回撤时，进场价相对极值的 tip_gap% 上限；0=不限制。
超时作废后若同根仍刺破可再次武装（量滞后于价时给量能追上来的机会）。

反弹追踪（方案J，rebound_enabled）：
  confirm 达标后不立刻市价，进入反弹窗；针尖可加深；
  现价从针尖反弹达 trigger% → 市价信号（保留 rebound 态，开仓失败可同根重试）；
  反弹达 abort% 或超时 → 放弃。
  confirm 时有效回撤上限 = min(max_retrace, abort)（避免 confirm 时已过放弃线）。
  arm_wait=0 时若开启 rebound，同刻全条件达标后仍走反弹窗（不再绕过）。
  trigger_pct<=0：confirm 后立刻市价（不等反弹）。
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Optional

from .strategy_engine import Signal, calculate_atr

# progress 量能放宽默认阈值（开关打开时生效）
_VOL_RELAX_PROGRESS_START = 1.0
_VOL_RELAX_PROGRESS_FULL = 1.5
_VOL_RELAX_MULT = 5.0

_MIN_MOVE_PCT = 3.0
_MAX_RETRACE_PCT = 50.0
_ARM_WAIT_SEC = 12.0
_ARM_RETRACE_GRACE_SEC = 6.0
_ARM_GRACE_MAX_TIP_GAP_PCT = 2.0


@dataclass
class WickSpikeParams:
    direction: str  # "long" | "short"
    volume_mult: float = 8.0
    atr_mult: float = 5.0
    cooldown_sec: float = 0.0
    # progress 量能放宽（默认开）
    vol_relax_enabled: bool = True
    vol_relax_progress_start: float = _VOL_RELAX_PROGRESS_START
    vol_relax_progress_full: float = _VOL_RELAX_PROGRESS_FULL
    vol_relax_mult: float = _VOL_RELAX_MULT
    # 本根相对开盘最小涨跌幅 %（0=关闭）
    min_move_pct: float = _MIN_MOVE_PCT
    # 开盘→极值方向上回撤占比上限 %（0=关闭）
    max_retrace_pct: float = _MAX_RETRACE_PCT
    # 刺破后等量窗口（秒）；0=关闭武装，走同刻全条件
    arm_wait_sec: float = _ARM_WAIT_SEC
    # 等量期间回撤宽限（秒）；仅 armed_awaiting_vol 时生效
    arm_retrace_grace_sec: float = _ARM_RETRACE_GRACE_SEC
    # grace 免回撤时 tip_gap% 上限（0=不限制）
    arm_grace_max_tip_gap_pct: float = _ARM_GRACE_MAX_TIP_GAP_PCT
    # 反弹追踪（方案J）：confirm后等价格从针尖反弹触发市价，追踪真针尖
    rebound_enabled: bool = False
    rebound_trigger_pct: float = 20.0  # 反弹占针深%触发市价
    rebound_abort_pct: float = 35.0    # 反弹占针深%放弃
    rebound_wait_sec: float = 5.0      # confirm后等反弹超时秒数


@dataclass
class WickBarSnapshot:
    """单根未收盘 K 的上下文（由 Runner 从 kline_stream 组装）。"""

    bar_open_ts: int
    bar_open: float
    atr: float  # 上根 ATR（已收盘序列）
    vol_now: float
    vol_sma: float
    kline_high: float
    kline_low: float


@dataclass
class WickSymbolState:
    bar_open_ts: Optional[int] = None
    bar_low: Optional[float] = None
    bar_high: Optional[float] = None
    triggered_bar_ts: Optional[int] = None
    cooldown_until_ms: int = 0
    # Arm–Confirm
    armed_bar_ts: Optional[int] = None
    armed_at_ms: int = 0
    armed_extreme: Optional[float] = None
    armed_awaiting_vol: bool = False
    armed_expired_bar_ts: Optional[int] = None
    # 作废时的 progress；同根仅当 progress 严格更高才允许再武装
    armed_expired_progress: float = 0.0
    # Rebound tracking（confirm后等反弹触发市价）
    rebound_bar_ts: Optional[int] = None
    rebound_at_ms: int = 0
    rebound_extreme: Optional[float] = None
    # 供 runner 打专用日志后清空：rebound_enter / rebound_fire / rebound_abort / …
    diag_event: Optional[str] = None


@dataclass
class PierceVolView:
    """刺破/量能快照（供 runner 武装窗强制重判）。"""

    pierced: bool
    vol_hot: bool
    progress: float
    need: float
    vol_ratio: float
    armed: bool = False
    arm_age_ms: int = 0
    retrace_pct: float = 0.0


def volume_sma(closed_volumes: list[float], period: int) -> Optional[float]:
    if period <= 0 or len(closed_volumes) < period:
        return None
    window = closed_volumes[-period:]
    return sum(window) / period


def build_bar_snapshot(
    klines: list,
    *,
    atr_period: int = 14,
    volume_sma_period: int = 20,
) -> Optional[WickBarSnapshot]:
    """从 OHLCV 列表构建当前未收盘 bar 快照；ATR/量均只用已收盘 K。"""
    if not klines or len(klines) < atr_period + 2:
        return None
    forming = klines[-1]
    closed = klines[:-1]
    if len(closed) < atr_period + 1:
        return None
    atr_series = calculate_atr(closed, atr_period)
    if not atr_series:
        return None
    atr = float(atr_series[-1])
    if atr <= 0:
        return None
    vols = [float(r[5]) for r in closed]
    v_sma = volume_sma(vols, volume_sma_period)
    if v_sma is None or v_sma <= 0:
        return None
    try:
        return WickBarSnapshot(
            bar_open_ts=int(forming[0]),
            bar_open=float(forming[1]),
            atr=atr,
            vol_now=float(forming[5]),
            vol_sma=v_sma,
            kline_high=float(forming[2]),
            kline_low=float(forming[3]),
        )
    except (TypeError, ValueError, IndexError):
        return None


def _volume_hot(
    params: WickSpikeParams, snap: WickBarSnapshot, *, volume_mult: float | None = None
) -> bool:
    mult = params.volume_mult if volume_mult is None else volume_mult
    if mult <= 0:
        return True
    return snap.vol_now >= snap.vol_sma * mult


def spike_progress(direction: str, bar_open: float, extreme: float, n: float) -> float:
    """刺破进度 = |极值-开盘| / N。"""
    if n <= 0 or bar_open <= 0 or extreme <= 0:
        return 0.0
    d = (direction or "").lower()
    if d == "short":
        return max(0.0, (extreme - bar_open) / n)
    if d == "long":
        return max(0.0, (bar_open - extreme) / n)
    return 0.0


def spike_move_pct(direction: str, bar_open: float, extreme: float) -> float:
    """本根接针方向涨跌幅 % = |极值-开盘| / 开盘 × 100。"""
    if bar_open <= 0 or extreme <= 0:
        return 0.0
    d = (direction or "").lower()
    if d == "short":
        return max(0.0, (extreme - bar_open) / bar_open * 100.0)
    if d == "long":
        return max(0.0, (bar_open - extreme) / bar_open * 100.0)
    return 0.0


def _move_ok(params: WickSpikeParams, direction: str, bar_open: float, extreme: float) -> bool:
    min_pct = float(params.min_move_pct or 0)
    if min_pct <= 0:
        return True
    return spike_move_pct(direction, bar_open, extreme) + 1e-12 >= min_pct


def tip_gap_pct(bar_open: float, extreme: float, entry_px: float) -> float:
    """进场价相对本根极值的距离（占开盘价 %）。越小越贴针尖。仅用于日志/统计。"""
    if bar_open <= 0:
        return 0.0
    return abs(float(entry_px) - float(extreme)) / bar_open * 100.0


def wick_retrace_pct(
    direction: str, bar_open: float, extreme: float, last_price: float
) -> float:
    """现价相对「开盘→极值」已回撤的比例 %。

    0 = 仍在极值；100 = 已回到开盘。做多：从低点往上收回；做空：从高点往下收回。
    """
    d = (direction or "").lower()
    if d == "long":
        span = float(bar_open) - float(extreme)
        if span <= 0:
            return 0.0
        recovered = float(last_price) - float(extreme)
        return max(0.0, min(100.0, recovered / span * 100.0))
    if d == "short":
        span = float(extreme) - float(bar_open)
        if span <= 0:
            return 0.0
        recovered = float(extreme) - float(last_price)
        return max(0.0, min(100.0, recovered / span * 100.0))
    return 0.0


def confirm_max_retrace_pct(params: WickSpikeParams) -> float:
    """confirm 用回撤上限。开启反弹时再与 abort% 取较小，避免进窗即放弃。

    返回值 <=0 表示不限制。
    """
    max_r = float(params.max_retrace_pct or 0)
    if not params.rebound_enabled:
        return max_r
    abort = float(params.rebound_abort_pct or 0)
    trig = float(params.rebound_trigger_pct or 0)
    # abort<=trigger 时 abort 视为关闭（与运行时一致）
    if abort > 0 and trig > 0 and abort <= trig + 1e-12:
        abort = 0.0
    if abort <= 0:
        return max_r
    if max_r <= 0:
        return abort
    return min(max_r, abort)


def _retrace_ok(
    params: WickSpikeParams,
    direction: str,
    bar_open: float,
    extreme: float,
    last_price: float,
    *,
    max_retrace_override: float | None = None,
) -> bool:
    max_r = (
        float(max_retrace_override)
        if max_retrace_override is not None
        else float(params.max_retrace_pct or 0)
    )
    if max_r <= 0:
        return True
    return wick_retrace_pct(direction, bar_open, extreme, last_price) <= max_r + 1e-12


def take_diag_event(state: WickSymbolState) -> Optional[str]:
    """取出并清空 diag_event（供 runner 打日志）。"""
    ev = state.diag_event
    state.diag_event = None
    return ev


def _norm_rebound_pcts(params: WickSpikeParams) -> tuple[float, float]:
    """返回 (trigger_pct, abort_pct)；abort<=trigger 时禁用 abort。"""
    trig = float(params.rebound_trigger_pct or 0)
    abort = float(params.rebound_abort_pct or 0)
    if abort > 0 and trig > 0 and abort <= trig + 1e-12:
        abort = 0.0
    return trig, abort


def _rebound_lines(
    *,
    is_long: bool,
    bar_open: float,
    rb_ext: float,
    trig_pct: float,
    abort_pct: float,
) -> tuple[float, float, float]:
    """返回 (spike_depth, trig_line, abort_line)。depth<=0 时 lines 无意义。"""
    depth = abs(float(bar_open) - float(rb_ext))
    if is_long:
        trig_line = rb_ext + depth * trig_pct / 100.0
        abort_line = rb_ext + depth * abort_pct / 100.0
    else:
        trig_line = rb_ext - depth * trig_pct / 100.0
        abort_line = rb_ext - depth * abort_pct / 100.0
    return depth, trig_line, abort_line


def _fire_signal(state: WickSymbolState, snap: WickBarSnapshot, is_long: bool) -> Signal:
    state.triggered_bar_ts = snap.bar_open_ts
    state.armed_awaiting_vol = False
    return Signal.LONG if is_long else Signal.SHORT


def _process_rebound_window(
    state: WickSymbolState,
    params: WickSpikeParams,
    snap: WickBarSnapshot,
    last_price: float,
    now_ms: int,
    *,
    is_long: bool,
    extreme: float,
) -> Optional[Signal]:
    """反弹窗内：加深针尖 / 触发 / 放弃 / 超时。触发时保留 rebound 供开仓失败重试。"""
    if state.rebound_extreme is None:
        state.rebound_extreme = float(extreme)
    if is_long:
        state.rebound_extreme = min(float(state.rebound_extreme), float(extreme))
    else:
        state.rebound_extreme = max(float(state.rebound_extreme), float(extreme))
    rb_ext = float(state.rebound_extreme)
    trig_pct, abort_pct = _norm_rebound_pcts(params)
    depth, trig_line, abort_line = _rebound_lines(
        is_long=is_long,
        bar_open=snap.bar_open,
        rb_ext=rb_ext,
        trig_pct=trig_pct,
        abort_pct=abort_pct,
    )
    if depth <= 0:
        clear_rebound(state)
        state.diag_event = "rebound_abort"
        return None

    if is_long:
        if abort_pct > 0 and last_price >= abort_line - 1e-12:
            clear_rebound(state)
            state.diag_event = "rebound_abort"
            return None
        if trig_pct > 0 and last_price >= trig_line - 1e-12:
            state.diag_event = "rebound_fire"
            return _fire_signal(state, snap, True)
    else:
        if abort_pct > 0 and last_price <= abort_line + 1e-12:
            clear_rebound(state)
            state.diag_event = "rebound_abort"
            return None
        if trig_pct > 0 and last_price <= trig_line + 1e-12:
            state.diag_event = "rebound_fire"
            return _fire_signal(state, snap, False)

    rw = float(params.rebound_wait_sec or 0)
    if rw > 0 and (now_ms - state.rebound_at_ms) > int(rw * 1000):
        clear_rebound(state)
        state.diag_event = "rebound_timeout"
        return None
    return None


def _after_confirm(
    state: WickSymbolState,
    params: WickSpikeParams,
    snap: WickBarSnapshot,
    last_price: float,
    now_ms: int,
    *,
    is_long: bool,
    extreme: float,
) -> Optional[Signal]:
    """confirm 达标：无反弹则立刻信号；有反弹则进窗并同刻评估。"""
    clear_arm(state)
    if not params.rebound_enabled:
        return _fire_signal(state, snap, is_long)

    trig_pct, _abort = _norm_rebound_pcts(params)
    # trigger<=0：confirm 后立刻市价（不等反弹）
    if trig_pct <= 0:
        state.diag_event = "rebound_immediate"
        return _fire_signal(state, snap, is_long)

    state.rebound_bar_ts = snap.bar_open_ts
    state.rebound_at_ms = now_ms
    state.rebound_extreme = float(extreme)
    state.diag_event = "rebound_enter"
    # 同刻评估：已过 abort → 放弃；已过 trigger → 立刻火；否则等待
    sig = _process_rebound_window(
        state,
        params,
        snap,
        last_price,
        now_ms,
        is_long=is_long,
        extreme=extreme,
    )
    # 进窗即放弃时覆盖事件名，便于区分「从未有效等待」
    if sig is None and state.rebound_bar_ts is None and state.diag_event == "rebound_abort":
        state.diag_event = "rebound_skip_past_abort"
    elif sig is not None and state.diag_event == "rebound_fire":
        pass  # 进窗同刻触发
    return sig


def snapshot_extreme(direction: str, snap: WickBarSnapshot, last_price: float) -> float:
    """用 K 线高低 + 最新价估本根接针方向极值。"""
    d = (direction or "").lower()
    if d == "short":
        hi = snap.kline_high if snap.kline_high > 0 else last_price
        return max(hi, last_price)
    if d == "long":
        lo = snap.kline_low if snap.kline_low > 0 else last_price
        return min(lo, last_price) if lo > 0 else last_price
    return last_price


def effective_volume_mult(params: WickSpikeParams, progress: float) -> float:
    """progress 量能放宽下的有效放量倍数。"""
    if not params.vol_relax_enabled:
        return params.volume_mult
    if params.volume_mult <= 0:
        return 0.0

    floor = min(params.volume_mult, max(0.0, params.vol_relax_mult))
    start = float(params.vol_relax_progress_start)
    full = float(params.vol_relax_progress_full)

    if progress < start:
        return params.volume_mult
    if full <= start or progress >= full:
        return floor

    t = (progress - start) / (full - start)
    return params.volume_mult + t * (floor - params.volume_mult)


def enrich_snap_with_trades(
    snap: WickBarSnapshot,
    *,
    trade_vol: float = 0.0,
    trade_high: float = 0.0,
    trade_low: float = 0.0,
    trade_bar_open_ts: int | None = None,
    trade_instant_vol: float = 0.0,
) -> WickBarSnapshot:
    """用成交流累计的量/高低补强 K 线快照（解决 K 线 WS 量能滞后）。

    trade_bar_open_ts 若与 snap.bar_open_ts 不一致（换根后尚未有新成交），
    忽略本根累计量/高低，避免上一根巨量/极值污染新根。
    trade_instant_vol（最近 N 秒折算到分钟的瞬时量）不受 bar 对齐约束：
    它反映"此刻"的放量程度，跨根也有效，专治开盘前几秒累计量滞后。
    """
    # 瞬时折算量总是纳入（跨根有效）
    vol = max(float(snap.vol_now or 0), float(trade_instant_vol or 0))
    hi = float(snap.kline_high or 0)
    lo = float(snap.kline_low or 0)
    # bar 对齐时才纳入本根累计量/高低
    if trade_bar_open_ts is None or int(trade_bar_open_ts) == int(snap.bar_open_ts):
        vol = max(vol, float(trade_vol or 0))
        if trade_high > 0:
            hi = max(hi, trade_high) if hi > 0 else trade_high
        if trade_low > 0:
            lo = min(lo, trade_low) if lo > 0 else trade_low
    if vol == snap.vol_now and hi == snap.kline_high and lo == snap.kline_low:
        return snap
    return replace(snap, vol_now=vol, kline_high=hi, kline_low=lo)


def clear_arm(state: WickSymbolState) -> None:
    state.armed_bar_ts = None
    state.armed_at_ms = 0
    state.armed_extreme = None
    state.armed_awaiting_vol = False


def clear_rebound(state: WickSymbolState) -> None:
    """清除反弹追踪状态。"""
    state.rebound_bar_ts = None
    state.rebound_at_ms = 0
    state.rebound_extreme = None


def is_arm_active(
    state: WickSymbolState, params: WickSpikeParams, bar_open_ts: int, now_ms: int
) -> bool:
    """武装窗或反弹追踪窗是否仍有效（供 runner 强制重判）。"""
    # 反弹追踪状态：confirm后等反弹触发
    if (
        state.rebound_bar_ts == bar_open_ts
        and state.rebound_at_ms > 0
        and state.triggered_bar_ts != bar_open_ts
    ):
        rw = float(params.rebound_wait_sec or 0)
        if rw <= 0:
            return False
        return (now_ms - state.rebound_at_ms) <= int(rw * 1000) + 1

    # 原武装窗检查
    wait = float(params.arm_wait_sec or 0)
    if wait <= 0:
        return False
    if state.armed_bar_ts != bar_open_ts or state.armed_at_ms <= 0:
        return False
    if state.triggered_bar_ts == bar_open_ts:
        return False
    return (now_ms - state.armed_at_ms) <= int(wait * 1000) + 1


def _retrace_waived(state: WickSymbolState, params: WickSpikeParams, now_ms: int) -> bool:
    """武装时在等量，且仍在 grace 内 → 免回撤（专治量滞后）。"""
    if not state.armed_awaiting_vol:
        return False
    grace = float(params.arm_retrace_grace_sec or 0)
    if grace <= 0 or state.armed_at_ms <= 0:
        return False
    age_sec = (now_ms - state.armed_at_ms) / 1000.0
    return age_sec <= grace + 1e-12


def near_miss_diag(
    params: WickSpikeParams,
    snap: WickBarSnapshot,
    state: WickSymbolState,
    last_price: float,
    *,
    now_ms: int | None = None,
) -> Optional[str]:
    """近阈值诊断文案（仅供服务器 logger，勿写入前端策略日志）。"""
    if last_price <= 0 or snap.bar_open <= 0 or snap.atr <= 0:
        return None
    if state.triggered_bar_ts == snap.bar_open_ts:
        return None

    n = snap.atr * params.atr_mult
    if n <= 0:
        return None

    direction = (params.direction or "").lower()
    hi = state.bar_high if state.bar_high is not None else max(last_price, snap.kline_high)
    lo = state.bar_low if state.bar_low is not None else min(last_price, snap.kline_low)

    if direction == "short":
        thr = snap.bar_open + n
        extreme = max(hi, last_price)
        if state.armed_extreme is not None and state.armed_bar_ts == snap.bar_open_ts:
            extreme = max(extreme, float(state.armed_extreme))
        pierced = extreme >= thr
    elif direction == "long":
        thr = snap.bar_open - n
        extreme = min(lo, last_price)
        if state.armed_extreme is not None and state.armed_bar_ts == snap.bar_open_ts:
            extreme = min(extreme, float(state.armed_extreme))
        pierced = extreme <= thr
    else:
        return None

    progress = spike_progress(direction, snap.bar_open, extreme, n)
    move_pct = spike_move_pct(direction, snap.bar_open, extreme)
    need = effective_volume_mult(params, progress)
    vol_ratio = (snap.vol_now / snap.vol_sma) if snap.vol_sma > 0 else 0.0
    vol_hot = _volume_hot(params, snap, volume_mult=need)
    vol_near = need <= 0 or vol_ratio >= need * 0.5
    px_near = progress >= 0.5
    armed = state.armed_bar_ts == snap.bar_open_ts and state.armed_at_ms > 0
    if not pierced and not px_near and not armed:
        return None
    if not (vol_near or px_near or armed):
        return None

    ts = int(now_ms) if now_ms is not None else state.armed_at_ms
    arm_age = (ts - state.armed_at_ms) if armed and state.armed_at_ms > 0 else 0
    retrace = wick_retrace_pct(direction, snap.bar_open, extreme, last_price)
    waived = _retrace_waived(state, params, ts) if armed else False

    return (
        f"dir={direction} px={last_price:.6g} open={snap.bar_open:.6g} "
        f"ext={extreme:.6g} thr={thr:.6g} pierce={pierced} "
        f"atrN={n:.6g} progress={progress:.2f} amp%={move_pct:.2f} "
        f"vol×={vol_ratio:.2f} need×={need:g} vol_hot={vol_hot} "
        f"retrace%={retrace:.2f} armed={armed} arm_age_ms={arm_age} "
        f"await_vol={state.armed_awaiting_vol if armed else False} "
        f"retrace_waived={waived}"
    )


def release_bar_trigger(state: WickSymbolState) -> None:
    """开仓未真正执行时回滚本根触发标记，允许同根 K 再次尝试。

    保留武装窗 / 反弹窗（反弹触发时不清 rebound，失败后可同根再判）。
    """
    state.triggered_bar_ts = None
    state.cooldown_until_ms = 0


def mark_bar_triggered(
    state: WickSymbolState,
    params: WickSpikeParams,
    bar_open_ts: int,
    now_ms: int,
) -> None:
    """开仓已提交（或确认不再重试）后锁定本根，防止重复下单。"""
    state.triggered_bar_ts = bar_open_ts
    clear_arm(state)
    clear_rebound(state)
    if params.cooldown_sec > 0:
        state.cooldown_until_ms = now_ms + int(params.cooldown_sec * 1000)


def _pierce_threshold(
    direction: str, bar_open: float, n: float
) -> tuple[float, bool]:
    """返回 (threshold, is_long)。"""
    if direction == "long":
        return bar_open - n, True
    return bar_open + n, False


def on_tick(
    state: WickSymbolState,
    params: WickSpikeParams,
    snap: WickBarSnapshot,
    last_price: float,
    now_ms: int,
) -> Optional[Signal]:
    """处理一笔最新价；触发则返回 LONG/SHORT，否则 None。

    注意：返回信号时**先**标记本根已触发；若调用方未能开仓，须 `release_bar_trigger`
    才能同根重试。成功开仓后调用 `mark_bar_triggered` 写入冷却（若配置了冷却秒数）。
    """
    if last_price <= 0 or snap.bar_open <= 0 or snap.atr <= 0:
        return None

    # 新 K：重置极值与武装
    if state.bar_open_ts != snap.bar_open_ts:
        state.bar_open_ts = snap.bar_open_ts
        state.bar_low = min(last_price, snap.kline_low) if snap.kline_low > 0 else last_price
        state.bar_high = max(last_price, snap.kline_high) if snap.kline_high > 0 else last_price
        clear_arm(state)
        clear_rebound(state)
        state.armed_expired_bar_ts = None
        state.armed_expired_progress = 0.0

    # 极值：成交价 + K 线 H/L 取更极端
    lo = min(last_price, snap.kline_low) if snap.kline_low > 0 else last_price
    hi = max(last_price, snap.kline_high) if snap.kline_high > 0 else last_price
    state.bar_low = lo if state.bar_low is None else min(state.bar_low, lo)
    state.bar_high = hi if state.bar_high is None else max(state.bar_high, hi)

    if state.triggered_bar_ts == snap.bar_open_ts:
        return None
    if params.cooldown_sec > 0 and now_ms < state.cooldown_until_ms:
        return None

    direction = (params.direction or "").lower()
    if direction == "long":
        extreme = state.bar_low if state.bar_low is not None else last_price
    elif direction == "short":
        extreme = state.bar_high if state.bar_high is not None else last_price
    else:
        return None

    n = snap.atr * params.atr_mult
    if n <= 0:
        return None

    thr, is_long = _pierce_threshold(direction, snap.bar_open, n)
    pierced = (extreme <= thr or last_price <= thr) if is_long else (
        extreme >= thr or last_price >= thr
    )

    # —— 反弹追踪窗（方案J）：confirm 后等价格从针尖反弹 ——
    if (
        params.rebound_enabled
        and state.rebound_bar_ts == snap.bar_open_ts
        and state.rebound_at_ms > 0
    ):
        return _process_rebound_window(
            state,
            params,
            snap,
            last_price,
            now_ms,
            is_long=is_long,
            extreme=extreme,
        )

    arm_wait = float(params.arm_wait_sec or 0)
    confirm_max_r = confirm_max_retrace_pct(params)

    # —— 关闭武装：同刻全条件；若开 rebound 仍进反弹窗（不绕过）——
    if arm_wait <= 0:
        progress = spike_progress(direction, snap.bar_open, extreme, n)
        need = effective_volume_mult(params, progress)
        if not _volume_hot(params, snap, volume_mult=need):
            return None
        if not _move_ok(params, direction, snap.bar_open, extreme):
            return None
        if not _retrace_ok(
            params,
            direction,
            snap.bar_open,
            extreme,
            last_price,
            max_retrace_override=confirm_max_r,
        ):
            return None
        if not pierced:
            return None
        return _after_confirm(
            state,
            params,
            snap,
            last_price,
            now_ms,
            is_long=is_long,
            extreme=extreme,
        )

    # —— Arm–Confirm ——
    # 深化武装极值
    if state.armed_bar_ts == snap.bar_open_ts and state.armed_extreme is not None:
        if is_long:
            state.armed_extreme = min(float(state.armed_extreme), float(extreme))
        else:
            state.armed_extreme = max(float(state.armed_extreme), float(extreme))

    # 超时作废（记下 progress，供同根更深针再武装）
    if state.armed_bar_ts == snap.bar_open_ts and state.armed_at_ms > 0:
        if (now_ms - state.armed_at_ms) > int(arm_wait * 1000):
            ext_x = (
                float(state.armed_extreme)
                if state.armed_extreme is not None
                else float(extreme)
            )
            state.armed_expired_bar_ts = snap.bar_open_ts
            state.armed_expired_progress = max(
                float(state.armed_expired_progress or 0),
                spike_progress(direction, snap.bar_open, ext_x, n),
            )
            clear_arm(state)
            return None

    # 新武装：刺破 + 最小涨跌幅
    # 作废后若同根仍刺破可再武装（量滞后于价时给量能追上来的机会）；
    # 不再要求 progress 创新高：极值已记入 state.bar_high，量达标即可触发。
    if state.armed_bar_ts != snap.bar_open_ts:
        if not pierced:
            return None
        if not _move_ok(params, direction, snap.bar_open, extreme):
            return None
        progress0 = spike_progress(direction, snap.bar_open, extreme, n)
        need0 = effective_volume_mult(params, progress0)
        vol_hot0 = _volume_hot(params, snap, volume_mult=need0)
        state.armed_bar_ts = snap.bar_open_ts
        state.armed_at_ms = now_ms
        state.armed_extreme = float(extreme)
        state.armed_awaiting_vol = not vol_hot0
        # 再武装后清作废标记（新一轮等待）
        state.armed_expired_bar_ts = None

    ext = float(state.armed_extreme) if state.armed_extreme is not None else float(extreme)
    progress = spike_progress(direction, snap.bar_open, ext, n)
    need = effective_volume_mult(params, progress)
    if not _volume_hot(params, snap, volume_mult=need):
        # 勿在此处把 awaiting_vol 改回 True：若武装时量已够，量抖动不应用 grace 绕过回撤
        return None

    # 量已够：grace 免回撤时仍受 tip_gap 上限约束；
    # 开启反弹时 abort 帽始终生效（避免 grace 把已过放弃线的单送进反弹窗）
    waived = _retrace_waived(state, params, now_ms)
    if not waived or params.rebound_enabled:
        if not _retrace_ok(
            params,
            direction,
            snap.bar_open,
            ext,
            last_price,
            max_retrace_override=confirm_max_r,
        ):
            return None
    if waived:
        max_gap = float(params.arm_grace_max_tip_gap_pct or 0)
        if max_gap > 0:
            gap = tip_gap_pct(snap.bar_open, ext, last_price)
            if gap > max_gap + 1e-12:
                return None

    # 刺破仍须成立（用冻结/深化极值或现价）
    if is_long:
        still = ext <= thr or last_price <= thr
    else:
        still = ext >= thr or last_price >= thr
    if not still:
        return None

    return _after_confirm(
        state,
        params,
        snap,
        last_price,
        now_ms,
        is_long=is_long,
        extreme=ext,
    )


def pierce_vol_view(
    params: WickSpikeParams,
    snap: WickBarSnapshot,
    state: WickSymbolState,
    last_price: float,
    *,
    now_ms: int | None = None,
) -> Optional[PierceVolView]:
    """读取当前是否已刺破、量是否达标（须在 on_tick 更新极值之后调用）。"""
    if last_price <= 0 or snap.bar_open <= 0 or snap.atr <= 0:
        return None
    direction = (params.direction or "").lower()
    n = snap.atr * params.atr_mult
    if n <= 0:
        return None
    if direction == "long":
        extreme = state.bar_low if state.bar_low is not None else last_price
        if state.armed_extreme is not None and state.armed_bar_ts == snap.bar_open_ts:
            extreme = min(float(extreme), float(state.armed_extreme))
        pierced = extreme <= snap.bar_open - n or last_price <= snap.bar_open - n
    elif direction == "short":
        extreme = state.bar_high if state.bar_high is not None else last_price
        if state.armed_extreme is not None and state.armed_bar_ts == snap.bar_open_ts:
            extreme = max(float(extreme), float(state.armed_extreme))
        pierced = extreme >= snap.bar_open + n or last_price >= snap.bar_open + n
    else:
        return None

    progress = spike_progress(direction, snap.bar_open, extreme, n)
    need = effective_volume_mult(params, progress)
    vol_ratio = (snap.vol_now / snap.vol_sma) if snap.vol_sma > 0 else 0.0
    ts = int(now_ms) if now_ms is not None else state.armed_at_ms
    armed = is_arm_active(state, params, snap.bar_open_ts, ts if ts > 0 else state.armed_at_ms)
    arm_age = (ts - state.armed_at_ms) if armed and state.armed_at_ms > 0 else 0
    retrace = wick_retrace_pct(direction, snap.bar_open, extreme, last_price)
    return PierceVolView(
        pierced=bool(pierced),
        vol_hot=_volume_hot(params, snap, volume_mult=need),
        progress=progress,
        need=need,
        vol_ratio=vol_ratio,
        armed=bool(armed),
        arm_age_ms=int(arm_age),
        retrace_pct=float(retrace),
    )

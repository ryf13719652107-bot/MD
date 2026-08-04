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
超时作废后若 progress 创新高可再次武装。
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
_ARM_RETRACE_GRACE_SEC = 3.0
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


def _retrace_ok(
    params: WickSpikeParams,
    direction: str,
    bar_open: float,
    extreme: float,
    last_price: float,
) -> bool:
    max_r = float(params.max_retrace_pct or 0)
    if max_r <= 0:
        return True
    return wick_retrace_pct(direction, bar_open, extreme, last_price) <= max_r + 1e-12


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
) -> WickBarSnapshot:
    """用成交流累计的量/高低补强 K 线快照（解决 K 线 WS 量能滞后）。

    trade_bar_open_ts 若与 snap.bar_open_ts 不一致（换根后尚未有新成交），
    忽略成交聚合，避免上一根巨量/极值污染新根。
    """
    if trade_bar_open_ts is not None and int(trade_bar_open_ts) != int(snap.bar_open_ts):
        return snap
    vol = max(float(snap.vol_now or 0), float(trade_vol or 0))
    hi = float(snap.kline_high or 0)
    lo = float(snap.kline_low or 0)
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


def is_arm_active(
    state: WickSymbolState, params: WickSpikeParams, bar_open_ts: int, now_ms: int
) -> bool:
    """武装窗是否仍有效（供 runner 强制重判）。"""
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
    """开仓未真正执行时回滚本根触发标记，允许同根 K 再次尝试（保留武装）。"""
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

    arm_wait = float(params.arm_wait_sec or 0)

    # —— 关闭武装：旧逻辑（同刻刺破+量+涨跌幅+回撤）——
    if arm_wait <= 0:
        progress = spike_progress(direction, snap.bar_open, extreme, n)
        need = effective_volume_mult(params, progress)
        if not _volume_hot(params, snap, volume_mult=need):
            return None
        if not _move_ok(params, direction, snap.bar_open, extreme):
            return None
        if not _retrace_ok(params, direction, snap.bar_open, extreme, last_price):
            return None
        if not pierced:
            return None
        state.triggered_bar_ts = snap.bar_open_ts
        return Signal.LONG if is_long else Signal.SHORT

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

    # 新武装：刺破 + 最小涨跌幅；作废后仅 progress 创新高可再武装
    if state.armed_bar_ts != snap.bar_open_ts:
        if not pierced:
            return None
        if not _move_ok(params, direction, snap.bar_open, extreme):
            return None
        progress0 = spike_progress(direction, snap.bar_open, extreme, n)
        if state.armed_expired_bar_ts == snap.bar_open_ts:
            if progress0 <= float(state.armed_expired_progress or 0) + 1e-9:
                return None
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

    # 量已够：grace 免回撤时仍受 tip_gap 上限约束
    waived = _retrace_waived(state, params, now_ms)
    if not waived:
        if not _retrace_ok(params, direction, snap.bar_open, ext, last_price):
            return None
    else:
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

    state.triggered_bar_ts = snap.bar_open_ts
    state.armed_awaiting_vol = False
    return Signal.LONG if is_long else Signal.SHORT


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

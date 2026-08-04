"""毫秒接针状态机（纯逻辑，无 I/O）。

流程：先放量(Vol >= SMA×mult) → 用本根极值追认 / 之后触及 open±N → 立刻信号。
N = 上根 ATR × atr_mult。开盘价由调用方传入（须为 K 线官方 open）。

progress 量能放宽（可选，默认开）：
  progress = |极值-开盘| / N；
  progress < start(1.0) 不放宽；
  start→full(1.5) 时 need 从 volume_mult 线性降到 vol_relax_mult(5×)；
  progress ≥ full 时 need = vol_relax_mult。

min_move_pct（默认 3）：本根极值相对开盘的涨跌幅 % 须 ≥ 该值才允许触发；0=关闭。
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


@dataclass
class PierceVolView:
    """刺破/量能快照（供 runner 短窗重试判断）。"""

    pierced: bool
    vol_hot: bool
    progress: float
    need: float
    vol_ratio: float


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
    """进场价相对本根极值的距离（占开盘价 %）。越小越贴针尖。"""
    if bar_open <= 0:
        return 0.0
    return abs(float(entry_px) - float(extreme)) / bar_open * 100.0


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


def near_miss_diag(
    params: WickSpikeParams,
    snap: WickBarSnapshot,
    state: WickSymbolState,
    last_price: float,
) -> Optional[str]:
    """近阈值诊断文案（仅供服务器 logger，勿写入前端策略日志）。

    价格/极值已走过刺破距离的一半，或已刺破且量能接近所需时返回说明。
    """
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
        pierced = extreme >= thr
    elif direction == "long":
        thr = snap.bar_open - n
        extreme = min(lo, last_price)
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
    # 未接近刺破时不因「半量」刷屏（例如大阴线对做空）
    if not pierced and not px_near:
        return None
    if not (vol_near or px_near):
        return None

    return (
        f"dir={direction} px={last_price:.6g} open={snap.bar_open:.6g} "
        f"ext={extreme:.6g} thr={thr:.6g} pierce={pierced} "
        f"atrN={n:.6g} progress={progress:.2f} amp%={move_pct:.2f} "
        f"vol×={vol_ratio:.2f} need×={need:g} vol_hot={vol_hot}"
    )


def release_bar_trigger(state: WickSymbolState) -> None:
    """开仓未真正执行时回滚本根触发标记，允许同根 K 再次尝试。"""
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
    if params.cooldown_sec > 0:
        state.cooldown_until_ms = now_ms + int(params.cooldown_sec * 1000)


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

    # 新 K：用成交价 + K 线 H/L 初始化极值（避免只靠 last 丢掉本根已走出的针）
    if state.bar_open_ts != snap.bar_open_ts:
        state.bar_open_ts = snap.bar_open_ts
        state.bar_low = min(last_price, snap.kline_low) if snap.kline_low > 0 else last_price
        state.bar_high = max(last_price, snap.kline_high) if snap.kline_high > 0 else last_price

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

    progress = spike_progress(direction, snap.bar_open, extreme, n)
    need = effective_volume_mult(params, progress)
    if not _volume_hot(params, snap, volume_mult=need):
        return None
    if not _move_ok(params, direction, snap.bar_open, extreme):
        return None

    if direction == "long":
        threshold = snap.bar_open - n
        if (state.bar_low is not None and state.bar_low <= threshold) or last_price <= threshold:
            state.triggered_bar_ts = snap.bar_open_ts
            return Signal.LONG
    else:
        threshold = snap.bar_open + n
        if (state.bar_high is not None and state.bar_high >= threshold) or last_price >= threshold:
            state.triggered_bar_ts = snap.bar_open_ts
            return Signal.SHORT

    return None


def pierce_vol_view(
    params: WickSpikeParams,
    snap: WickBarSnapshot,
    state: WickSymbolState,
    last_price: float,
) -> Optional[PierceVolView]:
    """读取当前是否已刺破、量是否达标（须在 on_tick 更新极值之后调用）。"""
    if last_price <= 0 or snap.bar_open <= 0 or snap.atr <= 0:
        return None
    direction = (params.direction or "").lower()
    if direction == "long":
        extreme = state.bar_low if state.bar_low is not None else last_price
        n = snap.atr * params.atr_mult
        if n <= 0:
            return None
        pierced = extreme <= snap.bar_open - n or last_price <= snap.bar_open - n
    elif direction == "short":
        extreme = state.bar_high if state.bar_high is not None else last_price
        n = snap.atr * params.atr_mult
        if n <= 0:
            return None
        pierced = extreme >= snap.bar_open + n or last_price >= snap.bar_open + n
    else:
        return None

    progress = spike_progress(direction, snap.bar_open, extreme, n)
    need = effective_volume_mult(params, progress)
    vol_ratio = (snap.vol_now / snap.vol_sma) if snap.vol_sma > 0 else 0.0
    return PierceVolView(
        pierced=bool(pierced),
        vol_hot=_volume_hot(params, snap, volume_mult=need),
        progress=progress,
        need=need,
        vol_ratio=vol_ratio,
    )

"""毫秒接针状态机（纯逻辑，无 I/O）。

流程：先放量(Vol >= SMA×mult) → 用本根极值追认 / 之后触及 open±N → 立刻信号。
N = 上根 ATR × atr_mult。开盘价由调用方传入（须为 K 线官方 open）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .strategy_engine import Signal, calculate_atr


@dataclass
class WickSpikeParams:
    direction: str  # "long" | "short"
    volume_mult: float = 8.0
    atr_mult: float = 5.0
    cooldown_sec: float = 0.0


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


def _volume_hot(params: WickSpikeParams, snap: WickBarSnapshot) -> bool:
    if params.volume_mult <= 0:
        return True
    return snap.vol_now >= snap.vol_sma * params.volume_mult


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

    # 新 K：重置本根极值（保留 cooldown / triggered 跨 bar 由 cooldown 控制）
    if state.bar_open_ts != snap.bar_open_ts:
        state.bar_open_ts = snap.bar_open_ts
        state.bar_low = last_price
        state.bar_high = last_price

    # 极值：成交价 + K 线 H/L 取更极端
    lo = min(last_price, snap.kline_low)
    hi = max(last_price, snap.kline_high)
    state.bar_low = lo if state.bar_low is None else min(state.bar_low, lo)
    state.bar_high = hi if state.bar_high is None else max(state.bar_high, hi)

    if state.triggered_bar_ts == snap.bar_open_ts:
        return None
    if params.cooldown_sec > 0 and now_ms < state.cooldown_until_ms:
        return None

    if not _volume_hot(params, snap):
        return None

    n = snap.atr * params.atr_mult
    if n <= 0:
        return None

    direction = (params.direction or "").lower()
    if direction == "long":
        threshold = snap.bar_open - n
        if (state.bar_low is not None and state.bar_low <= threshold) or last_price <= threshold:
            # 乐观锁定，防止并发重复；失败由调用方 release
            state.triggered_bar_ts = snap.bar_open_ts
            return Signal.LONG
    elif direction == "short":
        threshold = snap.bar_open + n
        if (state.bar_high is not None and state.bar_high >= threshold) or last_price >= threshold:
            state.triggered_bar_ts = snap.bar_open_ts
            return Signal.SHORT

    return None

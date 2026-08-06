from enum import Enum
from typing import Optional


class Signal(Enum):
    LONG = "long"
    SHORT = "short"
    NEUTRAL = "neutral"


def calculate_rsi(klines: list, period: int = 14) -> Optional[float]:
    """Calculate RSI using Wilder's smoothing method."""
    if len(klines) < period + 1:
        return None

    closes = [float(c[4]) for c in klines]  # Index 4 = close price
    gains = []
    losses = []

    for i in range(1, len(closes)):
        diff = closes[i] - closes[i - 1]
        gains.append(diff if diff > 0 else 0)
        losses.append(abs(diff) if diff < 0 else 0)

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0

    # Wilder's smoothing for remaining periods
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period

    if avg_loss == 0:
        return 100.0

    rs = avg_gain / avg_loss
    rsi = 100.0 - (100.0 / (1.0 + rs))
    return rsi


def generate_long_signal(rsi: float, rsi_threshold: float) -> Signal:
    """Generate long signal: RSI below threshold triggers LONG."""
    if rsi < rsi_threshold:
        return Signal.LONG
    return Signal.NEUTRAL


def generate_short_signal(rsi: float, rsi_threshold: float) -> Signal:
    """Generate short signal: RSI above threshold triggers SHORT."""
    if rsi > rsi_threshold:
        return Signal.SHORT
    return Signal.NEUTRAL


def generate_signal(rsi: float, direction: str, rsi_threshold: float) -> Signal:
    """Generate signal based on direction."""
    if direction == "long":
        return generate_long_signal(rsi, rsi_threshold)
    elif direction == "short":
        return generate_short_signal(rsi, rsi_threshold)
    return Signal.NEUTRAL


# ── WaveTrend ──────────────────────────────────────────────

def hlc3(high: float, low: float, close: float) -> float:
    return (high + low + close) / 3.0


def ema(data: list[float], period: int) -> list[float]:
    """Exponential Moving Average."""
    if len(data) < period:
        return []
    k = 2.0 / (period + 1)
    result = [sum(data[:period]) / period]
    for v in data[period:]:
        result.append(v * k + result[-1] * (1 - k))
    return result


def sma(data: list[float], period: int) -> list[float]:
    """Simple Moving Average."""
    if len(data) < period:
        return []
    result = []
    for i in range(period - 1, len(data)):
        result.append(sum(data[i - period + 1:i + 1]) / period)
    return result


def calculate_wavetrend(
    klines: list,
    channel_length: int = 10,
    average_length: int = 21,
) -> Optional[dict]:
    """WaveTrend by LazyBear — Pine Script v5 implementation.

    esa  = ta.ema(hlc3, channel_len)
    d    = ta.ema(math.abs(hlc3 - esa), channel_len)
    ci   = (hlc3 - esa) / (0.015 * d)
    wt1  = ta.ema(ci, avg_len)
    wt2  = ta.sma(wt1, 4)

    Returns dict with: wt1, wt2, cross_above, cross_below
    Cross signals only valid when wt1 is in overbought/oversold zone.
    """
    if len(klines) < channel_length + average_length + 4:
        return None

    highs = [float(c[2]) for c in klines]
    lows = [float(c[3]) for c in klines]
    closes = [float(c[4]) for c in klines]
    hlc3s = [hlc3(h, l, c) for h, l, c in zip(highs, lows, closes)]

    # 1. esa = EMA(HLC3, channel_length)
    esa = ema(hlc3s, channel_length)
    if not esa:
        return None
    offset_esa = len(hlc3s) - len(esa)

    # 2. d = EMA(|HLC3 - ESA|, channel_length)
    dev = [abs(hlc3s[i + offset_esa] - esa[i]) for i in range(len(esa))]
    d = ema(dev, channel_length)
    if not d:
        return None
    offset_d = len(esa) - len(d)

    # 3. ci = (HLC3 - ESA) / (0.015 * d)  — Pine Script exact formula
    ci = []
    for i in range(len(d)):
        esa_idx = i + offset_d
        hlc3_idx = esa_idx + offset_esa
        if d[i] != 0:
            ci.append((hlc3s[hlc3_idx] - esa[esa_idx]) / (0.015 * d[i]))
        else:
            ci.append(0.0)

    # 4. wt1 = EMA(CI, average_length)
    wt1 = ema(ci, average_length)
    if not wt1:
        return None

    # 5. wt2 = SMA(wt1, 4)  — Pine Script exact: ta.sma(wt1, 4)
    wt2 = sma(wt1, 4)
    if not wt2:
        return None

    # Align wt1 and wt2
    offset_final = len(wt1) - len(wt2)
    wt1_aligned = wt1[offset_final:]
    if len(wt1_aligned) != len(wt2):
        min_len = min(len(wt1_aligned), len(wt2))
        wt1_aligned = wt1_aligned[-min_len:]
        wt2 = wt2[-min_len:]

    # Cross detection
    cross_above = False
    cross_below = False
    if len(wt1_aligned) >= 2:
        cross_above = wt1_aligned[-2] <= wt2[-2] and wt1_aligned[-1] > wt2[-1]
        cross_below = wt1_aligned[-2] >= wt2[-2] and wt1_aligned[-1] < wt2[-1]

    wt1_last = wt1_aligned[-1]
    wt2_last = wt2[-1]

    return {
        "wt1": wt1_last,
        "wt2": wt2_last,
        "cross_above": cross_above,
        "cross_below": cross_below,
    }


def generate_wt_signal(wt: dict, direction: str, os_level: float = -60.0, ob_level: float = 60.0) -> Signal:
    """WaveTrend signal matching Pine Script:
    long  = crossover(wt1, wt2) AND wt1 < os_level
    short = crossunder(wt1, wt2) AND wt1 > ob_level
    """
    if direction == "long":
        if wt["cross_above"] and wt["wt1"] < os_level:
            return Signal.LONG
    elif direction == "short":
        if wt["cross_below"] and wt["wt1"] > ob_level:
            return Signal.SHORT
    return Signal.NEUTRAL


# ── Supertrend (Pine ta.supertrend) ────────────────────────

def true_range(highs: list[float], lows: list[float], closes: list[float]) -> list[float]:
    """True Range series; first bar uses high-low only."""
    if not highs:
        return []
    tr = [highs[0] - lows[0]]
    for i in range(1, len(highs)):
        tr.append(
            max(
                highs[i] - lows[i],
                abs(highs[i] - closes[i - 1]),
                abs(lows[i] - closes[i - 1]),
            )
        )
    return tr


def rma(data: list[float], period: int) -> list[float]:
    """Wilder's RMA (same as Pine ta.rma / ta.atr smoothing)."""
    if len(data) < period:
        return []
    alpha = 1.0 / period
    result = [sum(data[:period]) / period]
    for v in data[period:]:
        result.append(alpha * v + (1.0 - alpha) * result[-1])
    return result


def calculate_atr(klines: list, period: int = 14) -> Optional[list[float]]:
    """ATR via Wilder RMA（与 Pine ta.atr / 币安默认 ATR 同族）。

    注意：RMA 有路径依赖，输入 K 线历史越短，末值越容易与交易所长图不一致。
    接针侧应尽量用足够长的已收盘序列（见 wick_spike_runner._KLINE_ATR_BARS）。
    """
    if len(klines) < period + 1:
        return None
    highs = [float(c[2]) for c in klines]
    lows = [float(c[3]) for c in klines]
    closes = [float(c[4]) for c in klines]
    tr = true_range(highs, lows, closes)
    atr = rma(tr, period)
    return atr if atr else None


def calculate_supertrend(
    klines: list,
    atr_period: int = 10,
    factor: float = 3.0,
) -> Optional[dict]:
    """Pine Script ta.supertrend(factor, atrPeriod) — last bar only.

    direction: -1 = bullish (uptrend), 1 = bearish (downtrend)
    Returns: {direction, value, bullish}
    """
    if len(klines) < atr_period + 2:
        return None

    highs = [float(c[2]) for c in klines]
    lows = [float(c[3]) for c in klines]
    closes = [float(c[4]) for c in klines]
    n = len(closes)

    atr_vals = calculate_atr(klines, atr_period)
    if not atr_vals:
        return None
    # ATR series starts at index (atr_period - 1) of TR/klines
    atr_offset = n - len(atr_vals)

    direction = 1
    super_trend = 0.0
    prev_upper = None
    prev_lower = None
    prev_st = None

    for i in range(atr_offset, n):
        atr_i = atr_vals[i - atr_offset]
        src = (highs[i] + lows[i]) / 2.0
        basic_upper = src + factor * atr_i
        basic_lower = src - factor * atr_i

        if prev_lower is None or prev_upper is None:
            # First ATR bar: Pine sets direction := 1 when na(atr[1])
            upper = basic_upper
            lower = basic_lower
            direction = 1
            super_trend = upper
        else:
            # Trail bands (Pine nz + conditional)
            close_prev = closes[i - 1]
            lower = (
                basic_lower
                if (basic_lower > prev_lower or close_prev < prev_lower)
                else prev_lower
            )
            upper = (
                basic_upper
                if (basic_upper < prev_upper or close_prev > prev_upper)
                else prev_upper
            )

            if prev_st == prev_upper:
                direction = -1 if closes[i] > upper else 1
            else:
                direction = 1 if closes[i] < lower else -1
            super_trend = lower if direction == -1 else upper

        prev_upper = upper
        prev_lower = lower
        prev_st = super_trend

    return {
        "direction": direction,
        "value": super_trend,
        "bullish": direction < 0,
    }


def generate_trend_wt_signal(
    wt: dict,
    direction: str,
    st_bull_tf1: bool,
    st_bull_tf2: bool,
    os_level: float = -60.0,
    ob_level: float = 60.0,
) -> Signal:
    """Trend WT: WaveTrend entry + both Supertrend TFs agree with direction.

    long  = WT long  AND ST1 bullish AND ST2 bullish
    short = WT short AND ST1 bearish AND ST2 bearish
    """
    base = generate_wt_signal(wt, direction, os_level, ob_level)
    if base == Signal.LONG:
        if st_bull_tf1 and st_bull_tf2:
            return Signal.LONG
    elif base == Signal.SHORT:
        if (not st_bull_tf1) and (not st_bull_tf2):
            return Signal.SHORT
    return Signal.NEUTRAL

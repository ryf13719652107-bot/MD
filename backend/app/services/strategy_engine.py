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
    ob_level: float = 53.0,
    os_level: float = -53.0,
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
        "wt1": round(wt1_last, 2),
        "wt2": round(wt2_last, 2),
        "cross_above": cross_above,
        "cross_below": cross_below,
    }


def generate_wt_signal(wt: dict, direction: str) -> Signal:
    """WaveTrend signal matching Pine Script:
    long  = crossover(wt1, wt2) AND wt1 < os_level2 (-53)
    short = crossunder(wt1, wt2) AND wt1 > ob_level2 (53)
    """
    if direction == "long":
        if wt["cross_above"] and wt["wt1"] < -53:
            return Signal.LONG
    elif direction == "short":
        if wt["cross_below"] and wt["wt1"] > 53:
            return Signal.SHORT
    return Signal.NEUTRAL

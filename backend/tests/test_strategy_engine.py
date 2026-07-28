import pytest
from app.services.strategy_engine import calculate_rsi, generate_signal, Signal, generate_long_signal, generate_short_signal


def make_kline(close: float, time: int = 0) -> list:
    """Helper to create a minimal OHLCV kline."""
    return [time * 60000, close, close, close, close, 0]


def test_calculate_rsi_insufficient_data():
    """RSI should return None with insufficient klines."""
    klines = [make_kline(100, i) for i in range(10)]
    assert calculate_rsi(klines, 14) is None


def test_calculate_rsi_all_up():
    """RSI should be 100 when all moves are up."""
    klines = []
    price = 100.0
    for i in range(20):
        price += 1.0
        klines.append(make_kline(price, i))
    rsi = calculate_rsi(klines, 14)
    assert rsi is not None
    assert rsi > 90


def test_calculate_rsi_all_down():
    """RSI should be near 0 when all moves are down."""
    klines = []
    price = 100.0
    for i in range(20):
        price -= 1.0
        klines.append(make_kline(price, i))
    rsi = calculate_rsi(klines, 14)
    assert rsi is not None
    assert rsi < 10


def test_calculate_rsi_flat():
    """RSI should be near 50 when prices are flat."""
    klines = [make_kline(100, i) for i in range(20)]
    rsi = calculate_rsi(klines, 14)
    assert rsi is not None
    assert 45 <= rsi <= 55


def test_generate_long_signal_triggers_below_threshold():
    assert generate_long_signal(25, 30) == Signal.LONG


def test_generate_long_signal_neutral_above_threshold():
    assert generate_long_signal(35, 30) == Signal.NEUTRAL


def test_generate_short_signal_triggers_above_threshold():
    assert generate_short_signal(80, 75) == Signal.SHORT


def test_generate_short_signal_neutral_below_threshold():
    assert generate_short_signal(70, 75) == Signal.NEUTRAL


def test_generate_signal_respects_direction():
    assert generate_signal(25, "long", 30) == Signal.LONG
    assert generate_signal(80, "short", 75) == Signal.SHORT
    assert generate_signal(50, "long", 30) == Signal.NEUTRAL


# ── Supertrend / Trend WT ──────────────────────────────────

from app.services.strategy_engine import (
    calculate_supertrend,
    generate_wt_signal,
    generate_trend_wt_signal,
)


def make_ohlc(high: float, low: float, close: float, time: int = 0) -> list:
    return [time * 60000, close, high, low, close, 0]


def test_calculate_supertrend_insufficient_data():
    klines = [make_ohlc(110, 90, 100, i) for i in range(5)]
    assert calculate_supertrend(klines, atr_period=10, factor=3.0) is None


def test_calculate_supertrend_uptrend_on_rising_prices():
    """Strong sustained uptrend should yield bullish Supertrend."""
    klines = []
    price = 100.0
    for i in range(80):
        price += 1.5
        klines.append(make_ohlc(price + 0.5, price - 0.5, price, i))
    st = calculate_supertrend(klines, atr_period=10, factor=3.0)
    assert st is not None
    assert st["bullish"] is True
    assert st["direction"] == -1


def test_calculate_supertrend_downtrend_on_falling_prices():
    klines = []
    price = 200.0
    for i in range(80):
        price -= 1.5
        klines.append(make_ohlc(price + 0.5, price - 0.5, price, i))
    st = calculate_supertrend(klines, atr_period=10, factor=3.0)
    assert st is not None
    assert st["bullish"] is False
    assert st["direction"] == 1


def test_generate_trend_wt_long_requires_both_st_bullish():
    wt = {"wt1": -65.0, "wt2": -70.0, "cross_above": True, "cross_below": False}
    assert generate_wt_signal(wt, "long", -60.0, 60.0) == Signal.LONG
    assert generate_trend_wt_signal(wt, "long", True, True, -60.0, 60.0) == Signal.LONG
    assert generate_trend_wt_signal(wt, "long", True, False, -60.0, 60.0) == Signal.NEUTRAL
    assert generate_trend_wt_signal(wt, "long", False, True, -60.0, 60.0) == Signal.NEUTRAL


def test_generate_trend_wt_short_requires_both_st_bearish():
    wt = {"wt1": 65.0, "wt2": 70.0, "cross_above": False, "cross_below": True}
    assert generate_wt_signal(wt, "short", -60.0, 60.0) == Signal.SHORT
    assert generate_trend_wt_signal(wt, "short", False, False, -60.0, 60.0) == Signal.SHORT
    assert generate_trend_wt_signal(wt, "short", True, False, -60.0, 60.0) == Signal.NEUTRAL
    assert generate_trend_wt_signal(wt, "short", False, True, -60.0, 60.0) == Signal.NEUTRAL

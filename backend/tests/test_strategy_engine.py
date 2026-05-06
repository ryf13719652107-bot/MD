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

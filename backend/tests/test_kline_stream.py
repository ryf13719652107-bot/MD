"""kline_stream 辅助函数单测（无网络）。"""
import pytest

from app.services.kline_stream import (
    _buffer_stale_for_timeframe,
    _normalize_candles,
    _timeframe_ms,
)


def test_timeframe_ms():
    assert _timeframe_ms("1m") == 60_000
    assert _timeframe_ms("15m") == 900_000
    assert _timeframe_ms("unknown") == 60_000


def test_normalize_candles_nested_and_flat():
    nested = [[1000, 1, 2, 3, 4, 5], [2000, 1, 2, 3, 4, 6]]
    assert _normalize_candles(nested) == nested
    flat = [3000, 1.0, 2.0, 3.0, 4.0, 7.0]
    assert _normalize_candles(flat) == [[3000, 1.0, 2.0, 3.0, 4.0, 7.0]]
    assert _normalize_candles([]) == []
    assert _normalize_candles(None) == []


def test_buffer_stale():
    import time

    now_ms = int(time.time() * 1000)
    tf_ms = 60_000
    cur_open = (now_ms // tf_ms) * tf_ms
    buf_ok = [
        [cur_open - tf_ms, 1, 1, 1, 1, 1],
        [cur_open, 1, 1, 1, 1, 1],
    ]
    assert not _buffer_stale_for_timeframe(buf_ok, "1m")

    old_open = now_ms - 400_000
    assert _buffer_stale_for_timeframe([[old_open, 1, 1, 1, 1, 1]], "1m")


@pytest.mark.asyncio
async def test_refresh_forming_replaces_closed_bar_ohlc():
    """已收盘 K 不得只增不减，否则 ATR 会被盘中毛刺锁高。"""
    from types import SimpleNamespace

    from app.services.kline_stream import KlineStreamManager

    mgr = KlineStreamManager()
    client = SimpleNamespace(exchange_id="binance")
    key = mgr._key(client, "AKEUSDT", "1m")
    t0, t1 = 1_000_000, 1_060_000
    mgr._buffers[key] = [
        [t0, 1.0, 1.5, 0.9, 1.1, 100.0],  # 已收盘：内存高点被毛刺抬到 1.5
        [t1, 1.1, 1.2, 1.0, 1.15, 50.0],  # 本根
    ]

    async def fake_fetch(symbol, timeframe, limit=2):
        return [
            [t0, 1.0, 1.2, 0.95, 1.1, 90.0],   # REST 官方高点更低
            [t1, 1.1, 1.25, 0.99, 1.18, 60.0],  # 本根 REST 更高
        ]

    client.fetch_klines = fake_fetch
    await mgr.refresh_forming(client, "AKEUSDT", "1m", limit=2)
    buf = mgr._buffers[key]
    assert float(buf[0][2]) == pytest.approx(1.2)   # 已收盘采用 REST，可回落
    assert float(buf[0][3]) == pytest.approx(0.95)
    assert float(buf[1][2]) == pytest.approx(1.25)  # 本根只增：max(1.2, 1.25)
    assert float(buf[1][3]) == pytest.approx(0.99)  # 本根只减：min(1.0, 0.99)


@pytest.mark.asyncio
async def test_refresh_forming_keeps_inflated_forming_high():
    from types import SimpleNamespace

    from app.services.kline_stream import KlineStreamManager

    mgr = KlineStreamManager()
    client = SimpleNamespace(exchange_id="binance")
    key = mgr._key(client, "AKEUSDT", "1m")
    t0, t1 = 1_000_000, 1_060_000
    mgr._buffers[key] = [
        [t0, 1.0, 1.1, 1.0, 1.05, 10.0],
        [t1, 1.1, 1.4, 1.0, 1.2, 50.0],  # WS 已见 1.4
    ]

    async def fake_fetch(symbol, timeframe, limit=2):
        return [
            [t0, 1.0, 1.1, 1.0, 1.05, 10.0],
            [t1, 1.1, 1.3, 1.05, 1.25, 40.0],  # REST 略旧，高点更低
        ]

    client.fetch_klines = fake_fetch
    await mgr.refresh_forming(client, "AKEUSDT", "1m", limit=2)
    buf = mgr._buffers[key]
    assert float(buf[1][2]) == pytest.approx(1.4)  # 本根不被 REST 旧值压回
    assert float(buf[1][3]) == pytest.approx(1.0)  # low 只减不升：仍 1.0

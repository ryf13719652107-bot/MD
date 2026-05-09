"""kline_stream 辅助函数单测（无网络）。"""
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

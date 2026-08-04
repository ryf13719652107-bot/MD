"""价流成交唤醒事件。"""

import asyncio

import pytest

from app.services.price_stream import PriceStreamManager


@pytest.mark.asyncio
async def test_apply_trade_sets_wake_event():
    m = PriceStreamManager()
    ev = m.subscribe_wake("wick:1")
    assert not ev.is_set()
    m._apply_trade("BTCUSDT", 100.0, 1.0, 1_700_000_000_000)
    assert ev.is_set()
    ev.clear()
    m._apply_trade("BTCUSDT", 101.0, 0.5, 1_700_000_000_100)
    assert ev.is_set()
    m.unsubscribe_wake("wick:1")


@pytest.mark.asyncio
async def test_clear_wanted_unsubscribes_wake():
    m = PriceStreamManager()
    m.subscribe_wake("wick:9")
    await m.clear_wanted("wick:9")
    assert "wick:9" not in m._wake_events

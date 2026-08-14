"""price_stream 多策略并集与 per-symbol bar 对齐。"""

import pytest

from app.services.price_stream import PriceStreamManager


@pytest.mark.asyncio
async def test_set_wanted_union_across_owners():
    m = PriceStreamManager()
    try:
        await m.set_wanted(None, {"BTCUSDT", "ETHUSDT"}, timeframe="1m", owner="wick:1")
        await m.set_wanted(None, {"ETHUSDT", "SOLUSDT"}, timeframe="1m", owner="wick:2")
        assert m._wanted == {"BTCUSDT", "ETHUSDT", "SOLUSDT"}

        await m.clear_wanted("wick:1")
        assert m._wanted == {"ETHUSDT", "SOLUSDT"}
        assert "BTCUSDT" not in m._wanted

        await m.clear_wanted("wick:2")
        assert m._wanted == set()
    finally:
        await m.shutdown()


@pytest.mark.asyncio
async def test_set_wanted_does_not_clobber_other_owner():
    m = PriceStreamManager()
    try:
        await m.set_wanted(None, {"AAAUSDT"}, timeframe="1m", owner="wick:10")
        await m.set_wanted(None, {"BBBUSDT"}, timeframe="5m", owner="wick:12")
        assert "AAAUSDT" in m._wanted
        assert "BBBUSDT" in m._wanted
        assert m._tf_ms_by_sym["AAAUSDT"] == 60_000
        assert m._tf_ms_by_sym["BBBUSDT"] == 300_000
    finally:
        await m.shutdown()


def test_apply_trade_resets_bar_on_boundary():
    m = PriceStreamManager()
    m._tf_ms_by_sym["X"] = 60_000
    m._apply_trade("X", 1.0, 10.0, 60_000)  # bar 60000
    assert m.bar_volume("X") == 10.0
    m._apply_trade("X", 1.1, 5.0, 60_500)
    assert m.bar_volume("X") == 15.0
    m._apply_trade("X", 1.2, 3.0, 120_000)  # new bar
    assert m.bar_open_ms("X") == 120_000
    assert m.bar_volume("X") == 3.0


def test_ratchet_bar_from_kline_raises_tip():
    m = PriceStreamManager()
    m._tf_ms_by_sym["ACEUSDT"] = 60_000
    m._apply_trade("ACEUSDT", 0.33, 1.0, 60_100)
    assert m.bar_high("ACEUSDT") == 0.33
    # REST 补到更高 tip
    m.ratchet_bar_from_kline(
        "ACEUSDT", bar_open_ts=60_000, high=0.339, low=0.32, volume=50.0
    )
    assert m.bar_high("ACEUSDT") == 0.339
    assert m.bar_low("ACEUSDT") == 0.32
    assert m.bar_volume("ACEUSDT") == 50.0
    # 错根不污染
    m.ratchet_bar_from_kline(
        "ACEUSDT", bar_open_ts=120_000, high=0.5, low=0.1, volume=999.0
    )
    assert m.bar_high("ACEUSDT") == 0.339


def test_take_resync_needed_keeps_other_symbols():
    m = PriceStreamManager()
    m.note_resync_needed("AAAUSDT")
    m.note_resync_needed("BBBUSDT")
    m.note_resync_needed("CCCUSDT")
    taken = m.take_resync_needed({"AAAUSDT", "XXXUSDT"})
    assert taken == {"AAAUSDT"}
    # 其它策略池币仍在队列
    left = m.consume_resync_needed()
    assert left == {"BBBUSDT", "CCCUSDT"}

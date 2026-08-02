"""毫秒接针状态机单元测试。"""

from app.services.strategy_engine import Signal
from app.services.wick_spike_engine import (
    WickBarSnapshot,
    WickSpikeParams,
    WickSymbolState,
    enrich_snap_with_trades,
    mark_bar_triggered,
    near_miss_diag,
    on_tick,
    release_bar_trigger,
)


def _snap(
    *,
    open_=100.0,
    atr=1.0,
    vol_now=80.0,
    vol_sma=10.0,
    high=101.0,
    low=99.0,
    ts=1_000_000,
) -> WickBarSnapshot:
    return WickBarSnapshot(
        bar_open_ts=ts,
        bar_open=open_,
        atr=atr,
        vol_now=vol_now,
        vol_sma=vol_sma,
        kline_high=high,
        kline_low=low,
    )


def test_no_volume_no_trigger_even_if_pierced():
    state = WickSymbolState()
    params = WickSpikeParams(direction="long", volume_mult=8.0, atr_mult=5.0)
    # vol only 2x SMA — not hot; price already deep
    snap = _snap(vol_now=20.0, vol_sma=10.0, low=90.0)
    assert on_tick(state, params, snap, last_price=90.0, now_ms=1) is None


def test_volume_then_extreme_retroactive_long():
    """价先刺穿、量后到 — 放量瞬间用 bar_low 追认。"""
    state = WickSymbolState()
    params = WickSpikeParams(direction="long", volume_mult=8.0, atr_mult=5.0)
    # First: no volume, track extreme (open-5*ATR = 95)
    snap_cold = _snap(vol_now=10.0, vol_sma=10.0, low=94.0)
    assert on_tick(state, params, snap_cold, last_price=94.0, now_ms=1) is None
    assert state.bar_low == 94.0

    # Volume becomes 8x; extreme already pierced
    snap_hot = _snap(vol_now=80.0, vol_sma=10.0, low=94.0)
    sig = on_tick(state, params, snap_hot, last_price=96.0, now_ms=2)
    assert sig == Signal.LONG


def test_volume_then_later_touch_long():
    state = WickSymbolState()
    params = WickSpikeParams(direction="long", volume_mult=8.0, atr_mult=5.0)
    snap = _snap(vol_now=80.0, vol_sma=10.0, low=99.0, high=101.0)
    assert on_tick(state, params, snap, last_price=99.0, now_ms=1) is None
    sig = on_tick(state, params, snap, last_price=94.5, now_ms=2)
    assert sig == Signal.LONG


def test_same_bar_only_once():
    state = WickSymbolState()
    params = WickSpikeParams(direction="long", volume_mult=8.0, atr_mult=5.0)
    snap = _snap(vol_now=80.0, vol_sma=10.0, low=90.0)
    assert on_tick(state, params, snap, last_price=90.0, now_ms=1) == Signal.LONG
    assert on_tick(state, params, snap, last_price=89.0, now_ms=2) is None


def test_other_bar_can_trigger_again_with_cooldown_zero():
    state = WickSymbolState()
    params = WickSpikeParams(direction="long", volume_mult=8.0, atr_mult=5.0, cooldown_sec=0)
    snap1 = _snap(ts=1, vol_now=80.0, vol_sma=10.0, low=90.0)
    assert on_tick(state, params, snap1, last_price=90.0, now_ms=1) == Signal.LONG
    snap2 = _snap(ts=2, vol_now=80.0, vol_sma=10.0, low=90.0)
    assert on_tick(state, params, snap2, last_price=90.0, now_ms=100) == Signal.LONG


def test_short_symmetric():
    state = WickSymbolState()
    params = WickSpikeParams(direction="short", volume_mult=8.0, atr_mult=5.0)
    # open+5*ATR = 105
    snap = _snap(vol_now=80.0, vol_sma=10.0, high=106.0, low=100.0)
    assert on_tick(state, params, snap, last_price=106.0, now_ms=1) == Signal.SHORT


def test_cooldown_blocks_next_bar():
    state = WickSymbolState()
    params = WickSpikeParams(direction="long", volume_mult=8.0, atr_mult=5.0, cooldown_sec=60)
    snap1 = _snap(ts=1, vol_now=80.0, vol_sma=10.0, low=90.0)
    assert on_tick(state, params, snap1, last_price=90.0, now_ms=1_000) == Signal.LONG
    mark_bar_triggered(state, params, snap1.bar_open_ts, now_ms=1_000)
    snap2 = _snap(ts=2, vol_now=80.0, vol_sma=10.0, low=90.0)
    assert on_tick(state, params, snap2, last_price=90.0, now_ms=30_000) is None
    assert on_tick(state, params, snap2, last_price=90.0, now_ms=70_000) == Signal.LONG


def test_release_allows_retry_same_bar():
    state = WickSymbolState()
    params = WickSpikeParams(direction="long", volume_mult=8.0, atr_mult=5.0)
    snap = _snap(vol_now=80.0, vol_sma=10.0, low=90.0)
    assert on_tick(state, params, snap, last_price=90.0, now_ms=1) == Signal.LONG
    assert on_tick(state, params, snap, last_price=90.0, now_ms=2) is None
    release_bar_trigger(state)
    assert on_tick(state, params, snap, last_price=90.0, now_ms=3) == Signal.LONG


def test_new_bar_seeds_extremes_from_kline():
    """换 K 时用 K 线高低初始化，避免只靠 last_price 丢掉本根已走出的针。"""
    state = WickSymbolState()
    params = WickSpikeParams(direction="short", volume_mult=8.0, atr_mult=5.0)
    snap = _snap(ts=1, vol_now=80.0, vol_sma=10.0, open_=100.0, high=106.0, low=99.0)
    assert on_tick(state, params, snap, last_price=101.0, now_ms=1) == Signal.SHORT
    assert state.bar_high == 106.0


def test_near_miss_diag_volume_half():
    state = WickSymbolState()
    params = WickSpikeParams(direction="short", volume_mult=8.0, atr_mult=5.0)
    # vol 4x = half of need 8x; price not pierced
    snap = _snap(vol_now=40.0, vol_sma=10.0, open_=100.0, high=101.0, low=99.0)
    on_tick(state, params, snap, last_price=101.0, now_ms=1)
    diag = near_miss_diag(params, snap, state, 101.0)
    assert diag is not None
    assert "vol_hot=False" in diag


def test_near_miss_diag_silent_when_far():
    state = WickSymbolState()
    params = WickSpikeParams(direction="short", volume_mult=8.0, atr_mult=5.0)
    snap = _snap(vol_now=10.0, vol_sma=10.0, open_=100.0, high=100.5, low=99.5)
    on_tick(state, params, snap, last_price=100.2, now_ms=1)
    assert near_miss_diag(params, snap, state, 100.2) is None


def test_enrich_snap_prefers_trade_volume_and_high():
    snap = _snap(vol_now=10.0, vol_sma=10.0, high=101.0, low=99.0)
    enriched = enrich_snap_with_trades(
        snap, trade_vol=80.0, trade_high=106.0, trade_low=98.5
    )
    assert enriched.vol_now == 80.0
    assert enriched.kline_high == 106.0
    assert enriched.kline_low == 98.5
    # 成交流量够热 + 刺破 → 立刻空头
    state = WickSymbolState()
    params = WickSpikeParams(direction="short", volume_mult=8.0, atr_mult=5.0)
    assert on_tick(state, params, enriched, last_price=102.0, now_ms=1) == Signal.SHORT

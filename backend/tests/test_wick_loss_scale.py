"""接针止损后再开（默认关）、同币种连亏加倍。"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.services.strategy_engine import Signal
from app.services.wick_loss_scale import (
    _norm_trade_symbol,
    consecutive_stop_loss_streak,
    infer_flat_close_reason,
    same_open_fill,
    wick_loss_scale_mult,
    wick_loss_scale_times_used,
)
from app.services.wick_spike_engine import (
    WickBarSnapshot,
    WickSpikeParams,
    WickSymbolState,
    on_tick,
    unlock_after_stop_loss,
)
from app.services.wick_spike_runner import WickSpikeRunner


def test_loss_scale_mult_powers_of_two_and_cap():
    assert wick_loss_scale_mult(0, 8) == 1.0
    assert wick_loss_scale_mult(1, 8) == 2.0
    assert wick_loss_scale_mult(2, 8) == 4.0
    assert wick_loss_scale_mult(3, 8) == 8.0
    assert wick_loss_scale_mult(4, 8) == 8.0
    assert wick_loss_scale_mult(4, 16) == 16.0
    assert wick_loss_scale_mult(-1, 8) == 1.0
    assert wick_loss_scale_mult(2, 8, 1.5) == pytest.approx(2.25)
    assert wick_loss_scale_mult(2, 8, 3) == 8.0
    assert wick_loss_scale_mult(1, 8, 3) == 3.0


def test_loss_scale_mult_max_times_caps_streak():
    assert wick_loss_scale_times_used(3, 2) == 2
    assert wick_loss_scale_mult(3, 8, 2, max_times=2) == 4.0
    assert wick_loss_scale_mult(5, 8, 2, max_times=2) == 4.0
    assert wick_loss_scale_mult(1, 8, 2, max_times=2) == 2.0
    assert wick_loss_scale_mult(3, 8, 2, max_times=None) == 8.0


def test_same_open_fill_rejects_different_order_ids():
    assert same_open_fill("111", "111") is True
    assert same_open_fill("111", "222") is False
    assert same_open_fill("", "222") is True
    assert same_open_fill("111", "") is True
    assert same_open_fill(None, "222") is True


def test_infer_flat_close_reason_by_pnl():
    assert infer_flat_close_reason("short", 0.545, 0.537) == "take_profit"
    assert infer_flat_close_reason("short", 0.541, 0.548) == "stop_loss"
    assert infer_flat_close_reason("long", 100, 102) == "take_profit"
    assert infer_flat_close_reason("long", 100, 99) == "stop_loss"
    assert infer_flat_close_reason("short", 0, 1) == "sync"


def test_norm_trade_symbol_matches_exchange_formats():
    assert _norm_trade_symbol("NIL/USDT:USDT") == "NILUSDT"
    assert _norm_trade_symbol("NIL/USDT") == "NILUSDT"
    assert _norm_trade_symbol("nil_usdt") == "NILUSDT"
    assert _norm_trade_symbol("NILUSDT") == "NILUSDT"


def test_consecutive_stop_loss_streak_resets_on_tp():
    assert consecutive_stop_loss_streak(["stop_loss", "stop_loss", "take_profit"]) == 2
    assert consecutive_stop_loss_streak(["take_profit", "stop_loss"]) == 0
    assert consecutive_stop_loss_streak(["stop_loss"]) == 1
    assert consecutive_stop_loss_streak([]) == 0


def _snap(*, ts=1_000_000, low=90.0, vol_now=80.0) -> WickBarSnapshot:
    return WickBarSnapshot(
        bar_open_ts=ts,
        bar_open=100.0,
        atr=1.0,
        vol_now=vol_now,
        vol_sma=10.0,
        kline_high=101.0,
        kline_low=low,
    )


def test_unlock_after_stop_loss_allows_same_bar_rearm():
    state = WickSymbolState()
    params = WickSpikeParams(direction="long", volume_mult=8.0, atr_mult=5.0)
    snap = _snap()
    assert on_tick(state, params, snap, last_price=90.0, now_ms=1) == Signal.LONG
    assert on_tick(state, params, snap, last_price=89.0, now_ms=2) is None
    unlock_after_stop_loss(state)
    assert on_tick(state, params, snap, last_price=89.0, now_ms=3) == Signal.LONG


@pytest.mark.asyncio
async def test_runner_unlock_after_sl_uses_registered_state():
    runner = WickSpikeRunner()
    st = WickSymbolState()
    st.triggered_bar_ts = 123
    runner._symbol_states[7] = {"BTCUSDT": st}
    assert runner.unlock_after_sl(7, "BTC/USDT:USDT") is True
    assert st.triggered_bar_ts is None


@pytest.mark.asyncio
async def test_apply_wick_close_hooks_unlocks_only_when_switch_on():
    runner = WickSpikeRunner()
    st = WickSymbolState()
    st.triggered_bar_ts = 99
    runner._symbol_states[4] = {"NILUSDT": st}
    await runner.apply_wick_close_hooks(
        object(),
        SimpleNamespace(
            id=4,
            signal_source="wick_spike",
            wick_loss_scale_enabled=False,
            wick_reopen_after_sl_enabled=False,
        ),
        "NILUSDT",
        "stop_loss",
    )
    assert st.triggered_bar_ts == 99
    await runner.apply_wick_close_hooks(
        object(),
        SimpleNamespace(
            id=4,
            signal_source="wick_spike",
            wick_loss_scale_enabled=False,
            wick_reopen_after_sl_enabled=True,
        ),
        "NILUSDT",
        "stop_loss",
    )
    assert st.triggered_bar_ts is None


@pytest.mark.asyncio
async def test_loss_scale_qty_uses_cached_streak():
    runner = WickSpikeRunner()
    runner.set_loss_streak(3, "NILUSDT", 2)
    qty, streak, mult = await runner._loss_scale_qty(
        SimpleNamespace(
            id=3,
            wick_loss_scale_enabled=True,
            wick_loss_scale_base=2,
            wick_loss_scale_max_mult=8,
        ),
        "NILUSDT",
        10.0,
    )
    assert streak == 2
    assert mult == 4.0
    assert qty == pytest.approx(40.0)


@pytest.mark.asyncio
async def test_loss_scale_qty_uses_custom_base():
    runner = WickSpikeRunner()
    runner.set_loss_streak(3, "NILUSDT", 2)
    qty, streak, mult = await runner._loss_scale_qty(
        SimpleNamespace(
            id=3,
            wick_loss_scale_enabled=True,
            wick_loss_scale_base=1.5,
            wick_loss_scale_max_mult=8,
        ),
        "NILUSDT",
        10.0,
    )
    assert streak == 2
    assert mult == pytest.approx(2.25)
    assert qty == pytest.approx(22.5)


@pytest.mark.asyncio
async def test_loss_scale_qty_respects_max_times():
    runner = WickSpikeRunner()
    runner.set_loss_streak(3, "NILUSDT", 3)
    qty, streak, mult = await runner._loss_scale_qty(
        SimpleNamespace(
            id=3,
            wick_loss_scale_enabled=True,
            wick_loss_scale_base=2,
            wick_loss_scale_max_mult=8,
            wick_loss_scale_max_times=2,
        ),
        "NILUSDT",
        10.0,
    )
    assert streak == 2
    assert mult == 4.0
    assert qty == pytest.approx(40.0)


@pytest.mark.asyncio
async def test_apply_wick_close_hooks_tp_resets_even_if_db_still_shows_sl():
    runner = WickSpikeRunner()
    runner.set_loss_streak(4, "NILUSDT", 1)
    with patch(
        "app.services.wick_spike_runner.count_consecutive_stop_losses",
        new=AsyncMock(return_value=1),
    ) as mocked:
        await runner.apply_wick_close_hooks(
            object(),
            SimpleNamespace(
                id=4,
                signal_source="wick_spike",
                wick_loss_scale_enabled=True,
            ),
            "NILUSDT",
            "take_profit",
        )
    mocked.assert_not_called()
    assert runner._loss_streaks[(4, "NILUSDT")] == 0


@pytest.mark.asyncio
async def test_apply_wick_close_hooks_sets_streak():
    runner = WickSpikeRunner()
    with patch(
        "app.services.wick_spike_runner.count_consecutive_stop_losses",
        new=AsyncMock(return_value=3),
    ):
        await runner.apply_wick_close_hooks(
            object(),
            SimpleNamespace(
                id=4,
                signal_source="wick_spike",
                wick_loss_scale_enabled=True,
            ),
            "NILUSDT",
            "stop_loss",
        )
    assert runner._loss_streaks[(4, "NILUSDT")] == 3


@pytest.mark.asyncio
async def test_apply_wick_close_hooks_skips_when_scale_off():
    runner = WickSpikeRunner()
    await runner.apply_wick_close_hooks(
        object(),
        SimpleNamespace(
            id=4,
            signal_source="wick_spike",
            wick_loss_scale_enabled=False,
        ),
        "NILUSDT",
        "stop_loss",
    )
    assert (4, "NILUSDT") not in runner._loss_streaks


@pytest.mark.asyncio
async def test_loss_scale_qty_off_is_identity():
    runner = WickSpikeRunner()
    runner.set_loss_streak(3, "NILUSDT", 3)
    qty, streak, mult = await runner._loss_scale_qty(
        SimpleNamespace(id=3, wick_loss_scale_enabled=False, wick_loss_scale_max_mult=8),
        "NILUSDT",
        10.0,
    )
    assert (qty, streak, mult) == (10.0, 0, 1.0)

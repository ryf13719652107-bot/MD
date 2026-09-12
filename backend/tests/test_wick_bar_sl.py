"""接针K条件限价止损：取价、挂撤、成交互撤、马丁改量。"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.position_manager import (
    PositionManager,
    wick_bar_sl_enabled,
    wick_bar_stop_price,
)
from app.services.tick_context import OpenApiResult
from app.services.strategy_engine import Signal


def test_wick_bar_stop_price_long_uses_low_minus_tick():
    assert wick_bar_stop_price("long", 1.20, 1.00, 0.01) == pytest.approx(0.99)


def test_wick_bar_stop_price_short_uses_high_plus_tick():
    assert wick_bar_stop_price("short", 1.20, 1.00, 0.01) == pytest.approx(1.21)


def test_wick_bar_stop_price_zero_tick_uses_relative_offset():
    assert wick_bar_stop_price("long", 1.20, 1.00, 0) == pytest.approx(1.00 - 1e-6)
    assert wick_bar_stop_price("short", 1.20, 1.00, 0) == pytest.approx(1.20 + 1.20e-6)


def test_wick_bar_sl_enabled_only_wick_and_switch():
    assert wick_bar_sl_enabled(SimpleNamespace(signal_source="wick_spike", wick_bar_sl_enabled=True))
    assert not wick_bar_sl_enabled(
        SimpleNamespace(signal_source="wick_spike", wick_bar_sl_enabled=False)
    )
    assert not wick_bar_sl_enabled(
        SimpleNamespace(signal_source="wavetrend", wick_bar_sl_enabled=True)
    )


def _open_result(**kwargs):
    defaults = dict(
        symbol="BTCUSDT",
        signal=Signal.SHORT,
        base_qty=1.0,
        current_price=1.1,
        rsi=0.0,
        signal_label="wick",
        side="sell",
        position_side="short",
        ps="SHORT",
        order={"id": "open-1"},
        avg_price=1.1,
        filled_qty=2.0,
        tp_price=1.08,
        sl_price=1.21,
    )
    defaults.update(kwargs)
    return OpenApiResult(**defaults)


@pytest.mark.asyncio
async def test_place_open_sl_stop_skips_when_disabled():
    pm = PositionManager()
    auth = MagicMock()
    auth.create_stop_limit_order = AsyncMock()
    strategy = SimpleNamespace(
        id=1, signal_source="wick_spike", wick_bar_sl_enabled=False
    )
    result = _open_result()
    out = await pm.place_open_sl_stop(auth, strategy, result)
    auth.create_stop_limit_order.assert_not_called()
    assert out.sl_stop_order_id is None


@pytest.mark.asyncio
async def test_place_open_sl_stop_uses_stop_limit_no_reduce_only():
    pm = PositionManager()
    auth = MagicMock()
    auth.create_stop_limit_order = AsyncMock(return_value={"id": "sl-9"})
    strategy = SimpleNamespace(
        id=1, signal_source="wick_spike", wick_bar_sl_enabled=True
    )
    result = _open_result(sl_price=1.21, filled_qty=2.0, position_side="short")
    out = await pm.place_open_sl_stop(auth, strategy, result)
    auth.create_stop_limit_order.assert_called_once()
    args, kwargs = auth.create_stop_limit_order.call_args
    assert args[0] == "BTCUSDT"
    assert args[1] == "buy"
    assert args[2] == 2.0
    assert args[3] == pytest.approx(1.21)
    assert kwargs["stop_price"] == pytest.approx(1.21)
    assert kwargs["position_side"] == "SHORT"
    assert "reduce_only" not in kwargs
    assert out.sl_stop_order_id == "sl-9"


@pytest.mark.asyncio
async def test_place_open_sl_stop_long_sells_at_low():
    pm = PositionManager()
    auth = MagicMock()
    auth.create_stop_limit_order = AsyncMock(return_value={"id": "sl-l"})
    strategy = SimpleNamespace(
        id=1, signal_source="wick_spike", wick_bar_sl_enabled=True
    )
    result = _open_result(
        signal=Signal.LONG,
        side="buy",
        position_side="long",
        ps="LONG",
        sl_price=0.99,
    )
    await pm.place_open_sl_stop(auth, strategy, result)
    args, kwargs = auth.create_stop_limit_order.call_args
    assert args[1] == "sell"
    assert kwargs["position_side"] == "LONG"


@pytest.mark.asyncio
async def test_ensure_sl_skips_and_cancels_when_switch_off():
    pm = PositionManager()
    session = AsyncMock()
    session.flush = AsyncMock()
    strategy = SimpleNamespace(
        id=1, signal_source="wick_spike", wick_bar_sl_enabled=False
    )
    auth = MagicMock()
    auth.cancel_order = AsyncMock(return_value={"status": "canceled"})
    pos = SimpleNamespace(
        sl_stop_order_id="sl-old",
        stop_loss_price=1.21,
        quantity=2.0,
        exchange_order_id="x",
        layer=0,
    )
    with patch(
        "app.services.position_manager._fetch_order",
        new=AsyncMock(return_value={"id": "sl-old", "status": "canceled"}),
    ):
        await pm._ensure_sl_stop_orders(
            session, strategy, "BTCUSDT", auth, [pos], 2.0, "short"
        )
    auth.cancel_order.assert_called()
    assert pos.sl_stop_order_id is None


@pytest.mark.asyncio
async def test_ensure_sl_places_when_missing():
    pm = PositionManager()
    session = AsyncMock()
    session.flush = AsyncMock()
    strategy = SimpleNamespace(
        id=1, signal_source="wick_spike", wick_bar_sl_enabled=True
    )
    auth = MagicMock()
    auth.create_stop_limit_order = AsyncMock(return_value={"id": "sl-new"})
    pos = SimpleNamespace(
        sl_stop_order_id=None,
        stop_loss_price=1.21,
        quantity=3.0,
        exchange_order_id="x",
        layer=0,
    )
    await pm._ensure_sl_stop_orders(
        session, strategy, "BTCUSDT", auth, [pos], 3.0, "short"
    )
    auth.create_stop_limit_order.assert_called_once()
    args, kwargs = auth.create_stop_limit_order.call_args
    assert args[2] == pytest.approx(3.0)
    assert args[3] == pytest.approx(1.21)
    assert pos.sl_stop_order_id == "sl-new"


@pytest.mark.asyncio
async def test_ensure_sl_keeps_open_order_same_qty():
    pm = PositionManager()
    session = AsyncMock()
    session.flush = AsyncMock()
    strategy = SimpleNamespace(
        id=1, signal_source="wick_spike", wick_bar_sl_enabled=True
    )
    auth = MagicMock()
    auth.create_stop_limit_order = AsyncMock()
    auth.cancel_order = AsyncMock()
    pos = SimpleNamespace(
        sl_stop_order_id="sl-1",
        stop_loss_price=1.21,
        quantity=2.0,
        exchange_order_id="x",
        layer=0,
    )
    with patch(
        "app.services.position_manager._fetch_order",
        new=AsyncMock(
            return_value={"id": "sl-1", "status": "open", "amount": 2.0, "price": 1.21}
        ),
    ):
        await pm._ensure_sl_stop_orders(
            session, strategy, "BTCUSDT", auth, [pos], 2.0, "short"
        )
    auth.create_stop_limit_order.assert_not_called()
    assert pos.sl_stop_order_id == "sl-1"


@pytest.mark.asyncio
async def test_cancel_bot_sls_only_local_ids():
    pm = PositionManager()
    auth = MagicMock()
    auth.cancel_order = AsyncMock(return_value={"status": "canceled"})
    p1 = SimpleNamespace(sl_stop_order_id="sl-1")
    p2 = SimpleNamespace(sl_stop_order_id="sl-1")
    p3 = SimpleNamespace(sl_stop_order_id=None)
    with patch(
        "app.services.position_manager._fetch_order",
        new=AsyncMock(return_value={"id": "sl-1", "status": "canceled"}),
    ):
        await pm.cancel_bot_sls_on_positions(auth, "BTCUSDT", [p1, p2, p3], 1)
    auth.cancel_order.assert_called()
    assert p1.sl_stop_order_id is None
    assert p2.sl_stop_order_id is None


@pytest.mark.asyncio
async def test_close_on_tp_fill_cancels_sl():
    pm = PositionManager()
    session = AsyncMock()
    session.flush = AsyncMock()
    session.add = MagicMock()
    strategy = SimpleNamespace(
        id=1,
        account_id=1,
        take_profit_limit_order=True,
        martingale_mult=1.5,
        max_layers=8,
        price_drop_multiplier=1.0,
        take_profit_pct=2.0,
        signal_source="wick_spike",
        wick_bar_sl_enabled=True,
    )
    auth = MagicMock()
    sl_called = []

    async def _spy_sl(_auth, symbol, positions, strategy_id):
        sl_called.append((symbol, strategy_id))
        for p in positions:
            p.sl_stop_order_id = None

    pm.cancel_bot_sls_on_positions = _spy_sl
    pm.cancel_bot_tps_on_positions = AsyncMock()
    pos = SimpleNamespace(
        id=11,
        strategy_id=1,
        account_id=1,
        symbol="BTCUSDT",
        side="short",
        quantity=2.0,
        entry_price=1.1,
        layer=0,
        exchange_order_id="open-1",
        tp_limit_order_id="tp-1",
        sl_stop_order_id="sl-1",
        stop_loss_price=1.21,
        closed_at=None,
        opened_at=None,
        take_profit_price=1.08,
        unrealized_pnl=0.0,
    )
    with (
        patch(
            "app.services.position_manager.claim_open_position_close",
            new=AsyncMock(return_value=True),
        ),
        patch(
            "app.services.position_manager.find_existing_leg_close_trade",
            new=AsyncMock(return_value=None),
        ),
        patch("app.services.position_manager.backup_trade"),
    ):
        await pm._close_positions(
            session,
            strategy,
            "BTCUSDT",
            auth,
            [pos],
            SimpleNamespace(),
            1.1,
            "short",
            "take_profit",
            1.08,
            pre_exit_price=1.079,
            fill_order={"id": "tp-1", "status": "filled", "average": 1.079},
        )
    assert sl_called
    assert sl_called[0][0] == "BTCUSDT"


@pytest.mark.asyncio
async def test_ensure_sl_replaces_when_qty_grows():
    """马丁后数量变大：撤旧单、同价重挂全腿数量。"""
    pm = PositionManager()
    session = AsyncMock()
    session.flush = AsyncMock()
    strategy = SimpleNamespace(
        id=1, signal_source="wick_spike", wick_bar_sl_enabled=True
    )
    auth = MagicMock()
    auth.cancel_order = AsyncMock(return_value={"status": "canceled"})
    auth.create_stop_limit_order = AsyncMock(return_value={"id": "sl-new"})
    pos = SimpleNamespace(
        sl_stop_order_id="sl-old",
        stop_loss_price=1.21,
        quantity=5.0,
        exchange_order_id="x",
        layer=0,
    )
    with patch(
        "app.services.position_manager._fetch_order",
        new=AsyncMock(
            return_value={"id": "sl-old", "status": "open", "amount": 2.0, "price": 1.21}
        ),
    ):
        await pm._ensure_sl_stop_orders(
            session, strategy, "BTCUSDT", auth, [pos], 5.0, "short"
        )
    auth.create_stop_limit_order.assert_called_once()
    args, _kwargs = auth.create_stop_limit_order.call_args
    assert args[2] == pytest.approx(5.0)
    assert args[3] == pytest.approx(1.21)
    assert pos.sl_stop_order_id == "sl-new"


@pytest.mark.asyncio
async def test_try_close_on_sl_fill_uses_stop_loss_reason():
    pm = PositionManager()
    session = AsyncMock()
    strategy = SimpleNamespace(id=1)
    auth = MagicMock()
    pos = SimpleNamespace(
        sl_stop_order_id="sl-1",
        symbol="BTCUSDT",
        side="short",
        quantity=2.0,
        entry_price=1.1,
        closed_at=None,
        exchange_order_id="x",
        layer=0,
    )
    closed = []

    async def _fake_close(*args, **kwargs):
        closed.append(kwargs.get("pre_exit_price") or args)

    pm._close_positions = AsyncMock(side_effect=_fake_close)
    with patch(
        "app.services.position_manager._fetch_order",
        new=AsyncMock(
            return_value={
                "id": "sl-1",
                "status": "filled",
                "average": 1.215,
                "filled": 2.0,
            }
        ),
    ):
        ok = await pm._try_close_on_sl_fill(
            session,
            strategy,
            "BTCUSDT",
            auth,
            [pos],
            SimpleNamespace(),
            1.1,
            "short",
            1.2,
        )
    assert ok is True
    pm._close_positions.assert_called_once()
    kwargs = pm._close_positions.call_args.kwargs
    assert kwargs["pre_exit_price"] == pytest.approx(1.215)
    # close_reason 是位置参数
    assert pm._close_positions.call_args.args[8] == "stop_loss"


@pytest.mark.asyncio
async def test_binance_stop_limit_params():
    from app.services.binance_service import BinanceService

    svc = BinanceService.__new__(BinanceService)
    svc.hedge_mode = True
    svc._pinned = True
    svc._exchange = MagicMock()
    svc._exchange.create_order = AsyncMock(return_value={"id": "1"})
    svc._exchange.price_to_precision = MagicMock(side_effect=lambda _s, p: f"{float(p):.4f}")
    svc._is_expired = lambda: False
    svc.ensure_markets_loaded = AsyncMock()
    svc._format_symbol = lambda s: s
    svc.price_tick_size = lambda _s: 0.01
    await svc.create_stop_limit_order(
        "BTCUSDT", "buy", 2.0, 1.21, stop_price=1.21, position_side="SHORT"
    )
    args, kwargs = svc.exchange.create_order.call_args
    assert args[1] == "STOP"
    assert args[2] == "buy"
    params = args[5]
    assert params["stopPrice"] == pytest.approx(1.21)
    assert params["workingType"] == "CONTRACT_PRICE"
    assert params["positionSide"] == "SHORT"
    assert "reduceOnly" not in params


@pytest.mark.asyncio
async def test_try_close_ignores_canceled_closed_without_fill():
    pm = PositionManager()
    pm._close_positions = AsyncMock()
    pos = SimpleNamespace(
        sl_stop_order_id="sl-1",
        symbol="BTCUSDT",
        side="short",
        quantity=2.0,
        entry_price=1.1,
        closed_at=None,
        exchange_order_id="x",
        layer=0,
    )
    with patch(
        "app.services.position_manager._fetch_order",
        new=AsyncMock(return_value={"id": "sl-1", "status": "closed", "filled": 0}),
    ):
        ok = await pm._try_close_on_sl_fill(
            AsyncMock(),
            SimpleNamespace(id=1),
            "BTCUSDT",
            MagicMock(),
            [pos],
            SimpleNamespace(),
            1.1,
            "short",
            1.2,
        )
    assert ok is False
    pm._close_positions.assert_not_called()


@pytest.mark.asyncio
async def test_manage_skips_martingale_when_wick_bar_sl_on():
    pm = PositionManager()
    pm._try_close_on_sl_fill = AsyncMock(return_value=False)
    pm._ensure_tp_limit_orders = AsyncMock()
    pm._ensure_sl_stop_orders = AsyncMock()
    pm._martingale_add = AsyncMock()
    session = AsyncMock()
    session.flush = AsyncMock()
    strategy = SimpleNamespace(
        id=1,
        signal_source="wick_spike",
        wick_bar_sl_enabled=True,
        take_profit_limit_order=False,
        stop_loss_enabled=False,
        single_symbol_stop_loss_enabled=False,
        martingale_mult=1.5,
        max_layers=8,
        price_drop_pct=1.0,
        price_drop_multiplier=1.0,
        take_profit_pct=2.0,
    )
    pos = SimpleNamespace(
        side="short",
        quantity=1.0,
        entry_price=100.0,
        layer=0,
        exchange_order_id="x",
        tp_limit_order_id=None,
        sl_stop_order_id="sl-1",
        stop_loss_price=110.0,
        trailing_tp_state=None,
        mark_price=None,
        unrealized_pnl=0,
    )
    await pm._manage_positions(
        session,
        strategy,
        "BTCUSDT",
        MagicMock(),
        MagicMock(),
        [pos],
        1.0,
        102.0,
        1000.0,
        10,
        None,
        None,
    )
    pm._martingale_add.assert_not_called()
    session.flush.assert_called()

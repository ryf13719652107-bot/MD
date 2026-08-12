"""止盈限价单补挂：只管机器人自己的单号与数量。"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.martingale_engine import MartingaleEngine
from app.services.position_manager import PositionManager


@pytest.mark.asyncio
async def test_ensure_tp_skips_when_limit_mode_off():
    pm = PositionManager()
    session = AsyncMock()
    strategy = SimpleNamespace(take_profit_limit_order=False, id=1)
    eng = MartingaleEngine(base_quantity=1, take_profit_pct=2)
    auth = MagicMock()
    auth.create_limit_order = AsyncMock()
    pos = SimpleNamespace(
        tp_limit_order_id=None, take_profit_price=None, layer=0, exchange_order_id="x"
    )
    await pm._ensure_tp_limit_orders(
        session, strategy, "BTCUSDT", auth, [pos], eng, 100.0, 1.0, "long"
    )
    auth.create_limit_order.assert_not_called()


@pytest.mark.asyncio
async def test_ensure_tp_keeps_bot_id_when_qty_ok():
    pm = PositionManager()
    session = AsyncMock()
    session.flush = AsyncMock()
    strategy = SimpleNamespace(take_profit_limit_order=True, id=1)
    eng = MartingaleEngine(base_quantity=1, take_profit_pct=2)
    auth = MagicMock()
    auth.create_limit_order = AsyncMock()
    auth.cancel_order = AsyncMock()
    pos = SimpleNamespace(
        tp_limit_order_id="oid-1",
        take_profit_price=102.0,
        layer=0,
        exchange_order_id="x",
        quantity=1.5,
    )
    with patch(
        "app.services.position_manager._fetch_order",
        new=AsyncMock(
            return_value={
                "id": "oid-1",
                "status": "open",
                "amount": 1.5,
                "price": 102.0,
            }
        ),
    ):
        await pm._ensure_tp_limit_orders(
            session, strategy, "BTCUSDT", auth, [pos], eng, 100.0, 1.5, "long"
        )
    auth.create_limit_order.assert_not_called()
    auth.cancel_order.assert_not_called()
    assert pos.tp_limit_order_id == "oid-1"


@pytest.mark.asyncio
async def test_ensure_tp_places_when_missing():
    pm = PositionManager()
    session = AsyncMock()
    session.flush = AsyncMock()
    strategy = SimpleNamespace(take_profit_limit_order=True, id=1)
    eng = MartingaleEngine(base_quantity=1, take_profit_pct=2)
    auth = MagicMock()
    auth.exchange_id = "binance"
    auth.hedge_mode = True
    auth.create_limit_order = AsyncMock(return_value={"id": "new-tp"})
    pos = SimpleNamespace(
        tp_limit_order_id=None,
        take_profit_price=None,
        layer=0,
        exchange_order_id="open-1",
        quantity=1.5,
    )
    await pm._ensure_tp_limit_orders(
        session, strategy, "BTCUSDT", auth, [pos], eng, 100.0, 99.0, "short"
    )
    auth.create_limit_order.assert_called_once()
    # 按机器人数量挂，不是传入的整腿 total_qty
    assert auth.create_limit_order.call_args.args[2] == pytest.approx(1.5)
    assert pos.tp_limit_order_id == "new-tp"
    assert pos.take_profit_price == eng.get_take_profit_price(100.0, "short")


@pytest.mark.asyncio
async def test_ensure_tp_does_not_place_for_non_bot_orphan():
    """本地无 exchange_order_id（非策略开仓）→ 不补挂止盈。"""
    pm = PositionManager()
    session = AsyncMock()
    strategy = SimpleNamespace(take_profit_limit_order=True, id=1)
    eng = MartingaleEngine(base_quantity=1, take_profit_pct=2)
    auth = MagicMock()
    auth.create_limit_order = AsyncMock()
    pos = SimpleNamespace(
        tp_limit_order_id=None,
        take_profit_price=None,
        layer=0,
        exchange_order_id="",
        quantity=10.0,
    )
    await pm._ensure_tp_limit_orders(
        session, strategy, "STARUSDT", auth, [pos], eng, 1.0, 10.0, "short"
    )
    auth.create_limit_order.assert_not_called()
    assert pos.tp_limit_order_id is None


@pytest.mark.asyncio
async def test_ensure_tp_replaces_undersized_bot_order_only():
    """本地已有止盈 id 数量不符 → 只撤该 id 并按机器人数量重挂。"""
    pm = PositionManager()
    session = AsyncMock()
    session.flush = AsyncMock()
    strategy = SimpleNamespace(take_profit_limit_order=True, id=1)
    eng = MartingaleEngine(base_quantity=1, take_profit_pct=2)
    auth = MagicMock()
    auth.exchange_id = "binance"
    auth.hedge_mode = True
    auth.create_limit_order = AsyncMock(return_value={"id": "new-tp"})
    auth.cancel_order = AsyncMock()
    pos = SimpleNamespace(
        tp_limit_order_id="old-tp",
        take_profit_price=110.0,
        layer=0,
        exchange_order_id="open-1",
        quantity=15.0,
    )
    with patch(
        "app.services.position_manager._fetch_order",
        new=AsyncMock(
            return_value={
                "id": "old-tp",
                "status": "open",
                "amount": 5.0,
                "price": 110.0,
            }
        ),
    ):
        await pm._ensure_tp_limit_orders(
            session, strategy, "BTCUSDT", auth, [pos], eng, 100.0, 15.0, "long"
        )
    auth.cancel_order.assert_called()
    auth.create_limit_order.assert_called_once()
    assert pos.tp_limit_order_id == "new-tp"


@pytest.mark.asyncio
async def test_reconcile_orphan_requires_adopt_and_order_id():
    pm = PositionManager()
    session = AsyncMock()
    strategy = SimpleNamespace(
        id=1,
        account_id=1,
        direction="long",
        martingale_mult=1.5,
        max_layers=5,
        price_drop_multiplier=1.0,
        take_profit_pct=1.0,
    )
    auth = MagicMock()
    raw = [
        {
            "symbol": "GUAUSDT",
            "side": "long",
            "contracts": 10,
            "entryPrice": 1.0,
            "markPrice": 1.0,
            "unrealizedPnl": 0,
        }
    ]
    out = await pm._reconcile_orphan_from_exchange(
        session, strategy, "GUAUSDT", auth, 1.0, raw_exchange_positions=raw
    )
    assert out == "no_orphan"
    session.add.assert_not_called()


@pytest.mark.asyncio
async def test_cancel_bot_tps_on_positions_only_cancels_local_ids():
    pm = PositionManager()
    auth = MagicMock()
    auth.cancel_order = AsyncMock(return_value={"id": "tp-1", "status": "canceled"})
    auth.fetch_order = AsyncMock(return_value={"id": "tp-1", "status": "canceled"})
    p1 = SimpleNamespace(tp_limit_order_id="tp-1")
    p2 = SimpleNamespace(tp_limit_order_id="tp-1")
    p3 = SimpleNamespace(tp_limit_order_id=None)
    with patch(
        "app.services.position_manager._fetch_order",
        new=AsyncMock(return_value={"id": "tp-1", "status": "canceled"}),
    ):
        await pm.cancel_bot_tps_on_positions(auth, "BTCUSDT", [p1, p2, p3], 1)
    auth.cancel_order.assert_called()
    assert p1.tp_limit_order_id is None
    assert p2.tp_limit_order_id is None


@pytest.mark.asyncio
async def test_recover_bot_open_after_db_fail_uses_fill_order_id():
    pm = PositionManager()
    session = AsyncMock()
    session.execute = AsyncMock(
        return_value=SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: []))
    )
    session.flush = AsyncMock()
    strategy = SimpleNamespace(
        id=1,
        account_id=1,
        direction="long",
        martingale_mult=1.5,
        max_layers=5,
        price_drop_multiplier=1.0,
        take_profit_pct=1.0,
    )
    auth = MagicMock()
    auth.fetch_positions = AsyncMock(
        return_value=[
            {
                "symbol": "BTCUSDT",
                "side": "long",
                "contracts": 2.0,
                "entryPrice": 100.0,
                "markPrice": 100.0,
                "unrealizedPnl": 0,
            }
        ]
    )
    result = SimpleNamespace(
        symbol="BTCUSDT",
        order={"id": "fill-1"},
        filled_qty=2.0,
        avg_price=100.0,
        current_price=100.0,
    )
    with patch("app.services.position_manager.strategy_log_service"):
        ok = await pm.recover_bot_open_after_db_fail(session, strategy, result, auth)
    assert ok is True
    session.add.assert_called_once()
    pos = session.add.call_args.args[0]
    assert pos.exchange_order_id == "fill-1"
    assert pos.quantity == pytest.approx(2.0)

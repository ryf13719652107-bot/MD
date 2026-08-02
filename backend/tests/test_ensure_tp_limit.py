"""止盈限价单补挂：已有 id 跳过；无限价模式跳过。"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

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
async def test_ensure_tp_skips_when_already_has_order_id():
    pm = PositionManager()
    session = AsyncMock()
    strategy = SimpleNamespace(take_profit_limit_order=True, id=1)
    eng = MartingaleEngine(base_quantity=1, take_profit_pct=2)
    auth = MagicMock()
    auth.create_limit_order = AsyncMock()
    auth.fetch_open_orders = AsyncMock(return_value=[])
    pos = SimpleNamespace(
        tp_limit_order_id="oid-1",
        take_profit_price=102.0,
        layer=0,
        exchange_order_id="x",
    )
    await pm._ensure_tp_limit_orders(
        session, strategy, "BTCUSDT", auth, [pos], eng, 100.0, 1.0, "long"
    )
    auth.create_limit_order.assert_not_called()


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
    auth.fetch_open_orders = AsyncMock(return_value=[])
    auth.exchange = MagicMock()
    auth.exchange.fetch_open_orders = AsyncMock(return_value=[])
    auth._format_symbol = lambda s: f"{s[:-4]}/USDT:USDT" if s.endswith("USDT") else s
    pos = SimpleNamespace(
        tp_limit_order_id=None,
        take_profit_price=None,
        layer=0,
        exchange_order_id="open-1",
    )
    await pm._ensure_tp_limit_orders(
        session, strategy, "BTCUSDT", auth, [pos], eng, 100.0, 1.5, "short"
    )
    auth.create_limit_order.assert_called_once()
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
    auth.exchange = MagicMock()
    auth.exchange.fetch_open_orders = AsyncMock(return_value=[])
    auth._format_symbol = lambda s: s
    auth.hedge_mode = True
    auth.exchange_id = "binance"
    pos = SimpleNamespace(
        tp_limit_order_id=None,
        take_profit_price=None,
        layer=0,
        exchange_order_id="",
    )
    await pm._ensure_tp_limit_orders(
        session, strategy, "STARUSDT", auth, [pos], eng, 1.0, 10.0, "short"
    )
    auth.create_limit_order.assert_not_called()
    assert pos.tp_limit_order_id is None

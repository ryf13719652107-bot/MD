import pytest
from unittest.mock import AsyncMock, MagicMock

from app.services.binance_service import BinanceService


@pytest.mark.asyncio
async def test_set_symbol_leverage_uses_ccxt():
    svc = BinanceService.__new__(BinanceService)
    svc.hedge_mode = True
    svc.exchange = MagicMock()
    svc.exchange.load_markets = AsyncMock()
    svc.exchange.set_leverage = AsyncMock()
    svc.exchange.fapiPrivatePostLeverage = AsyncMock()

    lev = await svc.set_symbol_leverage("BTCUSDT", 25)

    assert lev == 25
    svc.exchange.set_leverage.assert_awaited_once_with(25, "BTC/USDT:USDT")
    svc.exchange.fapiPrivatePostLeverage.assert_not_awaited()


@pytest.mark.asyncio
async def test_set_symbol_leverage_fallback_raw_api():
    svc = BinanceService.__new__(BinanceService)
    svc.hedge_mode = True
    svc.exchange = MagicMock()
    svc.exchange.load_markets = AsyncMock()
    svc.exchange.set_leverage = AsyncMock(side_effect=Exception("ccxt fail"))
    svc.exchange.fapiPrivatePostLeverage = AsyncMock()

    lev = await svc.set_symbol_leverage("ETHUSDT", 10)

    assert lev == 10
    svc.exchange.fapiPrivatePostLeverage.assert_awaited_once_with(
        {"symbol": "ETHUSDT", "leverage": 10}
    )


@pytest.mark.asyncio
async def test_set_symbol_leverage_already_set_treated_ok():
    svc = BinanceService.__new__(BinanceService)
    svc.hedge_mode = True
    svc.exchange = MagicMock()
    svc.exchange.load_markets = AsyncMock()
    svc.exchange.set_leverage = AsyncMock(
        side_effect=Exception('{"code":-4028,"msg":"Leverage 10 already exist with 10"}')
    )
    svc.exchange.fapiPrivatePostLeverage = AsyncMock()

    lev = await svc.set_symbol_leverage("BTCUSDT", 10)
    assert lev == 10
    svc.exchange.fapiPrivatePostLeverage.assert_not_awaited()


@pytest.mark.asyncio
async def test_set_symbol_leverage_clamps():
    svc = BinanceService.__new__(BinanceService)
    svc.hedge_mode = False
    svc.exchange = MagicMock()
    svc.exchange.load_markets = AsyncMock()
    svc.exchange.set_leverage = AsyncMock()

    assert await svc.set_symbol_leverage("BTCUSDT", 0) == 1
    svc.exchange.set_leverage.assert_awaited_with(1, "BTC/USDT:USDT")

    svc.exchange.set_leverage.reset_mock()
    assert await svc.set_symbol_leverage("BTCUSDT", 200) == 125
    svc.exchange.set_leverage.assert_awaited_with(125, "BTC/USDT:USDT")

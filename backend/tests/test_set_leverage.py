import pytest
from unittest.mock import AsyncMock, MagicMock

from app.services.binance_service import BinanceService


def _svc() -> BinanceService:
    svc = BinanceService.__new__(BinanceService)
    svc.hedge_mode = True
    ex = MagicMock()
    svc._exchange = ex
    svc._ws_exchange = ex
    svc._pinned = True
    svc._created_at = 0
    svc._leverage_cache = {}
    ex.load_markets = AsyncMock()
    ex.set_leverage = AsyncMock()
    ex.fapiPrivatePostLeverage = AsyncMock()
    return svc


@pytest.mark.asyncio
async def test_set_symbol_leverage_uses_ccxt():
    svc = _svc()

    lev, cache_hit = await svc.set_symbol_leverage("BTCUSDT", 25)

    assert lev == 25
    assert cache_hit is False
    svc.exchange.set_leverage.assert_awaited_once_with(25, "BTC/USDT:USDT")
    svc.exchange.fapiPrivatePostLeverage.assert_not_awaited()


@pytest.mark.asyncio
async def test_set_symbol_leverage_fallback_raw_api():
    svc = _svc()
    svc.exchange.set_leverage = AsyncMock(side_effect=Exception("ccxt fail"))

    lev, cache_hit = await svc.set_symbol_leverage("ETHUSDT", 10)

    assert lev == 10
    assert cache_hit is False
    svc.exchange.fapiPrivatePostLeverage.assert_awaited_once_with(
        {"symbol": "ETHUSDT", "leverage": 10}
    )


@pytest.mark.asyncio
async def test_set_symbol_leverage_already_set_treated_ok():
    svc = _svc()
    svc.exchange.set_leverage = AsyncMock(
        side_effect=Exception('{"code":-4028,"msg":"Leverage 10 already exist with 10"}')
    )

    lev, cache_hit = await svc.set_symbol_leverage("BTCUSDT", 10)
    assert lev == 10
    assert cache_hit is False
    svc.exchange.fapiPrivatePostLeverage.assert_not_awaited()


@pytest.mark.asyncio
async def test_set_symbol_leverage_reports_cache_hit():
    svc = _svc()
    svc._leverage_cache = {"BTCUSDT": 10}

    assert await svc.set_symbol_leverage("BTCUSDT", 10) == (10, True)
    svc.exchange.set_leverage.assert_not_awaited()
    svc.exchange.fapiPrivatePostLeverage.assert_not_awaited()


@pytest.mark.asyncio
async def test_set_symbol_leverage_clamps():
    svc = _svc()
    svc.hedge_mode = False

    assert await svc.set_symbol_leverage("BTCUSDT", 0) == (1, False)
    svc.exchange.set_leverage.assert_awaited_with(1, "BTC/USDT:USDT")

    svc.exchange.set_leverage.reset_mock()
    assert await svc.set_symbol_leverage("BTCUSDT", 200) == (125, False)
    svc.exchange.set_leverage.assert_awaited_with(125, "BTC/USDT:USDT")

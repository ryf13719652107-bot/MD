"""Tick execution: TickContext, two-phase signal/open, account-level concurrency."""
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.account_concurrency import (
    ACCOUNT_ORDER_CONCURRENCY,
    account_order_sem,
)
from app.services.binance_service import BinanceService
from app.services.position_manager import PositionManager
from app.services.strategy_engine import Signal
from app.services.tick_context import TickContext, SignalCandidate, exchange_legs_from_positions


def _make_binance_svc() -> BinanceService:
    svc = BinanceService.__new__(BinanceService)
    svc.hedge_mode = True
    svc.exchange = MagicMock()
    svc._markets_loaded = False
    svc._leverage_cache = {}
    return svc


@pytest.mark.asyncio
async def test_ensure_markets_loaded_once():
    svc = _make_binance_svc()
    svc.exchange.load_markets = AsyncMock()

    await svc.ensure_markets_loaded()
    await svc.ensure_markets_loaded()

    svc.exchange.load_markets.assert_awaited_once()
    assert svc._markets_loaded is True


@pytest.mark.asyncio
async def test_set_leverage_cache_skips_duplicate():
    svc = _make_binance_svc()
    svc.exchange.set_leverage = AsyncMock()
    svc.exchange.fapiPrivatePostLeverage = AsyncMock()

    await svc.set_symbol_leverage("BTCUSDT", 10)
    await svc.set_symbol_leverage("BTCUSDT", 10)

    svc.exchange.set_leverage.assert_awaited_once()


def test_exchange_legs_from_positions():
    raw = [
        {"symbol": "BTC/USDT:USDT", "side": "long", "contracts": 0.01},
        {"symbol": "ETHUSDT", "side": "short", "contracts": 1.0},
        {"symbol": "BTCUSDT", "side": "long", "contracts": 0},
    ]
    legs = exchange_legs_from_positions(raw)
    assert legs[("BTCUSDT", "long")] == pytest.approx(0.01)
    assert legs[("ETHUSDT", "short")] == pytest.approx(1.0)
    assert ("BTCUSDT", "short") not in legs


def test_passes_new_entry_filters_uses_tick_context():
    pm = PositionManager()
    strategy = MagicMock()
    strategy.direction = "long"
    strategy.use_coin_pool = True

    ctx = TickContext(
        exclude_norm=frozenset({"TRUMPUSDT"}),
        funding_rates={"BTCUSDT": 0.5},
        funding_filter_enabled=True,
        allow_new_norms=frozenset({"BTCUSDT"}),
        wallet_balance_valid=True,
    )

    with patch(
        "app.services.strategy_flags.funding_rate_blocks_new_entry",
        return_value=False,
    ), patch(
        "app.services.strategy_flags.funding_rate_threshold_pct",
        return_value=0.1,
    ):
        assert pm._passes_new_entry_filters("BTCUSDT", strategy, ctx) is True
        assert pm._passes_new_entry_filters("TRUMPUSDT", strategy, ctx) is False
        assert pm._passes_new_entry_filters("ETHUSDT", strategy, ctx) is False


def test_invalid_wallet_balance_blocks_new_entry():
    pm = PositionManager()
    strategy = MagicMock()
    strategy.direction = "long"
    ctx = TickContext()

    assert ctx.wallet_balance_valid is False
    assert pm._passes_new_entry_filters("BTCUSDT", strategy, ctx) is False


def test_exchange_symbols_for_direction_only_returns_active_matching_legs():
    from app.services.scheduler import _exchange_symbols_for_direction

    raw_positions = [
        {"symbol": "BTC/USDT:USDT", "side": "long", "contracts": 0.5},
        {"symbol": "BTCUSDT", "side": "long", "contracts": 0.5},
        {"symbol": "ETHUSDT", "side": "short", "contracts": 1},
        {"symbol": "SOLUSDT", "side": "long", "contracts": 0},
        {"symbol": "BADUSDT", "side": "long", "contracts": "invalid"},
    ]

    assert _exchange_symbols_for_direction(raw_positions, "long") == [
        "BTC/USDT:USDT"
    ]


def test_orphan_recovery_resolves_same_direction_strategy_ownership():
    from app.services.scheduler import _strategy_can_recover_symbol

    fixed_btc = MagicMock(id=1, use_coin_pool=False, symbol="BTCUSDT")
    pool_a = MagicMock(id=2, use_coin_pool=True, symbol=None)

    peers = [fixed_btc, pool_a]
    assert _strategy_can_recover_symbol(fixed_btc, peers, "BTCUSDT") is True
    assert _strategy_can_recover_symbol(pool_a, peers, "BTCUSDT") is False
    assert _strategy_can_recover_symbol(pool_a, peers, "SOLUSDT") is True

    pool_b = MagicMock(id=3, use_coin_pool=True, symbol=None)
    assert _strategy_can_recover_symbol(
        pool_a,
        [fixed_btc, pool_a, pool_b],
        "SOLUSDT",
    ) is False


@pytest.mark.asyncio
async def test_account_order_sem_limits_concurrency():
    account_id = 9001
    sem = account_order_sem(account_id)
    assert sem._value == ACCOUNT_ORDER_CONCURRENCY

    active = 0
    peak = 0
    lock = asyncio.Lock()

    async def worker():
        nonlocal active, peak
        async with sem:
            async with lock:
                active += 1
                peak = max(peak, active)
            await asyncio.sleep(0.05)
            async with lock:
                active -= 1

    await asyncio.gather(*[worker() for _ in range(5)])
    assert peak <= ACCOUNT_ORDER_CONCURRENCY


@pytest.mark.asyncio
async def test_execute_open_api_calls_market_order_not_evaluate_path():
    pm = PositionManager()
    strategy = MagicMock()
    strategy.id = 1
    strategy.leverage = 10
    strategy.martingale_mult = 2.0
    strategy.max_layers = 5
    strategy.price_drop_multiplier = 1.0
    strategy.take_profit_pct = 1.0
    strategy.take_profit_limit_order = False

    auth = _make_binance_svc()
    auth.set_symbol_leverage = AsyncMock(return_value=(10, True))
    auth.create_market_order = AsyncMock(
        return_value={"id": "1", "average": 100.0, "filled": 0.01}
    )
    auth.create_limit_order = AsyncMock()
    pm._is_blacklisted_now = AsyncMock(return_value=False)

    candidate = SignalCandidate(
        symbol="BTCUSDT",
        signal=Signal.LONG,
        klines=[],
        current_price=100.0,
        rsi=30.0,
        signal_label="RSI",
        base_qty=0.01,
    )

    result = await pm.execute_open_api(candidate, strategy, auth, 10)

    assert result is not None
    pm._is_blacklisted_now.assert_awaited_once_with(1, "BTCUSDT")
    auth.create_market_order.assert_awaited_once()
    auth.create_limit_order.assert_not_awaited()


@pytest.mark.asyncio
async def test_execute_open_api_rechecks_blacklist_before_market_order():
    pm = PositionManager()
    pm._is_blacklisted_now = AsyncMock(return_value=True)
    strategy = MagicMock()
    strategy.id = 1
    strategy.leverage = 10

    auth = _make_binance_svc()
    auth.set_symbol_leverage = AsyncMock(return_value=(10, True))
    auth.create_market_order = AsyncMock()

    candidate = SignalCandidate(
        symbol="1000PEPEUSDT",
        signal=Signal.LONG,
        klines=[],
        current_price=0.01,
        rsi=30.0,
        signal_label="RSI",
        base_qty=100.0,
    )

    result = await pm.execute_open_api(candidate, strategy, auth, 10)

    assert result is None
    pm._is_blacklisted_now.assert_awaited_once_with(1, "1000PEPEUSDT")
    auth.create_market_order.assert_not_awaited()


@pytest.mark.asyncio
async def test_sync_account_background_calls_syncer():
    from app.services.scheduler import StrategyScheduler

    scheduler = StrategyScheduler.__new__(StrategyScheduler)
    scheduler._syncer = MagicMock()
    scheduler._syncer.sync = AsyncMock()

    auth = _make_binance_svc()
    await scheduler._sync_account_background(auth, 42)

    scheduler._syncer.sync.assert_awaited_once_with(auth, 42, auth)


def test_scheduler_tick_uses_background_sync_not_await():
    import inspect
    from app.services.scheduler import StrategyScheduler

    src = inspect.getsource(StrategyScheduler._execute_strategy_impl)
    assert "create_task" in src
    assert "await self._syncer.sync" not in src


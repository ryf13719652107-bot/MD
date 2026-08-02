"""Preload and keep K-line streams warm for running strategies."""
import asyncio
import logging
from collections import defaultdict

from sqlalchemy import select

from ..database import async_session
from ..models.account import Account
from ..models.strategy import Strategy
from .exchange_factory import account_exchange_id, get_public_exchange
from .kline_stream import kline_stream_manager
from .leverage_prewarm import (
    _dedupe_symbols,
    _open_position_symbols,
    resolve_strategy_pool_symbols,
)

logger = logging.getLogger(__name__)

_KLINE_PREWARM_CONCURRENCY = 8


def _signal_kline_limit(strategy: Strategy) -> int:
    return 200 if strategy.signal_source in ("wavetrend", "trend_wt", "wick_spike") else 100


def _strategy_kline_timeframes(strategy: Strategy) -> list[tuple[str, int]]:
    """(timeframe, min_bars) pairs to prewarm for a strategy."""
    pairs = [(strategy.timeframe, _signal_kline_limit(strategy))]
    if strategy.signal_source == "trend_wt":
        atr_period = int(getattr(strategy, "st_atr_period", None) or 10)
        st_bars = max(200, atr_period + 100)
        for tf in (
            getattr(strategy, "st_timeframe_1", None) or "15m",
            getattr(strategy, "st_timeframe_2", None) or "30m",
        ):
            if tf != strategy.timeframe:
                pairs.append((tf, st_bars))
    seen: dict[str, int] = {}
    for tf, bars in pairs:
        seen[tf] = max(seen.get(tf, 0), bars)
    return list(seen.items())


async def prewarm_symbols_klines(
    public_client,
    symbols: list[str],
    timeframe: str,
    min_bars: int,
) -> tuple[int, int]:
    """Start K-line subscriptions and seed buffers. Returns (success_count, total)."""
    symbols = _dedupe_symbols(symbols)
    if not symbols:
        return 0, 0

    sem = asyncio.Semaphore(_KLINE_PREWARM_CONCURRENCY)

    async def one(sym: str) -> bool:
        async with sem:
            try:
                rows = await kline_stream_manager.get(
                    public_client, sym, timeframe, min_bars=min_bars
                )
                return len(rows) >= min_bars
            except Exception as e:
                logger.debug("kline prewarm %s %s failed: %s", sym, timeframe, e)
                return False

    results = await asyncio.gather(*[one(s) for s in symbols])
    return sum(1 for r in results if r), len(symbols)


async def prewarm_strategy_klines(
    strategy: Strategy,
    public_client=None,
) -> None:
    async with async_session() as session:
        account = await session.get(Account, strategy.account_id)
        exchange = account_exchange_id(account) if account else "binance"
    if public_client is None:
        public_client = await get_public_exchange(exchange)
    pool_syms = await resolve_strategy_pool_symbols(
        strategy, public_client, exchange=exchange
    )
    open_syms = await _open_position_symbols(strategy.id)
    symbols = _dedupe_symbols(pool_syms + open_syms)
    if not symbols:
        return

    for timeframe, min_bars in _strategy_kline_timeframes(strategy):
        ok, total = await prewarm_symbols_klines(
            public_client, symbols, timeframe, min_bars
        )
        logger.info(
            "Strategy %d: prewarmed K-lines %s %d/%d symbols",
            strategy.id,
            timeframe,
            ok,
            total,
        )


async def prewarm_strategy_klines_by_id(strategy_id: int) -> None:
    async with async_session() as session:
        strategy = await session.get(Strategy, strategy_id)
        if not strategy or strategy.status != "running":
            return
    await prewarm_strategy_klines(strategy)


async def prewarm_running_strategies_klines() -> None:
    """Prewarm K-line streams for all running strategies after pool refresh."""
    async with async_session() as session:
        result = await session.execute(select(Strategy).where(Strategy.status == "running"))
        strategies = list(result.scalars().all())
        accounts = {
            a.id: a
            for a in (
                await session.execute(select(Account))
            ).scalars().all()
        }
    if not strategies:
        return

    # Group by (exchange, timeframe, min_bars)
    groups: dict[tuple[str, str, int], list[str]] = defaultdict(list)
    for strategy in strategies:
        try:
            account = accounts.get(strategy.account_id)
            exchange = account_exchange_id(account) if account else "binance"
            public_client = await get_public_exchange(exchange)
            pool_syms = await resolve_strategy_pool_symbols(
                strategy, public_client, exchange=exchange
            )
            open_syms = await _open_position_symbols(strategy.id)
            syms = pool_syms + open_syms
            for timeframe, min_bars in _strategy_kline_timeframes(strategy):
                groups[(exchange, timeframe, min_bars)].extend(syms)
        except Exception as e:
            logger.warning("Strategy %d K-line prewarm resolve failed: %s", strategy.id, e)

    for (exchange, timeframe, min_bars), symbols in groups.items():
        public_client = await get_public_exchange(exchange)
        ok, total = await prewarm_symbols_klines(
            public_client, symbols, timeframe, min_bars
        )
        logger.info(
            "Running strategies [%s]: prewarmed K-lines %s %d/%d symbols",
            exchange,
            timeframe,
            ok,
            total,
        )

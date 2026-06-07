"""Pre-set exchange leverage for pool / open-position symbols (strategy start & pool refresh)."""
import asyncio
import logging

from sqlalchemy import select

from ..database import async_session
from ..models.account import Account
from ..models.position import Position
from ..models.strategy import Strategy
from .binance_service import (
    BinanceService,
    get_binance_service,
    get_public_binance,
    get_strategy_pool_exclude_symbols,
    filter_pool_symbols_by_funding,
)
from .coin_pool_service import coin_pool_service
from .encryption import decrypt
from .position_manager import _norm_sym
from .strategy_flags import (
    exclude_delisting_enabled,
    exclude_mainstream_enabled,
    exclude_funding_enabled,
    funding_rate_threshold_pct,
    normalize_coin_pool_source,
)

logger = logging.getLogger(__name__)

_PREWARM_CONCURRENCY = 5


def _dedupe_symbols(symbols: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for s in symbols:
        if not s:
            continue
        k = _norm_sym(s)
        if k in seen:
            continue
        seen.add(k)
        out.append(s)
    return out


async def resolve_strategy_pool_symbols(
    strategy: Strategy,
    public_binance: BinanceService,
) -> list[str]:
    """Pool or fixed symbol list with the same filters as scheduler tick."""
    if strategy.use_coin_pool:
        min_vol = float(getattr(strategy, "coin_pool_min_volume_24h", 0) or 0)
        mainstream_exclude = bool(exclude_mainstream_enabled(strategy))
        exclude_norm = await get_strategy_pool_exclude_symbols(
            public_binance,
            exclude_tradefi=bool(getattr(strategy, "exclude_tradefi", False)),
            exclude_delisting=exclude_delisting_enabled(strategy),
            exclude_mainstream=mainstream_exclude,
        )
        pool_symbols = await coin_pool_service.get_pool_symbols(
            normalize_coin_pool_source(strategy.coin_pool_source),
            strategy.coin_pool_top_n,
            min_volume_24h=min_vol,
            exclude_symbols_norm=set(exclude_norm) if exclude_norm else None,
        )
        if exclude_funding_enabled(strategy):
            pool_symbols = await filter_pool_symbols_by_funding(
                public_binance,
                pool_symbols,
                direction=strategy.direction,
                threshold_pct=funding_rate_threshold_pct(strategy),
            )
        return pool_symbols
    if strategy.symbol:
        return [strategy.symbol]
    return []


async def _open_position_symbols(strategy_id: int) -> list[str]:
    async with async_session() as session:
        stmt = (
            select(Position.symbol)
            .where(Position.strategy_id == strategy_id, Position.closed_at.is_(None))
            .distinct()
        )
        rows = (await session.execute(stmt)).scalars().all()
        return [s for s in rows if s]


async def auth_binance_for_strategy(strategy: Strategy) -> BinanceService | None:
    async with async_session() as session:
        account = await session.get(Account, strategy.account_id)
        if not account:
            return None
        api_key = decrypt(account.api_key_encrypted)
        api_secret = decrypt(account.api_secret_encrypted)
        return await get_binance_service(
            api_key, api_secret, account.testnet, account.hedge_mode
        )


async def prewarm_symbols_leverage(
    auth_binance: BinanceService,
    symbols: list[str],
    leverage: int,
) -> tuple[int, int]:
    """Returns (success_count, total_symbols)."""
    symbols = _dedupe_symbols(symbols)
    if not symbols:
        return 0, 0
    lev = max(1, min(125, int(leverage or 10)))
    try:
        await auth_binance.ensure_markets_loaded()
    except Exception as e:
        logger.warning("prewarm ensure_markets_loaded failed: %s", e)
        return 0, len(symbols)

    sem = asyncio.Semaphore(_PREWARM_CONCURRENCY)

    async def one(sym: str) -> bool:
        async with sem:
            try:
                await auth_binance.set_symbol_leverage(sym, lev)
                return True
            except Exception as e:
                logger.debug("prewarm leverage %s %sx failed: %s", sym, lev, e)
                return False

    results = await asyncio.gather(*[one(s) for s in symbols])
    return sum(1 for r in results if r), len(symbols)


async def prewarm_strategy_leverage(
    strategy: Strategy,
    auth_binance: BinanceService | None = None,
) -> None:
    public_binance = await get_public_binance()
    pool_syms = await resolve_strategy_pool_symbols(strategy, public_binance)
    open_syms = await _open_position_symbols(strategy.id)
    symbols = _dedupe_symbols(pool_syms + open_syms)
    if not symbols:
        return
    if auth_binance is None:
        auth_binance = await auth_binance_for_strategy(strategy)
    if auth_binance is None:
        logger.warning("Strategy %d: leverage prewarm skipped — no auth", strategy.id)
        return
    lev = int(strategy.leverage or 10)
    ok, total = await prewarm_symbols_leverage(auth_binance, symbols, lev)
    logger.info(
        "Strategy %d: prewarmed leverage %dx for %d/%d symbols",
        strategy.id, lev, ok, total,
    )


async def prewarm_strategy_leverage_by_id(strategy_id: int) -> None:
    async with async_session() as session:
        strategy = await session.get(Strategy, strategy_id)
        if not strategy or strategy.status != "running":
            return
    await prewarm_strategy_leverage(strategy)


async def prewarm_running_strategies_leverage() -> None:
    """Prewarm every running strategy after coin pool refresh."""
    async with async_session() as session:
        result = await session.execute(select(Strategy).where(Strategy.status == "running"))
        strategies = list(result.scalars().all())
    auth_cache: dict[int, BinanceService] = {}
    for strategy in strategies:
        try:
            if strategy.account_id not in auth_cache:
                auth = await auth_binance_for_strategy(strategy)
                if auth:
                    auth_cache[strategy.account_id] = auth
            auth_binance = auth_cache.get(strategy.account_id)
            if auth_binance:
                await prewarm_strategy_leverage(strategy, auth_binance)
        except Exception as e:
            logger.warning("Strategy %d leverage prewarm failed: %s", strategy.id, e)

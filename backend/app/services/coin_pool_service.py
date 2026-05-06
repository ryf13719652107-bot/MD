import asyncio
import logging
from datetime import datetime
from sqlalchemy import select, delete, func
from ..database import async_session
from ..models.coin_pool import CoinPool
from ..config import now_beijing
from .binance_service import BinanceService

logger = logging.getLogger(__name__)


class CoinPoolService:
    def __init__(self):
        self._refresh_task: asyncio.Task | None = None
        self._config = {
            "refresh_interval_seconds": 3600,
            "pool_source": "both",
            "max_symbols": 50,
        }
        self._last_refresh_ok: bool = False
        self._last_refresh_time: float = 0.0
        self._last_error: str = ""

    @property
    def config(self) -> dict:
        return self._config

    @property
    def status(self) -> dict:
        return {
            "last_refresh_ok": self._last_refresh_ok,
            "last_refresh_time": self._last_refresh_time,
            "last_error": self._last_error,
        }

    def update_config(self, **kwargs):
        self._config.update(kwargs)

    async def refresh_pool(self, binance_service: BinanceService):
        """Fetch top movers and update the coin pool in database."""
        movers = await binance_service.fetch_top_movers(
            source=self._config["pool_source"],
            limit=self._config["max_symbols"],
        )
        async with async_session() as session:
            await session.execute(delete(CoinPool))
            for item in movers:
                coin = CoinPool(
                    symbol=item["symbol"],
                    rank=item["rank"],
                    price_change_pct=item["price_change_pct"],
                    volume_24h=item.get("volume_24h", 0),
                    source=item["source"],
                    added_at=now_beijing(),
                    last_updated=now_beijing(),
                )
                session.add(coin)
            await session.commit()
        self._last_refresh_ok = True
        self._last_refresh_time = now_beijing().timestamp()
        self._last_error = ""
        logger.info("Coin pool refreshed: %d symbols", len(movers))

    async def get_pool(self, source: str | None = None) -> list[CoinPool]:
        """Get current coin pool from database.

        Args:
            source: 'gainers', 'losers', 'both', or None (all).
                    'both' returns all coins without source filtering.
        """
        async with async_session() as session:
            stmt = select(CoinPool).order_by(CoinPool.rank)
            if source and source != "both":
                stmt = stmt.where(CoinPool.source == source)
            result = await session.execute(stmt)
            return list(result.scalars().all())

    async def get_pool_symbols(self, source: str | None = None, limit: int = 0) -> list[str]:
        """Get list of symbols from coin pool, optionally limited to top N."""
        coins = await self.get_pool(source)
        if limit > 0:
            coins = coins[:limit]
        return [c.symbol for c in coins]

    async def get_pool_count(self) -> int:
        """Get total number of symbols in pool."""
        async with async_session() as session:
            result = await session.execute(select(func.count(CoinPool.id)))
            return result.scalar() or 0

    async def start_auto_refresh(self, binance_service: BinanceService):
        """Start periodic coin pool refresh."""
        async def _loop():
            # Initial refresh with 30s timeout
            try:
                await asyncio.wait_for(self.refresh_pool(binance_service), timeout=30.0)
            except asyncio.TimeoutError:
                self._last_refresh_ok = False
                self._last_error = "Initial refresh timed out after 30s"
                logger.error("Coin pool initial refresh timed out")
            except Exception as e:
                self._last_refresh_ok = False
                self._last_error = str(e)[:200]
                logger.error("Coin pool initial refresh failed: %s", e)

            while True:
                await asyncio.sleep(self._config["refresh_interval_seconds"])
                try:
                    await self.refresh_pool(binance_service)
                except Exception as e:
                    self._last_refresh_ok = False
                    self._last_error = str(e)[:200]
                    logger.error("Coin pool refresh error: %s", e)

        if self._refresh_task is None or self._refresh_task.done():
            self._refresh_task = asyncio.create_task(_loop())

    async def stop_auto_refresh(self):
        if self._refresh_task and not self._refresh_task.done():
            self._refresh_task.cancel()
            try:
                await self._refresh_task
            except asyncio.CancelledError:
                pass


coin_pool_service = CoinPoolService()

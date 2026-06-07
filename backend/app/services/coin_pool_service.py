import asyncio
import logging
from datetime import datetime
from sqlalchemy import select, delete, func
from ..database import async_session
from ..models.coin_pool import CoinPool
from ..models.strategy import Strategy
from ..config import now_beijing
from .binance_service import BinanceService
from .position_manager import _norm_sym

logger = logging.getLogger(__name__)


def sort_coin_pool_by_price_change(coins: list[CoinPool], source: str | None = None) -> list[CoinPool]:
    """按涨跌幅排序：涨幅榜降序，跌幅榜升序；both 时涨幅段在前。"""
    if not coins:
        return coins
    src = (source or "").lower()
    if src == "gainers":
        return sorted(coins, key=lambda c: c.price_change_pct, reverse=True)
    if src == "losers":
        return sorted(coins, key=lambda c: c.price_change_pct)
    gainers = sorted(
        [c for c in coins if c.source == "gainers"],
        key=lambda c: c.price_change_pct,
        reverse=True,
    )
    losers = sorted([c for c in coins if c.source == "losers"], key=lambda c: c.price_change_pct)
    other = [c for c in coins if c.source not in ("gainers", "losers")]
    return gainers + losers + other


class CoinPoolService:
    def __init__(self):
        self._refresh_task: asyncio.Task | None = None
        self._bg_tasks: set[asyncio.Task] = set()
        self._config = {
            "refresh_interval_seconds": 3600,
            "pool_source": "both",
            "max_symbols": 30,
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

    def _fire_background(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)

    async def sync_config_from_running_strategies(self) -> None:
        """按运行中策略汇总刷新周期与入库条数；仅一条策略时同步测试用 pool_source。"""
        async with async_session() as session:
            r = await session.execute(
                select(Strategy).where(
                    Strategy.use_coin_pool.is_(True),
                    Strategy.status == "running",
                )
            )
            strategies = list(r.scalars().all())
        if not strategies:
            return
        max_top = max(s.coin_pool_top_n for s in strategies)
        min_refresh = min(s.coin_pool_refresh_seconds for s in strategies)
        patch: dict = {
            "max_symbols": max(max_top, self._config.get("max_symbols", 30)),
            "refresh_interval_seconds": min_refresh,
        }
        if len(strategies) == 1:
            from .strategy_flags import normalize_coin_pool_source

            patch["pool_source"] = normalize_coin_pool_source(
                strategies[0].coin_pool_source
            )
        self.update_config(**patch)

    async def _limit_for_source(self, source: str) -> int:
        """该来源下运行策略所需的最大 top_n；无运行策略时用 max_symbols。"""
        async with async_session() as session:
            stmt = select(Strategy.coin_pool_top_n).where(
                Strategy.use_coin_pool.is_(True),
                Strategy.status == "running",
            )
            if source == "both":
                stmt = stmt.where(Strategy.coin_pool_source == "both")
            elif source in ("gainers", "losers"):
                stmt = stmt.where(Strategy.coin_pool_source.in_([source, "both"]))
            else:
                stmt = stmt.where(Strategy.coin_pool_source == source)
            tops = [row[0] for row in (await session.execute(stmt)).all()]
        if tops:
            return max(tops)
        return int(self._config.get("max_symbols", 30))

    async def _running_pool_sources(self) -> list[str]:
        """运行中策略需要的选币池来源（去重）；无运行策略时用全局配置。"""
        async with async_session() as session:
            r = await session.execute(
                select(Strategy.coin_pool_source).where(
                    Strategy.use_coin_pool.is_(True),
                    Strategy.status == "running",
                ).distinct()
            )
            rows = [row[0] for row in r.all() if row[0]]
        if not rows:
            return [self._config["pool_source"]]
        out: list[str] = []
        for s in rows:
            if s == "both":
                if "both" not in out:
                    out.append("both")
            elif s not in out:
                out.append(s)
        if not out:
            out = ["both"]
        return out

    async def refresh_pool(
        self,
        binance_service: BinanceService,
        source: str | None = None,
        *,
        limit: int | None = None,
    ):
        """拉取并写入指定来源的选币池；只替换该来源行，与其它来源互不影响。"""
        source = source or self._config["pool_source"]
        from .strategy_flags import normalize_coin_pool_source

        source = normalize_coin_pool_source(source)
        if limit is None:
            limit = await self._limit_for_source(source)
        movers = await binance_service.fetch_top_movers(
            source=source,
            limit=limit,
        )
        if not movers:
            self._last_refresh_ok = False
            self._last_error = f"选币池[{source}]返回空列表"
            logger.warning(
                "选币池[%s]拉取结果为空，保留该来源旧数据与其它来源",
                source,
            )
            return
        async with async_session() as session:
            if source == "both":
                await session.execute(
                    delete(CoinPool).where(CoinPool.source.in_(["gainers", "losers"]))
                )
            else:
                await session.execute(delete(CoinPool).where(CoinPool.source == source))
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
        logger.info("Coin pool refreshed [%s]: %d symbols", source, len(movers))

    async def refresh_pool_sources(
        self, binance_service: BinanceService, sources: list[str] | None = None
    ) -> None:
        """只刷新当前运行策略用到的来源（gainers / losers / both）。"""
        await self.sync_config_from_running_strategies()
        sources = sources or await self._running_pool_sources()
        for src in sources:
            timeout = 90.0
            try:
                await asyncio.wait_for(
                    self.refresh_pool(binance_service, source=src),
                    timeout=timeout,
                )
            except asyncio.TimeoutError:
                self._last_refresh_ok = False
                self._last_error = f"选币池[{src}]刷新超时({int(timeout)}s)"
                logger.error("Coin pool refresh timed out for source=%s", src)
            except Exception as e:
                self._last_refresh_ok = False
                self._last_error = str(e)[:200]
                logger.error("Coin pool refresh error source=%s: %s", src, e)
        from .kline_prewarm import prewarm_running_strategies_klines
        from .leverage_prewarm import prewarm_running_strategies_leverage

        self._fire_background(prewarm_running_strategies_leverage())
        self._fire_background(prewarm_running_strategies_klines())

    async def get_pool(self, source: str | None = None) -> list[CoinPool]:
        """Get current coin pool from database.

        Args:
            source: 'gainers', 'losers', 'both', or None (all).
                    'both' returns all coins without source filtering.
        """
        async with async_session() as session:
            stmt = select(CoinPool).order_by(CoinPool.rank)
            if source == "both":
                stmt = stmt.where(CoinPool.source.in_(["gainers", "losers"]))
            elif source:
                stmt = stmt.where(CoinPool.source == source)
            result = await session.execute(stmt)
            coins = list(result.scalars().all())
            return sort_coin_pool_by_price_change(coins, source)

    async def get_effective_pool_entries(
        self,
        *,
        source: str | None = None,
        limit: int = 0,
        min_volume_24h: float = 0,
        exclude_symbols_norm: set[str] | None = None,
    ) -> list[CoinPool]:
        """Strategy-facing pool: leaderboard top N → volume floor → optional symbol exclude."""
        coins = await self.get_pool(source)
        if limit > 0:
            coins = coins[:limit]
        if min_volume_24h > 0:
            coins = [c for c in coins if (c.volume_24h or 0) >= min_volume_24h]
        if exclude_symbols_norm:
            coins = [c for c in coins if _norm_sym(c.symbol) not in exclude_symbols_norm]
        return sort_coin_pool_by_price_change(coins, source)

    async def get_pool_symbols(
        self,
        source: str | None = None,
        limit: int = 0,
        min_volume_24h: float = 0,
        exclude_symbols_norm: set[str] | None = None,
    ) -> list[str]:
        """Symbol list for scheduler — same rules as get_effective_pool_entries."""
        coins = await self.get_effective_pool_entries(
            source=source,
            limit=limit,
            min_volume_24h=min_volume_24h,
            exclude_symbols_norm=exclude_symbols_norm,
        )
        return [c.symbol for c in coins]

    async def get_pool_count(self) -> int:
        """Get total number of symbols in pool."""
        async with async_session() as session:
            result = await session.execute(select(func.count(CoinPool.id)))
            return result.scalar() or 0

    async def _last_refresh_datetime_from_db(self) -> datetime | None:
        """上一次整池写入时间（各行 last_updated 在 refresh 时一致，取 max 即可）。"""
        async with async_session() as session:
            r = await session.execute(select(func.max(CoinPool.last_updated)))
            return r.scalar()

    def _seconds_until_next_refresh(self, last_dt: datetime | None) -> float:
        """距下一次「按计划」刷新应等待的秒数；无记录或已过期则 0（应尽快刷新）。"""
        interval = float(self._config["refresh_interval_seconds"])
        if last_dt is None:
            return 0.0
        elapsed = (now_beijing() - last_dt).total_seconds()
        return max(0.0, interval - elapsed)

    async def start_auto_refresh(self, binance_service: BinanceService):
        """按计划间隔循环刷新；重启后根据库内上次刷新时间补齐等待，避免整点相位被重置。"""

        async def _loop():
            while True:
                last_dt = await self._last_refresh_datetime_from_db()
                delay = self._seconds_until_next_refresh(last_dt)
                if delay > 0:
                    if last_dt is not None:
                        self._last_refresh_time = last_dt.timestamp()
                    logger.info(
                        "选币池将在 %.0f 秒后刷新（与重启前间隔对齐，周期=%ds）",
                        delay,
                        int(self._config["refresh_interval_seconds"]),
                    )
                    await asyncio.sleep(delay)
                try:
                    await self.refresh_pool_sources(binance_service)
                except Exception as e:
                    self._last_refresh_ok = False
                    self._last_error = str(e)[:200]
                    logger.error("Coin pool refresh error: %s", e)
                await asyncio.sleep(self._config["refresh_interval_seconds"])

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

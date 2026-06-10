import asyncio
import logging
from datetime import datetime, timedelta
from sqlalchemy import select, delete, func
from ..database import async_session
from ..models.coin_pool import CoinPool
from ..models.strategy import Strategy
from ..config import now_beijing
from .binance_service import BinanceService
from .position_manager import _norm_sym

logger = logging.getLogger(__name__)


def sort_coin_pool_by_price_change(coins: list[CoinPool], source: str | None = None) -> list[CoinPool]:
    """鎸夋定璺屽箙鎺掑簭锛氭定骞呮闄嶅簭锛岃穼骞呮鍗囧簭锛沚oth 鏃舵定骞呮鍦ㄥ墠銆?"""
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
        self._wake_event = asyncio.Event()
        self._refresh_lock = asyncio.Lock()
        self._config = {
            "refresh_interval_seconds": 3600,
            "pool_source": "both",
            "max_symbols": 30,
            "fetch_mode": "interval",
            "anchor_hour": 8,
            "anchor_minute": 0,
            "schedule_started_at": None,
        }
        self._last_refresh_ok: bool = False
        self._last_refresh_time: float = 0.0
        self._last_error: str = ""
        self._schedule_tolerance_seconds: float = 300.0

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

    def wake_refresh_loop(self) -> None:
        """Interrupt idle/sleep waits so a newly started strategy re-evaluates refresh timing."""
        self._wake_event.set()

    async def _interruptible_sleep(self, seconds: float) -> None:
        if seconds <= 0:
            return
        self._wake_event.clear()
        try:
            await asyncio.wait_for(self._wake_event.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass

    async def ensure_scheduled_pool_if_due(
        self, binance_service: BinanceService, strategy: Strategy
    ) -> None:
        """Refresh at anchor time when the background loop has not run yet (e.g. tick vs loop race)."""
        if not strategy.use_coin_pool:
            return
        if getattr(strategy, "coin_pool_fetch_mode", "interval") != "scheduled":
            return
        from .strategy_flags import normalize_coin_pool_source

        source = normalize_coin_pool_source(strategy.coin_pool_source)
        coins = await self.get_pool(source)
        if self._coin_pool_valid_for_strategy(strategy, coins):
            return
        last_dt = await self._last_refresh_datetime_from_db()
        delay = self._seconds_until_next_refresh(last_dt)
        if delay > 30:
            return
        async with self._refresh_lock:
            coins = await self.get_pool(source)
            if self._coin_pool_valid_for_strategy(strategy, coins):
                return
            await self.refresh_pool_sources(binance_service)

    async def sync_config_from_running_strategies(self) -> None:
        """鎸夎繍琛屼腑绛栫暐姹囨€诲埛鏂板懆鏈熶笌鍏ュ簱鏉℃暟锛涗粎涓€鏉＄瓥鐣ユ椂鍚屾娴嬭瘯鐢?pool_source銆?"""
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
        scheduled = [
            s for s in strategies
            if getattr(s, "coin_pool_fetch_mode", "interval") == "scheduled"
        ]
        patch: dict = {
            "max_symbols": max(max_top, self._config.get("max_symbols", 30)),
            "refresh_interval_seconds": min_refresh,
        }
        if scheduled:
            anchor_src = min(scheduled, key=lambda s: s.coin_pool_refresh_seconds)
            patch["fetch_mode"] = "scheduled"
            patch["anchor_hour"] = int(getattr(anchor_src, "coin_pool_anchor_hour", 8) or 8)
            patch["anchor_minute"] = int(getattr(anchor_src, "coin_pool_anchor_minute", 0) or 0)
            starts = [
                s.coin_pool_schedule_started_at
                for s in scheduled
                if getattr(s, "coin_pool_schedule_started_at", None)
            ]
            patch["schedule_started_at"] = min(starts) if starts else None
        else:
            patch["fetch_mode"] = "interval"
            patch["schedule_started_at"] = None
        if len(strategies) == 1:
            from .strategy_flags import normalize_coin_pool_source

            patch["pool_source"] = normalize_coin_pool_source(
                strategies[0].coin_pool_source
            )
        self.update_config(**patch)

    async def _limit_for_source(self, source: str) -> int:
        """璇ユ潵婧愪笅杩愯绛栫暐鎵€闇€鐨勬渶澶?top_n锛涙棤杩愯绛栫暐鏃剁敤 max_symbols銆?"""
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
        """杩愯涓瓥鐣ラ渶瑕佺殑閫夊竵姹犳潵婧愶紙鍘婚噸锛夛紱鏃犺繍琛岀瓥鐣ユ椂鐢ㄥ叏灞€閰嶇疆銆?"""
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
        """鎷夊彇骞跺啓鍏ユ寚瀹氭潵婧愮殑閫夊竵姹狅紱鍙浛鎹㈣鏉ユ簮琛岋紝涓庡叾瀹冩潵婧愪簰涓嶅奖鍝嶃€?"""
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
                "閫夊竵姹燵%s]鎷夊彇缁撴灉涓虹┖锛屼繚鐣欒鏉ユ簮鏃ф暟鎹笌鍏跺畠鏉ユ簮",
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
        """鍙埛鏂板綋鍓嶈繍琛岀瓥鐣ョ敤鍒扮殑鏉ユ簮锛坓ainers / losers / both锛夈€?"""
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
                self._last_error = f"閫夊竵姹燵{src}]鍒锋柊瓒呮椂({int(timeout)}s)"
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

    async def get_pool_for_strategy(
        self,
        *,
        source: str | None = None,
        strategy: Strategy,
    ) -> list[CoinPool]:
        """Raw pool for a strategy page: respect scheduled validity + cap at the strategy's top_n.

        "鏈繃婊? 浠呮寚鏈簲鐢ㄦ垚浜ら噺/TradFi/涓嬫灦/涓绘祦/璐圭巼绛夎繃婊わ紝浣嗕粛闄愬畾鍦ㄦ鍗曞墠 top_n 鍐咃紝
        涓庣瓥鐣ャ€屾姄鍙栨鍗曞墠 N銆嶈缃竴鑷达紝閬垮厤鍑虹幇澶氫簬 N 涓殑鍥版儜銆?
        """
        coins = await self.get_pool(source)
        if not self._coin_pool_valid_for_strategy(strategy, coins):
            return []
        top_n = int(getattr(strategy, "coin_pool_top_n", 0) or 0)
        if top_n > 0:
            coins = coins[:top_n]
        return coins

    @staticmethod
    def _first_anchor_at_or_after(
        started: datetime,
        anchor_hour: int,
        anchor_minute: int,
        tolerance_seconds: float = 0.0,
    ) -> datetime:
        """璁″垝鐢熸晥鍚庣涓€涓敋鐐规暣鐐癸紙濡傞厤缃簬 02:30銆侀敋鐐?08:00 鈫?褰撴棩 08:00锛夈€?"""
        anchor_day = started.replace(hour=anchor_hour, minute=anchor_minute, second=0, microsecond=0)
        if started <= anchor_day + timedelta(seconds=tolerance_seconds):
            return anchor_day
        return anchor_day + timedelta(days=1)

    @staticmethod
    def _next_slot_from_base(base: datetime, after: datetime, interval: float) -> datetime:
        """浠?base 涓洪涓椂鍒汇€佹瘡 interval 绉掍竴鏍硷紝杩斿洖涓ユ牸鏅氫簬 after 鐨勪笅涓€鏍笺€?"""
        if after < base:
            return base
        elapsed = (after - base).total_seconds()
        n = int(elapsed // interval) + 1
        return base + timedelta(seconds=n * interval)

    def _is_scheduled_refresh_time(
        self,
        dt: datetime,
        anchor_hour: int,
        anchor_minute: int,
        interval: float,
        tolerance: float = 300.0,
    ) -> bool:
        """Return True when a refresh timestamp belongs to the scheduled grid.

        Only accept refreshes after a scheduled slot. A pool written shortly before
        the slot (for example 02:56 for a 03:00 schedule) is still an old pool.
        """
        anchor_today = dt.replace(hour=anchor_hour, minute=anchor_minute, second=0, microsecond=0)
        base = anchor_today if anchor_today <= dt else anchor_today - timedelta(days=1)
        elapsed = (dt - base).total_seconds()
        remainder = elapsed % interval
        return remainder <= tolerance

    def _coin_pool_valid_for_strategy(self, strategy: Strategy, coins: list[CoinPool]) -> bool:
        """Scheduled: 绗竴娆″繀椤荤瓑鍒拌鍒掔敓鏁堝悗鐨勯涓敋鐐癸紝涔嬪悗鎸夐棿闅旇繛缁€?

        鍗炽€屾寚瀹氭椂闂村紑閫夈€嶈涔夆€斺€旈涓€夊竵鏃跺埢 = schedule_started_at 涔嬪悗鐨勭涓€涓?anchor_hour 鏁寸偣锛?
        鍦ㄨ鏃跺埢涔嬪墠鍐欏叆鐨勬睜(鍚噷鏅?涓€寰嬭涓烘棫姹?鏃犳晥銆傝棣栫偣涔嬪悗鍒欐寜 interval 缃戞牸鎸佺画鏈夋晥銆?
        """
        if getattr(strategy, "coin_pool_fetch_mode", "interval") != "scheduled":
            return True
        if not coins:
            return False
        last_dt = max((c.last_updated for c in coins if c.last_updated), default=None)
        if last_dt is None:
            return False
        anchor = int(getattr(strategy, "coin_pool_anchor_hour", 8) or 8)
        anchor_minute = int(getattr(strategy, "coin_pool_anchor_minute", 0) or 0)
        interval = float(strategy.coin_pool_refresh_seconds or self._config["refresh_interval_seconds"])
        started = getattr(strategy, "coin_pool_schedule_started_at", None) or getattr(
            strategy, "started_at", None
        )
        if started is None:
            # 鏃犺鍒掔敓鏁堝弬鐓?鍘嗗彶鏁版嵁)锛氶€€鍥為敋鐐圭綉鏍煎垽瀹氾紝閬垮厤璇激杩愯涓瓥鐣?
            return self._is_scheduled_refresh_time(last_dt, anchor, anchor_minute, interval)
        tolerance = self._schedule_tolerance_seconds
        first_slot = self._first_anchor_at_or_after(
            started, anchor, anchor_minute, tolerance_seconds=tolerance
        )
        if last_dt < first_slot:
            return False
        return True

    async def get_effective_pool_entries(
        self,
        *,
        source: str | None = None,
        limit: int = 0,
        min_volume_24h: float = 0,
        exclude_symbols_norm: set[str] | None = None,
        strategy: Strategy | None = None,
    ) -> list[CoinPool]:
        """Strategy-facing pool: leaderboard top N 鈫?volume floor 鈫?optional symbol exclude."""
        coins = await self.get_pool(source)
        if strategy is not None and not self._coin_pool_valid_for_strategy(strategy, coins):
            return []
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
        strategy: Strategy | None = None,
    ) -> list[str]:
        """Symbol list for scheduler 鈥?same rules as get_effective_pool_entries."""
        coins = await self.get_effective_pool_entries(
            source=source,
            limit=limit,
            min_volume_24h=min_volume_24h,
            exclude_symbols_norm=exclude_symbols_norm,
            strategy=strategy,
        )
        return [c.symbol for c in coins]

    async def get_pool_count(self) -> int:
        """Get total number of symbols in pool."""
        async with async_session() as session:
            result = await session.execute(select(func.count(CoinPool.id)))
            return result.scalar() or 0

    async def _last_refresh_datetime_from_db(self) -> datetime | None:
        """涓婁竴娆℃暣姹犲啓鍏ユ椂闂达紙鍚勮 last_updated 鍦?refresh 鏃朵竴鑷达紝鍙?max 鍗冲彲锛夈€?"""
        async with async_session() as session:
            r = await session.execute(select(func.max(CoinPool.last_updated)))
            return r.scalar()

    async def _has_running_pool_strategies(self) -> bool:
        async with async_session() as session:
            r = await session.execute(
                select(Strategy.id).where(
                    Strategy.use_coin_pool.is_(True),
                    Strategy.status == "running",
                ).limit(1)
            )
            return r.scalar() is not None

    async def has_running_scheduled_strategies(self) -> bool:
        async with async_session() as session:
            r = await session.execute(
                select(Strategy.id).where(
                    Strategy.use_coin_pool.is_(True),
                    Strategy.status == "running",
                    Strategy.coin_pool_fetch_mode == "scheduled",
                ).limit(1)
            )
            return r.scalar() is not None

    def _next_anchor_slot_after(
        self, after: datetime, anchor_hour: int, anchor_minute: int, interval: float,
    ) -> datetime:
        """閿氱偣鏃跺埢璧锋瘡闅?interval 绉掔殑涓嬩竴娆″紑閫夋椂鍒伙紙涓ユ牸鏅氫簬 after锛夈€?

        缃戞牸璺ㄦ棩杩炵画锛氬噷鏅ㄦ湭鍒板綋鏃ラ敋鐐规椂锛屼粛浠庢槰鏃ラ敋鐐规帹绠楋紙濡?8:00/4h 鈫?鈥?0:00/0:00/4:00/8:00锛夈€?
        """
        anchor_today = after.replace(hour=anchor_hour, minute=anchor_minute, second=0, microsecond=0)
        base = anchor_today if anchor_today <= after else anchor_today - timedelta(days=1)
        elapsed = (after - base).total_seconds()
        n = int(elapsed // interval) + 1
        return base + timedelta(seconds=n * interval)

    def _seconds_until_next_refresh(self, last_dt: datetime | None) -> float:
        """璺濅笅涓€娆℃寜璁″垝鍒锋柊搴旂瓑寰呯殑绉掓暟锛涙湁鍘嗗彶璁板綍鏃朵笌涓婃閫夊竵鏃堕棿瀵归綈锛岄噸鍚笉绔嬪嵆閲嶉€夈€?"""
        interval = float(self._config["refresh_interval_seconds"])
        now = now_beijing()
        mode = self._config.get("fetch_mode", "interval")

        if last_dt is None:
            if mode == "scheduled":
                anchor = int(self._config.get("anchor_hour", 8))
                anchor_minute = int(self._config.get("anchor_minute", 0))
                started = self._config.get("schedule_started_at")
                if started is not None:
                    # 鎸囧畾鏃堕棿寮€閫変笖灏氭棤鍘嗗彶姹狅細绛夊埌璁″垝鐢熸晥鍚庣殑棣栦釜閿氱偣鍐嶉娆￠€夊竵
                    first_slot = self._first_anchor_at_or_after(
                        started,
                        anchor,
                        anchor_minute,
                        tolerance_seconds=self._schedule_tolerance_seconds,
                    )
                    if now < first_slot:
                        return max(0.0, (first_slot - now).total_seconds())
                    return 0.0
                next_slot = self._next_anchor_slot_after(
                    now - timedelta(seconds=1), anchor, anchor_minute, interval,
                )
                return max(0.0, (next_slot - now).total_seconds())
            return 0.0

        if mode == "interval":
            elapsed = (now - last_dt).total_seconds()
            return max(0.0, interval - elapsed)

        anchor = int(self._config.get("anchor_hour", 8))
        anchor_minute = int(self._config.get("anchor_minute", 0))
        started = self._config.get("schedule_started_at")
        if started is not None:
            # 鎸囧畾鏃堕棿寮€閫夛細棣栦釜閫夊竵鏃跺埢涓鸿鍒掔敓鏁堝悗鐨勭涓€涓敋鐐癸紝涔嬪墠涓嶅埛鏂?
            first_slot = self._first_anchor_at_or_after(
                started,
                anchor,
                anchor_minute,
                tolerance_seconds=self._schedule_tolerance_seconds,
            )
            if now < first_slot:
                return max(0.0, (first_slot - now).total_seconds())
            min_wait = max(0.0, interval - (now - last_dt).total_seconds())
            earliest = now + timedelta(seconds=min_wait)
            next_slot = self._next_slot_from_base(
                first_slot, earliest - timedelta(seconds=1), interval,
            )
            return max(0.0, (next_slot - now).total_seconds())

        elapsed = (now - last_dt).total_seconds()
        min_wait = max(0.0, interval - elapsed)
        earliest = now + timedelta(seconds=min_wait)
        next_slot = self._next_anchor_slot_after(
            earliest - timedelta(seconds=1), anchor, anchor_minute, interval,
        )
        return max(0.0, (next_slot - now).total_seconds())

    async def start_auto_refresh(self, binance_service: BinanceService):
        """鎸夎鍒掗棿闅斿惊鐜埛鏂帮紱閲嶅惎/鏀瑰弬鍚庢牴鎹簱鍐呬笂娆￠€夊竵鏃堕棿琛ラ綈绛夊緟锛屼笉绔嬪嵆閲嶉€夈€?"""

        async def _loop():
            while True:
                await self.sync_config_from_running_strategies()
                if not await self._has_running_pool_strategies():
                    await self._interruptible_sleep(60)
                    continue
                last_dt = await self._last_refresh_datetime_from_db()
                delay = self._seconds_until_next_refresh(last_dt)
                if delay > 0:
                    if last_dt is not None:
                        self._last_refresh_time = last_dt.timestamp()
                    mode = self._config.get("fetch_mode", "interval")
                    if mode == "scheduled":
                        logger.info(
                            "选币池将在 %.0f 秒后刷新（scheduled 锚点=%02d:%02d 周期=%ds）",
                            delay,
                            int(self._config.get("anchor_hour", 8)),
                            int(self._config.get("anchor_minute", 0)),
                            int(self._config["refresh_interval_seconds"]),
                        )
                    else:
                        logger.info(
                            "选币池将在 %.0f 秒后刷新（与上次选币对齐，周期=%ds）",
                            delay,
                            int(self._config["refresh_interval_seconds"]),
                        )
                    await self._interruptible_sleep(delay)
                try:
                    async with self._refresh_lock:
                        await self.refresh_pool_sources(binance_service)
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







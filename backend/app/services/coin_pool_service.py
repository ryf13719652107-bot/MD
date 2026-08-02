import asyncio
import logging
from datetime import datetime, timedelta
from sqlalchemy import select, delete, func
from ..database import async_session
from ..models.coin_pool import CoinPool
from ..models.strategy import Strategy
from ..config import now_beijing
from .position_manager import _norm_sym
from .exchange_factory import normalize_exchange_id

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


_DEFAULT_POOL_CONFIG = {
    "refresh_interval_seconds": 3600,
    "pool_source": "both",
    "max_symbols": 30,
    "fetch_mode": "interval",
    "anchor_hour": 8,
    "anchor_minute": 0,
    "schedule_started_at": None,
}


class CoinPoolService:
    def __init__(self):
        self._refresh_task: asyncio.Task | None = None
        self._bg_tasks: set[asyncio.Task] = set()
        self._wake_event = asyncio.Event()
        self._refresh_lock = asyncio.Lock()
        # _config：兼容旧 API（=币安侧）；真正刷新用 _configs_by_exchange
        self._config = dict(_DEFAULT_POOL_CONFIG)
        self._configs_by_exchange: dict[str, dict] = {}
        self._status_by_exchange: dict[str, dict] = {}
        self._schedule_tolerance_seconds: float = 300.0

    @property
    def config(self) -> dict:
        return self.config_for("binance")

    def config_for(self, exchange: str) -> dict:
        ex = normalize_exchange_id(exchange)
        if ex not in self._configs_by_exchange:
            # 币安侧与遗留 _config 共用同一 dict，避免两份漂移
            if ex == "binance":
                self._configs_by_exchange[ex] = self._config
            else:
                self._configs_by_exchange[ex] = dict(_DEFAULT_POOL_CONFIG)
        return self._configs_by_exchange[ex]

    @property
    def status(self) -> dict:
        return self.status_for("binance")

    def status_for(self, exchange: str) -> dict:
        ex = normalize_exchange_id(exchange)
        st = self._status_by_exchange.get(ex)
        if not st:
            return {
                "last_refresh_ok": False,
                "last_refresh_time": 0.0,
                "last_error": "",
            }
        return dict(st)

    def _set_refresh_status(
        self, exchange: str, *, ok: bool, error: str = ""
    ) -> None:
        ex = normalize_exchange_id(exchange)
        prev = self._status_by_exchange.get(ex) or {}
        self._status_by_exchange[ex] = {
            "last_refresh_ok": ok,
            "last_refresh_time": (
                now_beijing().timestamp() if ok else float(prev.get("last_refresh_time") or 0.0)
            ),
            "last_error": error or "",
        }

    def update_config(self, *, exchange: str = "binance", **kwargs):
        """按交易所更新选币配置；默认 binance 以兼容旧调用。"""
        cfg = self.config_for(exchange)
        cfg.update(kwargs)
        if normalize_exchange_id(exchange) == "binance":
            self._config.update(cfg)

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
        self, exchange_service, strategy: Strategy, *, exchange: str = "binance"
    ) -> None:
        """若 scheduled 池到期且后台循环未刷：只投递后台刷新，不阻塞调用方（接针价流）。"""
        if not strategy.use_coin_pool:
            return
        if getattr(strategy, "coin_pool_fetch_mode", "interval") != "scheduled":
            return
        from .strategy_flags import normalize_coin_pool_source

        ex = normalize_exchange_id(exchange)
        source = normalize_coin_pool_source(strategy.coin_pool_source)
        coins = await self.get_pool(source, exchange=ex)
        if self._coin_pool_valid_for_strategy(strategy, coins):
            return
        last_dt = await self._last_refresh_datetime_from_db(exchange=ex)
        delay = self._seconds_until_next_refresh(last_dt, exchange=ex)
        if delay > 30:
            return
        if self._refresh_lock.locked():
            return
        self._fire_background(self._refresh_exchange_locked(exchange_service, ex))

    async def _refresh_exchange_locked(self, exchange_service, exchange: str) -> None:
        ex = normalize_exchange_id(exchange)
        if self._refresh_lock.locked():
            return
        async with self._refresh_lock:
            await self.refresh_pool_sources(exchange_service, exchange=ex)

    @staticmethod
    def _patch_from_strategies(strategies: list[Strategy], base: dict) -> dict:
        """从同一交易所的运行策略聚合刷新配置。"""
        max_top = max(s.coin_pool_top_n for s in strategies)
        min_refresh = min(s.coin_pool_refresh_seconds for s in strategies)
        scheduled = [
            s for s in strategies
            if getattr(s, "coin_pool_fetch_mode", "interval") == "scheduled"
        ]
        patch: dict = {
            "max_symbols": max(max_top, base.get("max_symbols", 30)),
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
        return patch

    async def sync_config_from_running_strategies(self) -> None:
        """按交易所分别聚合运行中策略的选币配置（币安/GATE 互不影响）。"""
        from collections import defaultdict

        from ..models.account import Account

        async with async_session() as session:
            r = await session.execute(
                select(Strategy, Account)
                .join(Account, Account.id == Strategy.account_id)
                .where(
                    Strategy.use_coin_pool.is_(True),
                    Strategy.status == "running",
                )
            )
            rows = list(r.all())
        if not rows:
            return

        by_ex: dict[str, list[Strategy]] = defaultdict(list)
        for strategy, account in rows:
            by_ex[normalize_exchange_id(getattr(account, "exchange", None))].append(
                strategy
            )

        for ex, strategies in by_ex.items():
            cfg = self.config_for(ex)
            cfg.update(self._patch_from_strategies(strategies, cfg))

        # API 兼容：全局 _config 跟随币安侧（仅币安策略时行为与改前一致）
        if "binance" in by_ex:
            self._config.update(self.config_for("binance"))

    async def _limit_for_source(self, source: str, *, exchange: str = "binance") -> int:
        """该来源下运行策略所需的最大 top_n；无运行策略时用 max_symbols。"""
        from ..models.account import Account

        ex = normalize_exchange_id(exchange)
        async with async_session() as session:
            from sqlalchemy import func

            stmt = (
                select(Strategy.coin_pool_top_n)
                .join(Account, Account.id == Strategy.account_id)
                .where(
                    Strategy.use_coin_pool.is_(True),
                    Strategy.status == "running",
                    func.coalesce(Account.exchange, "binance") == ex,
                )
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
        return int(self.config_for(ex).get("max_symbols", 30))

    async def _running_pool_sources(self, *, exchange: str = "binance") -> list[str]:
        """运行中策略需要的选币池来源（去重）；无运行策略时用该交易所配置。"""
        from ..models.account import Account

        ex = normalize_exchange_id(exchange)
        async with async_session() as session:
            from sqlalchemy import func

            r = await session.execute(
                select(Strategy.coin_pool_source)
                .join(Account, Account.id == Strategy.account_id)
                .where(
                    Strategy.use_coin_pool.is_(True),
                    Strategy.status == "running",
                    func.coalesce(Account.exchange, "binance") == ex,
                )
                .distinct()
            )
            rows = [row[0] for row in r.all() if row[0]]
        if not rows:
            return [self.config_for(ex)["pool_source"]]
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

    async def _running_exchanges(self) -> list[str]:
        from ..models.account import Account

        async with async_session() as session:
            r = await session.execute(
                select(Account.exchange)
                .join(Strategy, Strategy.account_id == Account.id)
                .where(
                    Strategy.use_coin_pool.is_(True),
                    Strategy.status == "running",
                )
                .distinct()
            )
            rows = [normalize_exchange_id(row[0]) for row in r.all()]
        return sorted(set(rows)) or ["binance"]

    async def refresh_pool(
        self,
        exchange_service,
        source: str | None = None,
        *,
        limit: int | None = None,
        exchange: str = "binance",
    ):
        """拉取并写入指定交易所+来源的选币池。"""
        ex = normalize_exchange_id(exchange)
        client_ex = normalize_exchange_id(
            getattr(exchange_service, "exchange_id", None) or exchange
        )
        if client_ex != ex:
            logger.error(
                "Coin pool refresh exchange mismatch: param=%s client=%s — using client",
                ex,
                client_ex,
            )
            ex = client_ex
        source = source or self.config_for(ex)["pool_source"]
        from .strategy_flags import normalize_coin_pool_source

        source = normalize_coin_pool_source(source)
        if limit is None:
            limit = await self._limit_for_source(source, exchange=ex)
        movers = await exchange_service.fetch_top_movers(source=source, limit=limit)
        if not movers:
            self._set_refresh_status(
                ex, ok=False, error=f"选币池[{ex}/{source}]返回空列表"
            )
            logger.warning("选币池[%s/%s]拉取结果为空，保留该来源旧数据", ex, source)
            return
        async with async_session() as session:
            if source == "both":
                await session.execute(
                    delete(CoinPool).where(
                        CoinPool.exchange == ex,
                        CoinPool.source.in_(["gainers", "losers"]),
                    )
                )
            else:
                await session.execute(
                    delete(CoinPool).where(
                        CoinPool.exchange == ex,
                        CoinPool.source == source,
                    )
                )
            for item in movers:
                session.add(
                    CoinPool(
                        exchange=ex,
                        symbol=item["symbol"],
                        rank=item["rank"],
                        price_change_pct=item["price_change_pct"],
                        volume_24h=item.get("volume_24h", 0),
                        source=item["source"],
                        added_at=now_beijing(),
                        last_updated=now_beijing(),
                    )
                )
            await session.commit()
        self._set_refresh_status(ex, ok=True)
        logger.info("Coin pool refreshed [%s/%s]: %d symbols", ex, source, len(movers))

    async def refresh_pool_sources(
        self,
        exchange_service,
        sources: list[str] | None = None,
        *,
        exchange: str = "binance",
    ) -> None:
        ex = normalize_exchange_id(exchange)
        await self.sync_config_from_running_strategies()
        sources = sources or await self._running_pool_sources(exchange=ex)
        for src in sources:
            timeout = 90.0
            try:
                await asyncio.wait_for(
                    self.refresh_pool(exchange_service, source=src, exchange=ex),
                    timeout=timeout,
                )
            except asyncio.TimeoutError:
                self._set_refresh_status(
                    ex, ok=False, error=f"选币池[{ex}/{src}]刷新超时({int(timeout)}s)"
                )
                logger.error("Coin pool refresh timed out for exchange=%s source=%s", ex, src)
            except Exception as e:
                self._set_refresh_status(ex, ok=False, error=str(e)[:200])
                logger.error("Coin pool refresh error exchange=%s source=%s: %s", ex, src, e)
        from .kline_prewarm import prewarm_running_strategies_klines
        from .leverage_prewarm import prewarm_running_strategies_leverage

        self._fire_background(prewarm_running_strategies_leverage())
        self._fire_background(prewarm_running_strategies_klines())

    async def refresh_all_running_exchanges(self) -> None:
        from .exchange_factory import get_public_exchange

        for ex in await self._running_exchanges():
            client = await get_public_exchange(ex)
            await self.refresh_pool_sources(client, exchange=ex)

    async def get_pool(
        self, source: str | None = None, *, exchange: str = "binance"
    ) -> list[CoinPool]:
        ex = normalize_exchange_id(exchange)
        async with async_session() as session:
            stmt = select(CoinPool).where(CoinPool.exchange == ex).order_by(CoinPool.rank)
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
        exchange: str | None = None,
    ) -> list[CoinPool]:
        from ..models.account import Account

        if exchange is None:
            async with async_session() as session:
                acc = await session.get(Account, strategy.account_id)
                ex = normalize_exchange_id(getattr(acc, "exchange", None) if acc else None)
        else:
            ex = normalize_exchange_id(exchange)
        coins = await self.get_pool(source, exchange=ex)
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
        exchange: str = "binance",
    ) -> list[CoinPool]:
        """Strategy-facing pool: top N -> volume -> exclude."""
        ex = normalize_exchange_id(exchange)
        coins = await self.get_pool(source, exchange=ex)
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
        exchange: str = "binance",
    ) -> list[str]:
        coins = await self.get_effective_pool_entries(
            source=source,
            limit=limit,
            min_volume_24h=min_volume_24h,
            exclude_symbols_norm=exclude_symbols_norm,
            strategy=strategy,
            exchange=exchange,
        )
        return [c.symbol for c in coins]

    async def get_pool_count(self, *, exchange: str | None = None) -> int:
        async with async_session() as session:
            stmt = select(func.count(CoinPool.id))
            if exchange is not None:
                stmt = stmt.where(CoinPool.exchange == normalize_exchange_id(exchange))
            result = await session.execute(stmt)
            return result.scalar() or 0

    async def _last_refresh_datetime_from_db(
        self, *, exchange: str | None = None
    ) -> datetime | None:
        async with async_session() as session:
            stmt = select(func.max(CoinPool.last_updated))
            if exchange is not None:
                stmt = stmt.where(CoinPool.exchange == normalize_exchange_id(exchange))
            r = await session.execute(stmt)
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

    async def has_running_scheduled_strategies(
        self, *, exchange: str | None = None
    ) -> bool:
        """是否存在 scheduled 运行策略；可按交易所过滤，避免跨所拦截手动刷新。"""
        from ..models.account import Account

        async with async_session() as session:
            stmt = select(Strategy.id).where(
                Strategy.use_coin_pool.is_(True),
                Strategy.status == "running",
                Strategy.coin_pool_fetch_mode == "scheduled",
            )
            if exchange is not None:
                ex = normalize_exchange_id(exchange)
                stmt = stmt.join(Account, Account.id == Strategy.account_id).where(
                    func.coalesce(Account.exchange, "binance") == ex
                )
            r = await session.execute(stmt.limit(1))
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

    def _seconds_until_next_refresh(
        self, last_dt: datetime | None, *, exchange: str | None = None
    ) -> float:
        """距下次按计划刷新应等待的秒数；exchange 指定时用该所独立配置。"""
        cfg = self.config_for(exchange) if exchange is not None else self._config
        interval = float(cfg["refresh_interval_seconds"])
        now = now_beijing()
        mode = cfg.get("fetch_mode", "interval")

        if last_dt is None:
            if mode == "scheduled":
                anchor = int(cfg.get("anchor_hour", 8))
                anchor_minute = int(cfg.get("anchor_minute", 0))
                started = cfg.get("schedule_started_at")
                if started is not None:
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

        anchor = int(cfg.get("anchor_hour", 8))
        anchor_minute = int(cfg.get("anchor_minute", 0))
        started = cfg.get("schedule_started_at")
        if started is not None:
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

    async def start_auto_refresh(self, _legacy_public_client=None):
        """按计划间隔循环刷新各运行中交易所的选币池（各交易所独立计时）。"""

        async def _loop():
            from .exchange_factory import get_public_exchange

            while True:
                await self.sync_config_from_running_strategies()
                if not await self._has_running_pool_strategies():
                    await self._interruptible_sleep(60)
                    continue

                exchanges = await self._running_exchanges()
                due: list[str] = []
                next_wait: float | None = None
                wait_ex: str | None = None
                for ex in exchanges:
                    last_dt = await self._last_refresh_datetime_from_db(exchange=ex)
                    delay = self._seconds_until_next_refresh(last_dt, exchange=ex)
                    if delay <= 0:
                        due.append(ex)
                    elif next_wait is None or delay < next_wait:
                        next_wait = delay
                        wait_ex = ex

                if not due:
                    wait = next_wait if next_wait is not None else 60.0
                    cfg = self.config_for(wait_ex or "binance")
                    mode = cfg.get("fetch_mode", "interval")
                    if mode == "scheduled":
                        logger.info(
                            "选币池[%s]将在 %.0f 秒后刷新（scheduled 锚点=%02d:%02d 周期=%ds）",
                            wait_ex or "?",
                            wait,
                            int(cfg.get("anchor_hour", 8)),
                            int(cfg.get("anchor_minute", 0)),
                            int(cfg["refresh_interval_seconds"]),
                        )
                    else:
                        logger.info(
                            "选币池[%s]将在 %.0f 秒后刷新（交易所独立配置，周期=%ds）",
                            wait_ex or "?",
                            wait,
                            int(cfg["refresh_interval_seconds"]),
                        )
                    await self._interruptible_sleep(wait)
                    continue

                # 逐所后台刷新，避免长时间占满事件循环拖住接针价流
                for ex in due:
                    try:
                        client = await get_public_exchange(ex)
                        self._fire_background(self._refresh_exchange_locked(client, ex))
                    except Exception as e:
                        self._set_refresh_status(ex, ok=False, error=str(e)[:200])
                        logger.error("Coin pool refresh schedule error [%s]: %s", ex, e)
                # 勿紧挨着再扫：给后台任务让出时间片
                await self._interruptible_sleep(5.0)

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







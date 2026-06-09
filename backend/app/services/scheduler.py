"""Strategy scheduler: lifecycle management and main execution loop."""
import asyncio
import time
import logging
from datetime import datetime, timedelta
from typing import Optional
from sqlalchemy import select
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from ..database import async_session
from ..models.strategy import Strategy
from ..models.account import Account
from ..models.bot_config import BotConfig
from ..models.position import Position
from ..models.strategy_blacklist import StrategySymbolBlacklist
from ..config import now_beijing, BEIJING_TZ
from .binance_service import (
    BinanceService,
    get_binance_service,
    get_public_binance,
    get_strategy_pool_exclude_symbols,
    filter_pool_symbols_by_funding,
    get_cached_last_funding_rates_pct,
)
from .strategy_flags import (
    exclude_delisting_enabled,
    exclude_mainstream_enabled,
    exclude_funding_enabled,
    funding_rate_threshold_pct,
    normalize_coin_pool_source,
)
from .encryption import decrypt
from .coin_pool_service import coin_pool_service
from .log_service import strategy_log_service
from .sync_service import PositionSyncService
from .position_manager import PositionManager, _norm_sym
from .tick_context import SignalCandidate, TickContext, exchange_legs_from_positions
from .account_concurrency import account_order_sem, account_sync_lock
from .backup_service import backup_trade
from .order_times import exit_time_from_order

logger = logging.getLogger(__name__)

TIMEFRAME_SECONDS = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600}
# 并发跑「收盘整轮」的策略数；≥ running 策略数可避免整点排队。多账户分散 API，可适当提高（单账户一堆策略时勿过大以防 429）。
_STRATEGY_SEMAPHORE = asyncio.Semaphore(10)
# 单策略内并发评估池内币信号的上限（拉 K 线/算信号，多为 WS 缓存命中）。
_SIGNAL_EVAL_CONCURRENCY = 20


def _exchange_leg_map_from_positions(raw_positions: list) -> dict[tuple[str, str], float]:
    """Merge exchange position rows by (symbol, side) → contracts (same as panic close)."""
    return exchange_legs_from_positions(raw_positions)


def _merge_pool_and_open_symbols(pool_symbols: list[str], open_symbols: list[str]) -> list[str]:
    """Dedupe by _norm_sym: pool first (new entries only on pool), then orphan open legs."""
    seen: set[str] = set()
    out: list[str] = []
    for s in pool_symbols:
        if not s:
            continue
        k = _norm_sym(s)
        if k in seen:
            continue
        seen.add(k)
        out.append(s)
    for s in open_symbols:
        if not s:
            continue
        k = _norm_sym(s)
        if k in seen:
            continue
        seen.add(k)
        out.append(s)
    return out


def _next_candle_close(timeframe: str) -> datetime:
    """Return the next K-line close time aligned to the timeframe boundary."""
    now = now_beijing()
    secs = TIMEFRAME_SECONDS.get(timeframe, 60)
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    elapsed = (now - midnight).total_seconds()
    return midnight + timedelta(seconds=((elapsed // secs) + 1) * secs)


class StrategyScheduler:
    def __init__(self):
        self._scheduler = AsyncIOScheduler(timezone=BEIJING_TZ)
        self._strategy_tasks: dict[int, str] = {}
        self._binance_services: dict[int, BinanceService] = {}
        self._syncer = PositionSyncService()
        self._position_mgr = PositionManager()
        self._strategy_locks: dict[int, asyncio.Lock] = {}
        self._bg_sync_tasks: set[asyncio.Task] = set()

    @property
    def scheduler(self) -> AsyncIOScheduler:
        return self._scheduler

    async def resume_running_strategies(self):
        """进程重启后：为 DB 中仍为 running 的策略重新注册 APScheduler 任务（不改 status/started_at）。"""
        async with async_session() as session:
            result = await session.execute(
                select(Strategy).where(Strategy.status == "running").order_by(Strategy.id)
            )
            rows = list(result.scalars().all())
        for s in rows:
            self._register_strategy_jobs(s.id, s.timeframe)
            strategy_log_service.info(
                s.id, "后端已重启：已恢复调度任务（仍为运行中）",
            )
            logger.info(
                "Resumed scheduler jobs for strategy %d (%s)",
                s.id, s.name,
            )

    def _register_strategy_jobs(self, strategy_id: int, timeframe: str) -> None:
        """注册主周期任务（:00 扫信号/开仓）+ 中段任务（:30 止盈检测/加仓）；不写数据库。"""
        interval_seconds = TIMEFRAME_SECONDS.get(timeframe, 60)
        next_run = _next_candle_close(timeframe)

        job_id = f"strategy_{strategy_id}"
        if self._scheduler.get_job(job_id):
            self._scheduler.remove_job(job_id)
        self._scheduler.add_job(
            self._execute_strategy,
            "interval",
            seconds=interval_seconds,
            id=job_id,
            args=[strategy_id],
            next_run_time=next_run,
        )
        self._strategy_tasks[strategy_id] = job_id

        tp_job_id = f"strategy_{strategy_id}_tp"
        if self._scheduler.get_job(tp_job_id):
            self._scheduler.remove_job(tp_job_id)
        self._scheduler.add_job(
            self._execute_tp_check,
            "interval",
            seconds=interval_seconds,
            id=tp_job_id,
            args=[strategy_id],
            next_run_time=next_run + timedelta(seconds=30),
        )

    def start(self):
        if not self._scheduler.running:
            self._scheduler.start()

    def stop(self):
        if self._scheduler.running:
            self._scheduler.shutdown(wait=False)

    async def add_strategy(self, strategy_id: int, session=None):
        if session is None:
            async with async_session() as session:
                return await self._add_strategy_impl(strategy_id, session)
        else:
            return await self._add_strategy_impl(strategy_id, session)

    async def _add_strategy_impl(self, strategy_id: int, session):
        strategy = await session.get(Strategy, strategy_id)
        if not strategy:
            logger.warning("Strategy %d not found", strategy_id)
            return False

        self._register_strategy_jobs(strategy_id, strategy.timeframe)

        strategy.status = "running"
        strategy.started_at = now_beijing()
        await session.commit()
        await session.refresh(strategy)
        logger.info("Strategy %d (%s) started", strategy_id, strategy.name)
        strategy_log_service.success(strategy_id, f"策略启动 — {strategy.name}")
        from .leverage_prewarm import prewarm_strategy_leverage_by_id
        from .kline_prewarm import prewarm_strategy_klines_by_id

        for coro in (
            prewarm_strategy_leverage_by_id(strategy_id),
            prewarm_strategy_klines_by_id(strategy_id),
        ):
            task = asyncio.create_task(coro)
            self._bg_sync_tasks.add(task)
            task.add_done_callback(self._bg_sync_tasks.discard)
        return True

    async def remove_strategy(self, strategy_id: int):
        job_id = f"strategy_{strategy_id}"
        tp_job_id = f"strategy_{strategy_id}_tp"
        self._strategy_tasks.pop(strategy_id, None)
        self._strategy_locks.pop(strategy_id, None)
        for jid in (job_id, tp_job_id):
            existing_job = self._scheduler.get_job(jid)
            if existing_job:
                self._scheduler.remove_job(jid)
        async with async_session() as session:
            strategy = await session.get(Strategy, strategy_id)
            if strategy:
                strategy.status = "stopped"
                await session.commit()
        logger.info("Strategy %d stopped", strategy_id)

    async def _get_binance_for_strategy(self, strategy: Strategy):
        if strategy.account_id in self._binance_services:
            return self._binance_services[strategy.account_id]
        async with async_session() as session:
            account = await session.get(Account, strategy.account_id)
            if not account:
                logger.warning("Strategy %d: account %d not found", strategy.id, strategy.account_id)
                return None
            api_key = decrypt(account.api_key_encrypted)
            api_secret = decrypt(account.api_secret_encrypted)
            service = await get_binance_service(api_key, api_secret, account.testnet, account.hedge_mode)
            self._binance_services[strategy.account_id] = service
            return service

    def _get_strategy_lock(self, strategy_id: int) -> asyncio.Lock:
        if strategy_id not in self._strategy_locks:
            self._strategy_locks[strategy_id] = asyncio.Lock()
        return self._strategy_locks[strategy_id]

    async def _execute_strategy(self, strategy_id: int):
        lock = self._get_strategy_lock(strategy_id)
        if lock.locked():
            logger.info("Strategy %d: skipping tick — previous tick or TP check still running", strategy_id)
            return
        async with lock:
            async with _STRATEGY_SEMAPHORE:
                await self._execute_strategy_impl(strategy_id)

    async def _execute_tp_check(self, strategy_id: int):
        """Mid-candle (+30s): TP fill check + manage only (martingale/SL); no new opens."""
        lock = self._get_strategy_lock(strategy_id)
        if lock.locked():
            logger.info("Strategy %d: skipping mid-candle — previous tick still running", strategy_id)
            return
        async with lock:
            async with _STRATEGY_SEMAPHORE:
                await self._execute_strategy_impl(strategy_id, mid_candle=True)

    async def _sync_account_background(self, auth_binance: BinanceService, account_id: int):
        lock = account_sync_lock(account_id)
        async with lock:
            try:
                await self._syncer.sync(auth_binance, account_id, auth_binance)
            except Exception as e:
                logger.error("Background sync failed for account %d: %s", account_id, e)

    async def _execute_strategy_impl(self, strategy_id: int, *, mid_candle: bool = False):
        async with async_session() as session:
            # Master switch
            switch_result = await session.execute(select(BotConfig).where(BotConfig.key == "master_switch"))
            switch = switch_result.scalar()
            if switch and switch.value == "false":
                return

            strategy = await session.get(Strategy, strategy_id)
            if not strategy or strategy.status != "running":
                return

            sync_account_id = strategy.account_id

            strategy_log_service.info(
                strategy_id,
                "中段执行开始" if mid_candle else "执行周期开始",
            )

            auth_binance = await self._get_binance_for_strategy(strategy)
            public_binance = await get_public_binance()

            if not auth_binance:
                logger.warning("Strategy %d: no auth_binance (account %d)", strategy_id, sync_account_id)
                strategy_log_service.warning(strategy_id, "无法获取API连接 — 请检查账户配置")
                return

            # Prefetch balance + positions concurrently (saves ~1 round-trip at tick start).
            auth_binance.begin_tick()
            try:
                await auth_binance.ensure_markets_loaded()
            except Exception as e:
                logger.warning("Strategy %d: ensure_markets_loaded failed: %s", strategy_id, e)
            _prefetch_balance, _prefetch_positions = await asyncio.gather(
                auth_binance.fetch_balance(),
                auth_binance.fetch_positions(),
                return_exceptions=True,
            )

            # Check margin threshold
            total_margin = 0.0
            leverage = float(strategy.leverage) if strategy.leverage else 10.0
            if auth_binance:
                try:
                    if isinstance(_prefetch_balance, Exception):
                        raise _prefetch_balance
                    balance = _prefetch_balance
                    total_margin = float(balance.get("total", {}).get("USDT", 0) or 0)
                    logger.info("Strategy %d: balance fetched — total=%.2f USDT", strategy_id, total_margin)
                    if strategy.margin_threshold > 0 and total_margin < strategy.margin_threshold:
                        strategy.status = "stopped"
                        await session.commit()
                        self._strategy_tasks.pop(strategy_id, None)
                        for jid in (f"strategy_{strategy_id}", f"strategy_{strategy_id}_tp"):
                            if self._scheduler.get_job(jid):
                                self._scheduler.remove_job(jid)
                        logger.warning("Strategy %d margin %.2f below threshold %.2f — stopping and closing all positions", strategy_id, total_margin, strategy.margin_threshold)
                        # Close ALL exchange positions (closePosition + chunked maxQty, same as panic close)
                        from ..models.trade import Trade
                        try:
                            auth_binance.pin()
                            margin_trades_to_backup: list[Trade] = []
                            max_rounds = 3
                            initial_nonempty = False
                            for round_i in range(max_rounds):
                                eps = await auth_binance.fetch_positions()
                                leg_map = _exchange_leg_map_from_positions(eps)
                                if not leg_map:
                                    break
                                if round_i == 0:
                                    initial_nonempty = True
                                logger.info(
                                    "Strategy %d margin stop: close round %d/%d, %d leg(s)",
                                    strategy_id, round_i + 1, max_rounds, len(leg_map),
                                )
                                stmt_open_batch = select(Position).where(
                                    Position.strategy_id == strategy_id,
                                    Position.closed_at.is_(None),
                                )
                                pos_batch = list((await session.execute(stmt_open_batch)).scalars().all())

                                for (sym, side), contracts in leg_map.items():
                                    order = None
                                    try:
                                        order = await auth_binance.close_position(sym, side)
                                    except Exception as ex1:
                                        logger.error("Margin stop: failed to close %s %s: %s", sym, side, ex1)
                                        continue
                                    if not order:
                                        logger.error(
                                            "Margin stop: empty order %s %s (contracts=%.6f)",
                                            sym, side, contracts,
                                        )
                                        continue
                                    exit_price = float(order.get("average", 0) or order.get("price", 0) or 0)
                                    exit_time = exit_time_from_order(order)
                                    sk = _norm_sym(sym)
                                    sd = side.lower()
                                    for lp2 in pos_batch:
                                        if lp2.closed_at is not None:
                                            continue
                                        if _norm_sym(lp2.symbol) != sk or (lp2.side or "").lower() != sd:
                                            continue
                                        ep_val = exit_price if exit_price > 0 else (lp2.mark_price or lp2.entry_price)
                                        pnl = (ep_val - lp2.entry_price) * lp2.quantity if lp2.side == "long" else (lp2.entry_price - ep_val) * lp2.quantity
                                        pct = ((ep_val - lp2.entry_price) / lp2.entry_price * 100) if lp2.side == "long" else ((lp2.entry_price - ep_val) / lp2.entry_price * 100)
                                        trade = Trade(
                                            strategy_id=strategy_id, account_id=strategy.account_id,
                                            symbol=lp2.symbol, side=lp2.side, quantity=lp2.quantity,
                                            entry_price=lp2.entry_price, exit_price=ep_val,
                                            realized_pnl=pnl, pnl_pct=round(pct, 2),
                                            entry_time=lp2.opened_at or exit_time, exit_time=exit_time,
                                            layer=lp2.layer, close_reason="margin_stop",
                                        )
                                        session.add(trade)
                                        margin_trades_to_backup.append(trade)
                                        lp2.closed_at = exit_time
                                    logger.info("Margin stop: closed %s %s (contracts=%s)", sym, side, contracts)

                            eps_final = await auth_binance.fetch_positions()
                            remaining = _exchange_leg_map_from_positions(eps_final)
                            if remaining:
                                parts = [f"{s} {d} x{c:g}" for (s, d), c in sorted(remaining.items())]
                                verify_msg = "保证金止损后校验失败，交易所仍有持仓: " + "; ".join(parts)
                                logger.error("Strategy %d: %s", strategy_id, verify_msg)
                                strategy_log_service.error(strategy_id, verify_msg)
                            elif initial_nonempty:
                                strategy_log_service.success(
                                    strategy_id, "保证金止损平仓已完成（交易所校验无持仓）"
                                )
                            await session.commit()
                            for t in margin_trades_to_backup:
                                backup_trade(t)
                        except Exception as e:
                            logger.error("Margin stop: failed to close positions for strategy %d: %s", strategy_id, e)
                        finally:
                            auth_binance.unpin()
                        return
                except Exception as e:
                    logger.error("Strategy %d: balance check failed: %s", strategy_id, e)
                    strategy_log_service.error(strategy_id, f"余额获取失败 — {e}")

            # Get symbols: coin pool or fixed (for new entries), ∪ DB open positions (manage always)
            pool_symbols: list[str] = []
            pool_entry_norms: set[str] | None = None
            pool_exclude_norm: frozenset[str] = frozenset()
            strategy_blacklist_norm: frozenset[str] = frozenset()
            pool_exclude_loaded = False

            stmt_open_syms = (
                select(Position.symbol)
                .where(Position.strategy_id == strategy_id, Position.closed_at.is_(None))
                .distinct()
            )
            open_sym_rows = (await session.execute(stmt_open_syms)).scalars().all()
            open_syms = [s for s in open_sym_rows if s]
            bl_rows = (
                await session.execute(
                    select(StrategySymbolBlacklist.symbol_norm).where(
                        StrategySymbolBlacklist.strategy_id == strategy_id
                    )
                )
            ).scalars().all()
            strategy_blacklist_norm = frozenset((s or "").upper() for s in bl_rows if s)

            if mid_candle:
                # Mid-candle: manage open legs only — no pool scan, no new entries.
                if not open_syms:
                    return
                symbols = open_syms
            else:
                if strategy.use_coin_pool:
                    try:
                        min_vol = float(getattr(strategy, "coin_pool_min_volume_24h", 0) or 0)
                        excluded = await get_strategy_pool_exclude_symbols(
                            public_binance,
                            exclude_tradefi=bool(strategy.exclude_tradefi),
                            exclude_delisting=exclude_delisting_enabled(strategy),
                            exclude_mainstream=exclude_mainstream_enabled(strategy),
                        )
                        merged_exclude = set(excluded) if excluded else set()
                        merged_exclude.update(strategy_blacklist_norm)
                        pool_exclude_norm = frozenset(merged_exclude) if merged_exclude else frozenset()
                        pool_exclude_loaded = True
                        pool_symbols = await coin_pool_service.get_pool_symbols(
                            normalize_coin_pool_source(strategy.coin_pool_source),
                            strategy.coin_pool_top_n,
                            min_volume_24h=min_vol,
                            exclude_symbols_norm=set(pool_exclude_norm) if pool_exclude_norm else None,
                            strategy=strategy,
                        )
                        if exclude_funding_enabled(strategy):
                            pool_symbols = await filter_pool_symbols_by_funding(
                                public_binance,
                                pool_symbols,
                                direction=strategy.direction,
                                threshold_pct=funding_rate_threshold_pct(strategy),
                            )
                        if min_vol > 0 and not pool_symbols:
                            logger.info(
                                "Strategy %d: coin pool empty after min_volume_24h=%.0f USDT",
                                strategy_id,
                                min_vol,
                            )
                        if not pool_symbols:
                            pool_count = await coin_pool_service.get_pool_count()
                            pool_status = coin_pool_service.status
                            logger.warning(
                                "Strategy %d: coin pool returned 0 symbols (total=%d, ok=%s)",
                                strategy_id, pool_count, pool_status["last_refresh_ok"],
                            )
                    except Exception as e:
                        logger.error("Strategy %d: coin pool query failed: %s", strategy_id, e)
                        pool_symbols = []
                elif strategy.symbol:
                    pool_symbols = [strategy.symbol]

                symbols = _merge_pool_and_open_symbols(pool_symbols, open_syms)

                if strategy.use_coin_pool:
                    pool_entry_norms = {_norm_sym(s) for s in pool_symbols if s}

                if not symbols:
                    if strategy.use_coin_pool:
                        strategy.last_signal = "no_pool"
                        strategy.last_signal_at = now_beijing()
                        await session.commit()
                        strategy_log_service.warning(strategy_id, "选币池为空且无未平持仓，无法交易")
                    else:
                        strategy_log_service.warning(strategy_id, "未设置交易对")
                    return

            if open_syms and not mid_candle:
                uniq_norm = sorted({_norm_sym(s) for s in open_syms if s})
                n = len(uniq_norm)
                max_show = 25
                head = ", ".join(uniq_norm[:max_show])
                if n > max_show:
                    summ = f"未平仓 {n} 个: {head} …(+{n - max_show})"
                else:
                    summ = f"未平仓 {n} 个: {head}"
                strategy_log_service.info(strategy_id, summ)

            # Tick-level context: filters + exchange snapshot (once per tick per account)
            exclude_norm: frozenset[str] = pool_exclude_norm
            funding_rates = None
            funding_filter_enabled = False
            if not mid_candle:
                mainstream_exclude = bool(
                    strategy.use_coin_pool and exclude_mainstream_enabled(strategy)
                )
                if (
                    getattr(strategy, "exclude_tradefi", False)
                    or exclude_delisting_enabled(strategy)
                    or mainstream_exclude
                ) and not pool_exclude_loaded:
                    excluded = await get_strategy_pool_exclude_symbols(
                        public_binance,
                        exclude_tradefi=bool(strategy.exclude_tradefi),
                        exclude_delisting=exclude_delisting_enabled(strategy),
                        exclude_mainstream=mainstream_exclude,
                    )
                    merged_exclude = set(excluded) if excluded else set()
                    merged_exclude.update(strategy_blacklist_norm)
                    exclude_norm = frozenset(merged_exclude) if merged_exclude else frozenset()
                elif strategy_blacklist_norm:
                    exclude_norm = frozenset(set(exclude_norm) | set(strategy_blacklist_norm))

                if strategy.use_coin_pool and exclude_funding_enabled(strategy):
                    funding_filter_enabled = True
                    funding_rates = await get_cached_last_funding_rates_pct(public_binance)

            # Reuse positions prefetched at tick start (concurrent with balance above).
            raw_exchange_positions: list = []
            exchange_legs: dict[tuple[str, str], float] = {}
            if isinstance(_prefetch_positions, Exception):
                logger.warning(
                    "Strategy %d: tick fetch_positions failed: %s",
                    strategy_id, _prefetch_positions,
                )
            else:
                raw_exchange_positions = _prefetch_positions or []
                exchange_legs = exchange_legs_from_positions(raw_exchange_positions)

            tick_ctx = TickContext(
                exclude_norm=exclude_norm,
                funding_rates=funding_rates,
                funding_filter_enabled=funding_filter_enabled,
                exchange_legs=exchange_legs,
                raw_exchange_positions=raw_exchange_positions,
                allow_new_norms=pool_entry_norms,
            )

            # Preload open positions per symbol (one query)
            stmt_all_open = select(Position).where(
                Position.strategy_id == strategy_id,
                Position.closed_at.is_(None),
            )

            async def _load_open_by_norm() -> dict[str, list[Position]]:
                rows = list((await session.execute(stmt_all_open)).scalars().all())
                by_norm: dict[str, list[Position]] = {}
                for p in rows:
                    k = _norm_sym(p.symbol)
                    by_norm.setdefault(k, []).append(p)
                for positions in by_norm.values():
                    positions.sort(key=lambda x: x.layer, reverse=True)
                return by_norm

            open_by_norm = await _load_open_by_norm()

            if mid_candle and auth_binance:
                try:
                    await self._position_mgr.check_tp_fills(session, strategy, auth_binance, 0)
                    await session.commit()
                    open_by_norm = await _load_open_by_norm()
                except Exception as e:
                    logger.error("Strategy %d mid-candle TP check error: %s", strategy_id, e)
                    await session.rollback()

            # Phase 1b + 2: signal scan and new opens — K-line close (:00) only.
            manage_symbols: list[tuple[str, list[Position]]] = []
            if mid_candle:
                seen_manage: set[str] = set()
                for symbol in symbols:
                    sym_key = _norm_sym(symbol)
                    if sym_key in seen_manage:
                        continue
                    open_positions = open_by_norm.get(sym_key, [])
                    if open_positions:
                        seen_manage.add(sym_key)
                        manage_symbols.append((symbol, open_positions))
            else:
                eval_symbols: list[str] = []
                for symbol in symbols:
                    sym_key = _norm_sym(symbol)
                    open_positions = open_by_norm.get(sym_key, [])
                    if open_positions:
                        manage_symbols.append((symbol, open_positions))
                        continue
                    allow_new = pool_entry_norms is None or sym_key in pool_entry_norms
                    if allow_new:
                        eval_symbols.append(symbol)

                # Concurrent signal scan (kline fetch + compute, no DB writes).
                # Start open API as soon as each symbol produces a signal; do not
                # wait for slower symbols in the pool to finish evaluating.
                if eval_symbols:
                    eval_sem = asyncio.Semaphore(_SIGNAL_EVAL_CONCURRENCY)
                    order_sem = account_order_sem(sync_account_id)

                    async def _eval_one(sym: str):
                        async with eval_sem:
                            try:
                                res = await self._position_mgr.evaluate_signal_nodb(
                                    strategy, sym, public_binance, total_margin, tick_ctx,
                                )
                                return sym, res, None
                            except Exception as e:
                                return sym, None, e

                    async def _run_open_api(cand):
                        async with order_sem:
                            return await self._position_mgr.execute_open_api(
                                cand, strategy, auth_binance, leverage,
                            )

                    pending_eval = {asyncio.create_task(_eval_one(s)) for s in eval_symbols}
                    pending_open: dict[asyncio.Task, SignalCandidate] = {}

                    while pending_eval or pending_open:
                        done_tasks, _ = await asyncio.wait(
                            set(pending_eval) | set(pending_open),
                            return_when=asyncio.FIRST_COMPLETED,
                        )
                        for done in done_tasks:
                            if done in pending_eval:
                                pending_eval.remove(done)
                                sym, cand, err = await done
                                if err is not None:
                                    logger.error(
                                        "Strategy %d: evaluate %s failed: %s",
                                        strategy_id, sym, err,
                                    )
                                    continue
                                if cand is None:
                                    continue

                                side = cand.signal.value
                                leg_key = (_norm_sym(cand.symbol), side)
                                if tick_ctx.exchange_legs.get(leg_key, 0) > 0:
                                    try:
                                        await self._position_mgr._open_from_candidate(
                                            session, strategy, cand, auth_binance, public_binance,
                                            total_margin, leverage, tick_ctx,
                                        )
                                        await session.commit()
                                    except Exception as e:
                                        logger.error(
                                            "Strategy %d: reconcile/open precheck for %s failed: %s",
                                            strategy_id, cand.symbol, e,
                                        )
                                        await session.rollback()
                                elif auth_binance:
                                    task = asyncio.create_task(_run_open_api(cand))
                                    pending_open[task] = cand
                                continue

                            cand = pending_open.pop(done)
                            try:
                                res = await done
                            except Exception as e:
                                logger.error(
                                    "Strategy %d: parallel open API for %s failed: %s",
                                    strategy_id, cand.symbol, e,
                                )
                                continue
                            if res is None:
                                continue
                            try:
                                await self._position_mgr.execute_open_db(session, strategy, res)
                                await session.commit()
                            except Exception as e:
                                logger.error(
                                    "Strategy %d: commit after open %s failed: %s",
                                    strategy_id, cand.symbol, e,
                                )
                                await session.rollback()

                    # Persist strategy.last_signal/last_rsi if no open DB commit happened last.
                    try:
                        await session.commit()
                    except Exception as e:
                        logger.error("Strategy %d: commit after signal scan failed: %s", strategy_id, e)
                        await session.rollback()

            # Phase 1a: manage existing positions (TP/martingale/SL).
            # :00 runs after opens; :30 runs manage only (after TP fill check above).
            for symbol, open_positions in manage_symbols:
                try:
                    await self._position_mgr.manage_symbol(
                        session, strategy, symbol, auth_binance, public_binance,
                        open_positions, total_margin, leverage, tick_ctx,
                    )
                    await session.commit()
                except Exception as e:
                    logger.error("Strategy %d: manage %s failed: %s", strategy_id, symbol, e)
                    await session.rollback()
                    continue

            # Sync after signal processing — non-blocking background task.
            # Keep a strong reference so the task is not GC'd mid-flight.
            if auth_binance:
                task = asyncio.create_task(
                    self._sync_account_background(auth_binance, sync_account_id)
                )
                self._bg_sync_tasks.add(task)
                task.add_done_callback(self._bg_sync_tasks.discard)


# Singleton
strategy_scheduler = StrategyScheduler()

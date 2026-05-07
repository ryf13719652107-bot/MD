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
from ..config import now_beijing, BEIJING_TZ
from .binance_service import BinanceService, get_binance_service, get_public_binance
from .encryption import decrypt
from .coin_pool_service import coin_pool_service
from .log_service import strategy_log_service
from .sync_service import PositionSyncService
from .position_manager import PositionManager, set_cooldown_lock

logger = logging.getLogger(__name__)

TIMEFRAME_SECONDS = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600}
_STRATEGY_SEMAPHORE = asyncio.Semaphore(5)


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
        set_cooldown_lock(asyncio.Lock())

    @property
    def scheduler(self) -> AsyncIOScheduler:
        return self._scheduler

    async def _reset_stale_running_strategies(self):
        async with async_session() as session:
            result = await session.execute(select(Strategy).where(Strategy.status == "running"))
            stale = result.scalars().all()
            for s in stale:
                s.status = "stopped"
                logger.info("Reset stale strategy %d (%s) to stopped", s.id, s.name)
            if stale:
                await session.commit()

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

        interval_seconds = TIMEFRAME_SECONDS.get(strategy.timeframe, 60)
        next_run = _next_candle_close(strategy.timeframe)

        # Main trading job — at candle close
        job_id = f"strategy_{strategy_id}"
        existing_job = self._scheduler.get_job(job_id)
        if existing_job:
            self._scheduler.remove_job(job_id)
        self._scheduler.add_job(
            self._execute_strategy, "interval", seconds=interval_seconds,
            id=job_id, args=[strategy_id], next_run_time=next_run,
        )
        self._strategy_tasks[strategy_id] = job_id

        # TP fill check job — offset 30s after trading, runs mid-candle
        tp_job_id = f"strategy_{strategy_id}_tp"
        existing_tp = self._scheduler.get_job(tp_job_id)
        if existing_tp:
            self._scheduler.remove_job(tp_job_id)
        from datetime import timedelta
        self._scheduler.add_job(
            self._execute_tp_check, "interval", seconds=interval_seconds,
            id=tp_job_id, args=[strategy_id],
            next_run_time=next_run + timedelta(seconds=30),
        )
        strategy.status = "running"
        strategy.started_at = now_beijing()
        await session.commit()
        await session.refresh(strategy)
        logger.info("Strategy %d (%s) started", strategy_id, strategy.name)
        strategy_log_service.success(strategy_id, f"策略启动 — {strategy.name}")
        return True

    async def remove_strategy(self, strategy_id: int):
        job_id = f"strategy_{strategy_id}"
        tp_job_id = f"strategy_{strategy_id}_tp"
        self._strategy_tasks.pop(strategy_id, None)
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

    async def _execute_strategy(self, strategy_id: int):
        async with _STRATEGY_SEMAPHORE:
            await self._execute_strategy_impl(strategy_id)

    async def _execute_tp_check(self, strategy_id: int):
        """Mid-candle TP fill check — no trading, just detect filled limit orders."""
        async with async_session() as session:
            strategy = await session.get(Strategy, strategy_id)
            if not strategy or strategy.status != "running":
                return
            auth_binance = await self._get_binance_for_strategy(strategy)
            if not auth_binance:
                return
            try:
                # Load all open positions with TP orders
                from ..models.position import Position
                stmt = select(Position).where(
                    Position.strategy_id == strategy_id,
                    Position.closed_at.is_(None),
                )
                result = await session.execute(stmt)
                open_positions = list(result.scalars().all())
                has_tp = [p for p in open_positions if p.tp_limit_order_id]
                if not has_tp:
                    return
                # Check TP fills without iterating all coin pool symbols
                for p in has_tp:
                    try:
                        await self._position_mgr.check_tp_fills(session, strategy, auth_binance, 0)
                        break  # one close per tick
                    except Exception:
                        pass
                await session.commit()
            except Exception as e:
                logger.error("Strategy %d TP check error: %s", strategy_id, e)

    async def _execute_strategy_impl(self, strategy_id: int):
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

            strategy_log_service.info(strategy_id, "执行周期开始")

            auth_binance = await self._get_binance_for_strategy(strategy)
            public_binance = await get_public_binance()

            if not auth_binance:
                logger.warning("Strategy %d: no auth_binance (account %d)", strategy_id, sync_account_id)
                strategy_log_service.warning(strategy_id, "无法获取API连接 — 请检查账户配置")
                return

            # Check margin threshold
            total_margin = 0.0
            leverage = float(strategy.leverage) if strategy.leverage else 20.0
            if auth_binance:
                try:
                    balance = await auth_binance.fetch_balance()
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
                        # Close ALL exchange positions with -1106 retry fallback
                        from ..models.position import Position as PosModel
                        from ..models.trade import Trade
                        try:
                            eps = await auth_binance.fetch_positions()
                            for ep in eps:
                                contracts = float(ep.get("contracts", 0) or 0)
                                if contracts <= 0:
                                    continue
                                sym = (ep.get("symbol") or "").replace("/", "").replace(":USDT", "")
                                side = (ep.get("side") or "").lower()
                                ps = "LONG" if side == "long" else "SHORT"
                                cs = "sell" if side == "long" else "buy"
                                order = None
                                try:
                                    order = await auth_binance.create_market_order(sym, cs, contracts, reduce_only=True, position_side=ps)
                                except Exception as ex1:
                                    if "-1106" in str(ex1):
                                        try:
                                            order = await auth_binance.create_market_order(sym, cs, contracts, reduce_only=False, position_side=ps)
                                        except Exception as ex2:
                                            logger.error("Margin stop: failed to close %s %s: %s", sym, side, ex2)
                                            continue
                                    else:
                                        logger.error("Margin stop: failed to close %s %s: %s", sym, side, ex1)
                                        continue
                                if order:
                                    exit_price = float(order.get("average", 0) or order.get("price", 0) or 0)
                                    # Mark local positions as closed and record trades
                                    stmt_pos = select(PosModel).where(
                                        PosModel.strategy_id == strategy_id, PosModel.closed_at.is_(None),
                                        PosModel.symbol == sym, PosModel.side == side
                                    )
                                    pos_result = await session.execute(stmt_pos)
                                    for lp2 in pos_result.scalars().all():
                                        ep_val = exit_price if exit_price > 0 else (lp2.mark_price or lp2.entry_price)
                                        pnl = (ep_val - lp2.entry_price) * lp2.quantity if lp2.side == "long" else (lp2.entry_price - ep_val) * lp2.quantity
                                        pct = ((ep_val - lp2.entry_price) / lp2.entry_price * 100) if lp2.side == "long" else ((lp2.entry_price - ep_val) / lp2.entry_price * 100)
                                        trade = Trade(
                                            strategy_id=strategy_id, account_id=strategy.account_id,
                                            symbol=sym, side=lp2.side, quantity=lp2.quantity,
                                            entry_price=lp2.entry_price, exit_price=ep_val,
                                            realized_pnl=pnl, pnl_pct=round(pct, 2),
                                            entry_time=lp2.opened_at, exit_time=now_beijing(),
                                            layer=lp2.layer, close_reason="margin_stop",
                                        )
                                        session.add(trade)
                                        lp2.closed_at = now_beijing()
                                    logger.info("Margin stop: closed %s %s (contracts=%s)", sym, side, contracts)
                            await session.commit()
                        except Exception as e:
                            logger.error("Margin stop: failed to close positions for strategy %d: %s", strategy_id, e)
                        return
                except Exception as e:
                    logger.error("Strategy %d: balance check failed: %s", strategy_id, e)
                    strategy_log_service.error(strategy_id, f"余额获取失败 — {e}")

            # Get symbols from coin pool or fixed
            symbols = []
            if strategy.use_coin_pool:
                try:
                    symbols = await coin_pool_service.get_pool_symbols(strategy.coin_pool_source, strategy.coin_pool_top_n)
                    if not symbols:
                        pool_count = await coin_pool_service.get_pool_count()
                        pool_status = coin_pool_service.status
                        logger.warning("Strategy %d: coin pool returned 0 symbols (total=%d, ok=%s)", strategy_id, pool_count, pool_status["last_refresh_ok"])
                except Exception as e:
                    logger.error("Strategy %d: coin pool query failed: %s", strategy_id, e)
                    return
            elif strategy.symbol:
                symbols = [strategy.symbol]

            if not symbols:
                if strategy.use_coin_pool:
                    strategy.last_signal = "no_pool"
                    strategy.last_signal_at = now_beijing()
                    await session.commit()
                    strategy_log_service.warning(strategy_id, "选币池为空，无法交易")
                else:
                    strategy_log_service.warning(strategy_id, "未设置交易对")
                return

            # Process each symbol — single transaction, any failure rolls back all
            for symbol in symbols:
                try:
                    await self._position_mgr.process_symbol(
                        session, strategy, symbol, auth_binance, public_binance,
                        total_margin, leverage,
                    )
                except Exception as e:
                    logger.error("Strategy %d: error processing %s: %s", strategy_id, symbol, e)
                    # Rollback entire tick on any symbol failure
                    await session.rollback()
                    return

            await session.commit()

            # Sync after signal processing — TP detection runs first
            if auth_binance:
                await self._syncer.sync(auth_binance, sync_account_id, auth_binance)


# Singleton
strategy_scheduler = StrategyScheduler()

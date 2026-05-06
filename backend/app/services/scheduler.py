import asyncio
import time
import logging
from datetime import datetime
from typing import Optional
from sqlalchemy import select
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from ..database import async_session
from ..models.strategy import Strategy
from ..models.position import Position
from ..models.account import Account
from .binance_service import BinanceService, get_binance_service, get_public_binance
from .strategy_engine import calculate_rsi, generate_signal, Signal
from .martingale_engine import MartingaleEngine
from .risk_manager import RiskManager
from .encryption import decrypt
from .websocket_manager import ws_manager
from .coin_pool_service import coin_pool_service

logger = logging.getLogger(__name__)

# Concurrency limit: max 5 strategies running simultaneously
_strategy_semaphore = asyncio.Semaphore(5)

# Market data cache: {(symbol, timeframe): (timestamp, klines)}
_kline_cache: dict[tuple[str, str], tuple[float, list]] = {}
_KLINE_CACHE_TTL = 1.0  # 1 second

# Signal cooldown: {(strategy_id, symbol): last_signal_time}
_signal_cooldowns: dict[tuple[int, str], float] = {}
_SIGNAL_COOLDOWN_SECONDS = 60  # Don't re-enter same symbol within 60s

# Position sync interval (per account)
_POSITION_SYNC_INTERVAL = 300  # 5 minutes


def _get_cached_klines(symbol: str, timeframe: str) -> Optional[list]:
    key = (symbol, timeframe)
    if key in _kline_cache:
        ts, data = _kline_cache[key]
        if time.time() - ts < _KLINE_CACHE_TTL:
            return data
    return None


def _set_cached_klines(symbol: str, timeframe: str, klines: list):
    _kline_cache[(symbol, timeframe)] = (time.time(), klines)


def _check_cooldown(strategy_id: int, symbol: str) -> bool:
    """Return True if in cooldown (should skip)."""
    key = (strategy_id, symbol)
    if key in _signal_cooldowns:
        if time.time() - _signal_cooldowns[key] < _SIGNAL_COOLDOWN_SECONDS:
            return True
    return False


def _set_cooldown(strategy_id: int, symbol: str):
    _signal_cooldowns[(strategy_id, symbol)] = time.time()


class StrategyScheduler:
    def __init__(self):
        self._scheduler = AsyncIOScheduler()
        self._strategy_tasks: dict[int, str] = {}
        self._binance_services: dict[int, BinanceService] = {}

    @property
    def scheduler(self) -> AsyncIOScheduler:
        return self._scheduler

    async def _reset_stale_running_strategies(self):
        """On startup, reset any strategies marked 'running' back to 'stopped'
        since their APScheduler jobs were lost when the server stopped."""
        async with async_session() as session:
            result = await session.execute(
                select(Strategy).where(Strategy.status == "running")
            )
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

    async def add_strategy(self, strategy_id: int):
        async with async_session() as session:
            strategy = await session.get(Strategy, strategy_id)
            if not strategy:
                return

            job_id = f"strategy_{strategy_id}"
            # Remove stale job if it exists (e.g., after server restart)
            existing_job = self._scheduler.get_job(job_id)
            if existing_job:
                self._scheduler.remove_job(job_id)

            self._scheduler.add_job(
                self._execute_strategy,
                "interval",
                seconds=strategy.run_interval_seconds,
                id=job_id,
                args=[strategy_id],
                next_run_time=datetime.now(),
            )
            self._strategy_tasks[strategy_id] = job_id
            strategy.status = "running"
            await session.commit()
            logger.info("Strategy %d (%s) started", strategy_id, strategy.name)

    async def remove_strategy(self, strategy_id: int):
        # Always remove by job ID pattern (not just from _strategy_tasks dict)
        job_id = f"strategy_{strategy_id}"
        self._strategy_tasks.pop(strategy_id, None)
        existing_job = self._scheduler.get_job(job_id)
        if existing_job:
            self._scheduler.remove_job(job_id)
            logger.info("APScheduler job removed for strategy %d", strategy_id)

        async with async_session() as session:
            strategy = await session.get(Strategy, strategy_id)
            if strategy:
                strategy.status = "stopped"
                await session.commit()
        logger.info("Strategy %d stopped", strategy_id)

    async def _get_binance_for_strategy(self, strategy: Strategy) -> Optional[BinanceService]:
        if strategy.account_id in self._binance_services:
            return self._binance_services[strategy.account_id]

        async with async_session() as session:
            account = await session.get(Account, strategy.account_id)
            if not account:
                logger.warning("Strategy %d: account %d not found", strategy.id, strategy.account_id)
                return None
            api_key = decrypt(account.api_key_encrypted)
            api_secret = decrypt(account.api_secret_encrypted)
            service = get_binance_service(api_key, api_secret, account.testnet, account.hedge_mode)
            self._binance_services[strategy.account_id] = service
            return service

    async def _sync_positions_with_exchange(self, auth_binance: BinanceService, account_id: int):
        """Periodically sync local position records with exchange reality for a specific account."""
        global _last_position_sync

        # Use per-account sync key to avoid skipping accounts
        sync_key = f"sync_{account_id}"
        if not hasattr(self, '_sync_timestamps'):
            self._sync_timestamps: dict[str, float] = {}
        now = time.time()
        if now - self._sync_timestamps.get(sync_key, 0) < _POSITION_SYNC_INTERVAL:
            return
        self._sync_timestamps[sync_key] = now

        try:
            exchange_positions = await auth_binance.fetch_positions()
            async with async_session() as session:
                local_positions = await session.execute(
                    select(Position).where(
                        Position.closed_at.is_(None),
                        Position.account_id == account_id,
                    )
                )
                local_positions = list(local_positions.scalars().all())

                exchange_ids = set()
                for ep in exchange_positions:
                    if float(ep.get("contracts", 0)) <= 0:
                        continue
                    exchange_ids.add(ep.get("id", ""))

                # Close local positions that no longer exist on exchange
                for lp in local_positions:
                    if lp.exchange_order_id and lp.exchange_order_id not in exchange_ids:
                        lp.closed_at = datetime.utcnow()
                        logger.warning(
                            "Position %d (%s %s) closed on exchange but not in local DB, syncing",
                            lp.id, lp.symbol, lp.side
                        )
                await session.commit()
        except Exception as e:
            logger.error("Position sync for account %d failed: %s", account_id, e)

    async def _execute_strategy(self, strategy_id: int):
        """Execute one tick of a strategy with concurrency control."""
        async with _strategy_semaphore:
            await self._execute_strategy_impl(strategy_id)

    async def _execute_strategy_impl(self, strategy_id: int):
        async with async_session() as session:
            strategy = await session.get(Strategy, strategy_id)
            if not strategy or strategy.status != "running":
                return


            strategy_log_service.info(strategy_id, "执行周期开始")

            # Sync positions periodically for this account
            if auth_binance:
                await self._sync_positions_with_exchange(auth_binance, strategy.account_id)

            # Sync AFTER signal processing so TP detection runs first
            # (moved to end of tick)
            # Check margin threshold
            total_margin = 0.0
            leverage = float(strategy.leverage) if strategy.leverage else 20.0
            if auth_binance:
                    balance = await auth_binance.fetch_balance()
                    total_margin = float(balance.get("total", {}).get("USDT", 0) or 0)
                    logger.info("Strategy %d: balance fetched — total=%.2f USDT", strategy_id, total_margin)
                    if strategy.margin_threshold > 0 and total_margin < strategy.margin_threshold:
                        logger.warning(
                            "Strategy %d stopped: margin %.2f below threshold %.2f",
                            strategy_id, total_margin, strategy.margin_threshold
                        )
                        await session.commit()
                        self._strategy_tasks.pop(strategy_id, None)
                        logger.warning("Strategy %d stopped: margin %.2f below threshold %.2f", strategy_id, total_margin, strategy.margin_threshold)
                except Exception as e:
            risk_mgr = RiskManager()

            # Determine symbols
                    strategy_log_service.error(strategy_id, f"余额获取失败 — {e}")

            # Snapshot exchange positions once — prevent duplicate opens (in-memory, no per-symbol API call)
            exchange_open_set: set[tuple[str, str]] = set()
            if auth_binance:
                try:
                    eps = await auth_binance.fetch_positions()
                        logger.warning(
                            "Strategy %d: coin pool returned 0 symbols (pool total=%d, last_ok=%s, error=%s)",
                            strategy_id, pool_count,
                            pool_status["last_refresh_ok"],
                            pool_status["last_error"][:100] if pool_status["last_error"] else "none"
                        )
                        if float(ep.get("contracts", 0) or 0) > 0:
                            sym = (ep.get("symbol") or "").replace("/", "").replace(":USDT", "")
                            side = (ep.get("side") or "").lower()
                            exchange_open_set.add((sym, side))
                except Exception:
                    pass

            # Get symbols from coin pool or fixed
            symbols = []
                    strategy.last_signal_at = datetime.utcnow()
                try:
                        pool_status = coin_pool_service.status
                        logger.warning("Strategy %d: coin pool returned 0 symbols (total=%d, ok=%s)", strategy_id, pool_count, pool_status["last_refresh_ok"])
                    logger.error("Strategy %d: coin pool query failed: %s", strategy_id, e)
                    return
                    await self._process_symbol(
                        session, strategy, symbol, auth_binance, public_binance,
                        risk_mgr, total_margin
                    )
                    strategy.last_signal = "no_pool"
                    logger.error(
                        "Strategy %d: error processing %s: %s", strategy_id, symbol, e
                    )
            # Commit all changes from this tick at once
                    strategy_log_service.warning(strategy_id, "选币池为空，无法交易")
                else:
    async def _process_symbol(
        self, session, strategy: Strategy, symbol: str,
        auth_binance: Optional[BinanceService], public_binance: BinanceService,
        risk_mgr: RiskManager, total_margin: float
    ):
        strategy_id = strategy.id

        # --- Fetch klines with cache ---
        klines = _get_cached_klines(symbol, strategy.timeframe)
        if klines is None:
            klines = await public_binance.fetch_klines(symbol, strategy.timeframe, limit=20)
            _set_cached_klines(symbol, strategy.timeframe, klines)

        rsi = calculate_rsi(klines, strategy.rsi_period)
        if rsi is None:
            return

        signal = generate_signal(rsi, strategy.direction, strategy.rsi_entry_threshold)

        # Record signal history on strategy
        strategy.last_rsi = round(rsi, 1)
        strategy.last_signal = signal.value
        strategy.last_signal_at = datetime.utcnow()

        # --- Get open positions for this strategy+symbol ---
        stmt = (
            select(Position)
            .where(
                Position.strategy_id == strategy_id,
                Position.symbol == symbol,
                Position.closed_at.is_(None),
            )
            .order_by(Position.layer.desc())
        )
        result = await session.execute(stmt)
        open_positions = list(result.scalars().all())

        # --- Current price ---
        ticker = await public_binance.fetch_ticker(symbol)
        current_price = float(ticker["last"])

        # --- Calculate base quantity ---
        base_qty = strategy.base_qty_value
        if strategy.base_qty_type == "margin_pct":
            if total_margin <= 0:
                return  # Cannot calculate margin-based quantity without balance
            base_qty = (total_margin * strategy.base_qty_value / 100) / current_price

        # --- Skip all trading actions if no authenticated API access ---
        # Analysis (RSI, signals) can continue, but orders require auth
        if not auth_binance:
            await session.flush()  # Save any signal history updates
            return

        # --- Open new position ---
        if signal != Signal.NEUTRAL and not open_positions:
            if _check_cooldown(strategy_id, symbol):
                return

            all_positions_stmt = select(Position).where(Position.closed_at.is_(None))
            all_positions_result = await session.execute(all_positions_stmt)
            all_open_positions = list(all_positions_result.scalars().all())

            position_value = base_qty * current_price
            risk_check = risk_mgr.can_open_position(
                all_open_positions, symbol, total_margin, position_value
            )
            if not risk_check.passed:
                logger.info("Strategy %d: risk check failed for %s: %s",
                            strategy_id, symbol, risk_check.reason)
                return

            side = "buy" if signal == Signal.LONG else "sell"
            ps = "LONG" if signal == Signal.LONG else "SHORT"
            try:
                order = await auth_binance.create_market_order(symbol, side, base_qty, position_side=ps)
                avg_price = float(order.get("average", current_price))

                eng = MartingaleEngine(
                    base_quantity=base_qty,
                    multiplier=strategy.martingale_mult,
                    max_layers=strategy.max_layers,
                    take_profit_pct=strategy.take_profit_pct,
                )
                tp_price = eng.get_take_profit_price(avg_price, strategy.direction)

                pos = Position(
                    strategy_id=strategy_id,
                    account_id=strategy.account_id,
                    symbol=symbol,
                    side="long" if side == "buy" else "short",
                    quantity=base_qty,
                    entry_price=avg_price,
                    mark_price=current_price,
                    layer=0,
                    take_profit_price=tp_price,
                    exchange_order_id=order.get("id", ""),
                )
                session.add(pos)
                await session.flush()
                _set_cooldown(strategy_id, symbol)
                logger.info("Strategy %d: opened %s %s qty=%.4f price=%.4f RSI=%.1f",
                            strategy_id, side, symbol, base_qty, avg_price, rsi)
            except Exception as e:
                await session.rollback()
                logger.error("Strategy %d: failed to open %s: %s", strategy_id, symbol, e)
            return

        # --- Manage existing positions ---
        if open_positions:
            positions_data = [
                {"quantity": p.quantity, "entry_price": p.entry_price}
                for p in open_positions
            ]
            eng = MartingaleEngine(
                base_quantity=base_qty,
                multiplier=strategy.martingale_mult,
                max_layers=strategy.max_layers,
                price_drop_pct=strategy.price_drop_pct,
                take_profit_pct=strategy.take_profit_pct,
            )
            avg_entry, total_qty = eng.get_avg_entry_price(positions_data)
            current_layer = max(p.layer for p in open_positions)

            # Update mark prices & unrealized PnL
            for p in open_positions:
                p.mark_price = current_price
                if p.side == "long":
                    p.unrealized_pnl = (current_price - p.entry_price) * p.quantity
                else:
                    p.unrealized_pnl = (p.entry_price - current_price) * p.quantity

            # --- Check stop loss & take profit ---
            close_reason = None
            if strategy.stop_loss_enabled and risk_mgr.check_stop_loss(
                avg_entry, current_price, strategy.stop_loss_pct, strategy.direction
            ):
                close_reason = "stop_loss"
            elif eng.check_take_profit(avg_entry, current_price, strategy.direction):
                close_reason = "take_profit"

            if close_reason and auth_binance:
                close_success = False
                try:
                    if close_reason == "take_profit" and strategy.take_profit_limit_order:
                        limit_price = eng.get_take_profit_price(avg_entry, strategy.direction)
                        await auth_binance.close_position_with_limit(
                            symbol, strategy.direction, limit_price
                        )
                    else:
                        await auth_binance.close_position(symbol, strategy.direction)
                    close_success = True
                    logger.info("Strategy %d: closed %s due to %s", strategy_id, symbol, close_reason)
                except Exception as e:
                    logger.error("Strategy %d: close position failed: %s", strategy_id, e)

                if close_success:
                    now = datetime.utcnow()
                    from ..models.trade import Trade
                    for p in open_positions:
                        p.closed_at = now
                        exit_pnl = (
                            (current_price - p.entry_price) * p.quantity
                            if p.side == "long"
                            else (p.entry_price - current_price) * p.quantity
                        )
                        exit_pnl_pct = (
                            (current_price - p.entry_price) / p.entry_price * 100
                            if p.side == "long"
                            else (p.entry_price - current_price) / p.entry_price * 100
                        )
                        trade = Trade(
                            strategy_id=strategy_id,
                            account_id=strategy.account_id,
                            symbol=symbol,
                            side=p.side,
                            quantity=p.quantity,
                            entry_price=p.entry_price,
                            exit_price=current_price,
                            realized_pnl=exit_pnl,
                            pnl_pct=exit_pnl_pct,
                            entry_time=p.opened_at,
                            exit_time=now,
                            layer=p.layer,
                            close_reason=close_reason,
                        )
                        session.add(trade)
                    await session.flush()
                return

            # --- Check martingale add ---
            last_entry = max(open_positions, key=lambda p: p.layer).entry_price
            result = eng.should_add_position(
                current_layer, last_entry, current_price, strategy.direction
            )
            if result.should_add and auth_binance:
                # No cooldown for martingale adds - they have their own price-drop trigger
                side = "buy" if strategy.direction == "long" else "sell"
                ps = "LONG" if strategy.direction == "long" else "SHORT"
                try:
                    order = await auth_binance.create_market_order(
                        symbol, side, result.next_quantity, position_side=ps
                    )
                    new_avg = float(order.get("average", current_price))
                    new_total = total_qty + result.next_quantity
                    new_avg_entry = (
                        (avg_entry * total_qty + new_avg * result.next_quantity) / new_total
                    )
                    tp_price = eng.get_take_profit_price(new_avg_entry, strategy.direction)

                    pos = Position(
                        strategy_id=strategy_id,
                        account_id=strategy.account_id,
                        symbol=symbol,
                        side=strategy.direction,
                        quantity=result.next_quantity,
                        entry_price=new_avg,
                        mark_price=current_price,
                        layer=result.next_layer,
                        take_profit_price=tp_price,
                        exchange_order_id=order.get("id", ""),
                    )
                    session.add(pos)
                    await session.flush()
                    _set_cooldown(strategy_id, symbol)
                    logger.info(
                        "Strategy %d: martingale add layer %d for %s qty=%.4f price=%.4f drop=%.1f%%",
                        strategy_id, result.next_layer, symbol,
                        result.next_quantity, new_avg, result.price_drop_from_last
                    )
                except Exception as e:
                    await session.rollback()
                    logger.error("Strategy %d: martingale add failed: %s", strategy_id, e)
                return

            await session.flush()


                    async with session.begin_nested():
                        await self._position_mgr.process_symbol(
                            session, strategy, symbol, auth_binance, public_binance,
                            total_margin, leverage, exchange_open_set,
                        )
                except Exception as e:
                    logger.error("Strategy %d: error processing %s: %s", strategy_id, symbol, e)

            await session.commit()

            # Sync after signal processing — TP detection runs first
            if auth_binance:
                await self._syncer.sync(auth_binance, sync_account_id, auth_binance)


# Singleton
strategy_scheduler = StrategyScheduler()

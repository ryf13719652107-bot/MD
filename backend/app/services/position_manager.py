"""Per-symbol position processing: signal, open, manage, close, martingale."""
import asyncio
import logging
from datetime import datetime
from typing import Optional
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from ..models.strategy import Strategy
from ..models.position import Position
from ..models.trade import Trade
from ..config import now_beijing
from .binance_service import BinanceService
from .strategy_engine import calculate_rsi, generate_signal, Signal, calculate_wavetrend, generate_wt_signal
from .martingale_engine import MartingaleEngine
from .risk_manager import RiskManager
from .log_service import strategy_log_service

logger = logging.getLogger(__name__)

# Shared caches and cooldown (module-level, shared with scheduler)
_kline_cache: dict[tuple[str, str], tuple[float, list]] = {}
_KLINE_CACHE_TTL = 30.0
_signal_cooldowns: dict[tuple[int, str], float] = {}
_signal_cooldown_lock = asyncio.Lock()
_SIGNAL_COOLDOWN_SECONDS = 5


def set_cooldown_lock(lock):
    global _signal_cooldown_lock
    _signal_cooldown_lock = lock


def _get_cached_klines(symbol: str, timeframe: str) -> Optional[list]:
    import time
    key = (symbol, timeframe)
    if key in _kline_cache:
        ts, data = _kline_cache[key]
        if time.time() - ts < _KLINE_CACHE_TTL:
            return data
    return None


def _set_cached_klines(symbol: str, timeframe: str, klines: list):
    import time
    _kline_cache[(symbol, timeframe)] = (time.time(), klines)


async def _check_cooldown(strategy_id: int, symbol: str) -> bool:
    import time
    async with _signal_cooldown_lock:
        now = time.time()
        key = (strategy_id, symbol)
        # Cleanup stale entries
        stale = [k for k, v in _signal_cooldowns.items() if now - v > _SIGNAL_COOLDOWN_SECONDS * 2]
        for k in stale:
            del _signal_cooldowns[k]
        if key in _signal_cooldowns:
            if now - _signal_cooldowns[key] < _SIGNAL_COOLDOWN_SECONDS:
                return True
        _signal_cooldowns[key] = now
        return False


def _clear_cooldown(strategy_id: int, symbol: str):
    import asyncio
    async def _clear():
        async with _signal_cooldown_lock:
            _signal_cooldowns.pop((strategy_id, symbol), None)
    # fire-and-forget if no loop, otherwise schedule
    try:
        loop = __import__('asyncio').get_running_loop()
        loop.create_task(_clear())
    except RuntimeError:
        pass


class PositionManager:
    """Handles per-symbol processing within a strategy tick."""

    def __init__(self, risk_mgr: Optional[RiskManager] = None):
        self.risk_mgr = risk_mgr or RiskManager()

    async def process_symbol(
        self,
        session: AsyncSession,
        strategy: Strategy,
        symbol: str,
        auth_binance: Optional[BinanceService],
        public_binance: BinanceService,
        total_margin: float,
        leverage: float,
    ):
        strategy_id = strategy.id

        # --- Fetch klines with cache ---
        klines = _get_cached_klines(symbol, strategy.timeframe)
        if klines is None:
            # RSI Wilder smoothing needs ~100 bars to converge; WT needs 33+
            limit = 50 if strategy.signal_source == "wavetrend" else 100
            klines = await public_binance.fetch_klines(symbol, strategy.timeframe, limit=limit)
            _set_cached_klines(symbol, strategy.timeframe, klines)

        # --- Generate signal based on source ---
        # Use the just-closed candle for signal detection (no lag).
        # Scheduler is aligned to candle close times, so klines[-1] is complete.
        rsi = 0.0  # always defined, used for logging
        signal_label = "RSI"
        if strategy.signal_source == "wavetrend":
            wt = calculate_wavetrend(klines, strategy.wt_channel_length, strategy.wt_average_length)
            if wt is None:
                return
            signal = generate_wt_signal(wt, strategy.direction, strategy.wt_os_level, strategy.wt_ob_level)
            strategy.last_rsi = wt["wt1"]  # reuse field for WT1 display
            strategy.last_signal = signal.value
            strategy.last_signal_at = now_beijing()
            rsi = wt["wt1"]
            signal_label = "WT1"
            if signal != Signal.NEUTRAL:
                strategy_log_service.info(strategy_id, f"{symbol} WT1={wt['wt1']} WT2={wt['wt2']} 信号={signal.value}")
        else:
            rsi = calculate_rsi(klines, strategy.rsi_period)
            if rsi is None:
                return
            signal = generate_signal(rsi, strategy.direction, strategy.rsi_entry_threshold)
            strategy.last_rsi = round(rsi, 1)
            strategy.last_signal = signal.value
            strategy.last_signal_at = now_beijing()
            if signal != Signal.NEUTRAL:
                strategy_log_service.info(strategy_id, f"{symbol} RSI={round(rsi,1)} 信号={signal.value}")

        # --- Get open positions for this strategy+symbol ---
        stmt = (
            select(Position)
            .where(Position.strategy_id == strategy_id, Position.symbol == symbol, Position.closed_at.is_(None))
            .order_by(Position.layer.desc())
        )
        result = await session.execute(stmt)
        open_positions = list(result.scalars().all())

        if open_positions:
            strategy_log_service.info(strategy_id, f"{symbol} 已有{len(open_positions)}个持仓，进入管理")

        # --- Current price ---
        try:
            current_price = float(klines[-1][4])
        except (TypeError, ValueError, IndexError):
            logger.warning("Strategy %d: %s invalid kline data, skipping", strategy_id, symbol)
            strategy_log_service.warning(strategy_id, f"{symbol} K线数据异常，跳过")
            return

        # --- Base quantity ---
        base_qty = strategy.base_qty_value
        if strategy.base_qty_type == "margin_pct":
            if total_margin <= 0:
                logger.warning("Strategy %d: cannot open %s — total_margin=%.2f", strategy_id, symbol, total_margin)
                strategy_log_service.warning(strategy_id, f"{symbol} 无法开仓 — 余额为0(当前{total_margin:.1f})")
                return
            base_qty = (total_margin * strategy.base_qty_value / 100) / current_price
        elif strategy.base_qty_type == "usdt":
            base_qty = strategy.base_qty_value / current_price

        if not auth_binance:
            logger.warning("Strategy %d: cannot open %s — no API auth (account %d)", strategy_id, symbol, strategy.account_id)
            strategy_log_service.warning(strategy_id, f"{symbol} 无法开仓 — API未认证")
            await session.flush()
            return

        # --- Open new position ---
        if signal != Signal.NEUTRAL and not open_positions:
            logger.info("Strategy %d: %s signal=%s, attempting to open...", strategy_id, symbol, signal.value)
            # Fresh exchange check before opening — prevent duplicate opens across concurrent ticks
            try:
                eps = await auth_binance.fetch_positions([symbol])
                for ep in eps:
                    ep_side = (ep.get("side") or "").lower()
                    if ep_side == signal.value and float(ep.get("contracts", 0) or 0) > 0:
                        strategy_log_service.info(strategy_id, f"{symbol} 交易所已有仓位，跳过开仓")
                        return
            except Exception:
                pass  # if fetch fails, proceed with open (better than blocking)
            await self._open_position(session, strategy, symbol, auth_binance, public_binance, signal, base_qty, current_price, total_margin, leverage, rsi)
            return

        # --- Manage existing positions ---
        if open_positions:
            await self._manage_positions(session, strategy, symbol, auth_binance, public_binance, open_positions, base_qty, current_price, total_margin, leverage, klines)

    async def _open_position(
        self, session, strategy, symbol, auth_binance, public_binance, signal, base_qty, current_price, total_margin, leverage, rsi
    ):
        strategy_id = strategy.id

        if await _check_cooldown(strategy_id, symbol):
            logger.info("Strategy %d: %s in cooldown, skipping", strategy_id, symbol)
            strategy_log_service.info(strategy_id, f"{symbol} 冷却中，跳过")
            return

        side = "buy" if signal == Signal.LONG else "sell"
        ps = "LONG" if signal == Signal.LONG else "SHORT"
        position_side = "long" if side == "buy" else "short"

        try:
            order = await auth_binance.create_market_order(
                symbol, side, base_qty, position_side=ps,
            )
            avg_price = float(order.get("average") or order.get("price") or 0)
            if avg_price <= 0:
                avg_price = current_price
                logger.warning("Strategy %d: %s order filled but no average/price in response, using kline close", strategy_id, symbol)
            filled_qty = float(order.get("filled") or order.get("amount") or base_qty)
        except Exception as e:
            _clear_cooldown(strategy_id, symbol)
            logger.error("Strategy %d: failed to open %s: %s", strategy_id, symbol, e)
            strategy_log_service.error(strategy_id, f"{symbol} 开仓失败 — {e}")
            return

        # Order succeeded on exchange — ensure DB record is written even if TP order fails
        try:
            eng = MartingaleEngine(base_quantity=filled_qty, multiplier=strategy.martingale_mult, max_layers=strategy.max_layers, take_profit_pct=strategy.take_profit_pct)
            tp_price = eng.get_take_profit_price(avg_price, position_side)

            pos = Position(
                strategy_id=strategy_id, account_id=strategy.account_id,
                symbol=symbol, side=position_side, quantity=filled_qty,
                entry_price=avg_price, mark_price=current_price, layer=0,
                take_profit_price=tp_price, exchange_order_id=order.get("id", ""),
            )
            session.add(pos)
            await session.flush()
        except Exception as e:
            logger.critical("Strategy %d: %s order filled on exchange but DB record failed: %s", strategy_id, symbol, e)
            strategy_log_service.error(strategy_id, f"{symbol} 开仓已成交但DB记录失败 — 请手动检查交易所仓位!")
            return

        # Place limit TP order (best-effort, non-fatal)
        if strategy.take_profit_limit_order and tp_price > 0:
            tp_placed = False
            close_side = "sell" if position_side == "long" else "buy"
            for attempt in range(2):
                try:
                    tp_order = await auth_binance.create_limit_order(symbol, close_side, filled_qty, tp_price, reduce_only=False, position_side=ps)
                    tp_order_id = tp_order.get("id", "")
                    if tp_order_id:
                        pos.tp_limit_order_id = tp_order_id
                        await session.flush()
                        strategy_log_service.info(strategy_id, f"{symbol} 挂止盈限价单 @{tp_price:.6f} id={tp_order_id}")
                        tp_placed = True
                        break
                    else:
                        strategy_log_service.warning(strategy_id, f"{symbol} 挂止盈单异常 — 返回无id: {tp_order}")
                except Exception as tp_err:
                    logger.error("Strategy %d: TP limit order failed for %s (attempt %d): %s", strategy_id, symbol, attempt + 1, tp_err)
                    if attempt == 0:
                        await asyncio.sleep(0.5)
            if not tp_placed:
                strategy_log_service.warning(strategy_id, f"{symbol} 止盈挂单失败(已重试) — 下次tick将用市价止盈兜底")

        logger.info("Strategy %d: opened %s %s qty=%.4f price=%.4f %s=%.1f", strategy_id, side, symbol, base_qty, avg_price, signal_label, rsi)
        strategy_log_service.success(strategy_id, f"{symbol} 开{position_side}成功 qty={base_qty:.4f} price={avg_price:.4f} {signal_label}={round(rsi,1)}")

    async def _manage_positions(
        self, session, strategy, symbol, auth_binance, public_binance, open_positions, base_qty, current_price, total_margin, leverage, klines=None
    ):
        strategy_id = strategy.id
        pos_side = open_positions[0].side

        positions_data = [{"quantity": p.quantity, "entry_price": p.entry_price} for p in open_positions]
        eng = MartingaleEngine(base_quantity=base_qty, multiplier=strategy.martingale_mult, max_layers=strategy.max_layers,
                               price_drop_pct=strategy.price_drop_pct, take_profit_pct=strategy.take_profit_pct)
        avg_entry, total_qty = eng.get_avg_entry_price(positions_data)
        current_layer = max(p.layer for p in open_positions)

        # Update mark prices
        for p in open_positions:
            p.mark_price = current_price
            p.unrealized_pnl = (current_price - p.entry_price) * p.quantity if p.side == "long" else (p.entry_price - current_price) * p.quantity

        # --- Check TP limit order fill (independent of price condition) ---
        if strategy.take_profit_limit_order:
            for p in open_positions:
                if p.tp_limit_order_id:
                    try:
                        order_info = await asyncio.wait_for(
                            auth_binance.exchange.fetch_order(p.tp_limit_order_id, auth_binance._format_symbol(symbol)),
                            timeout=2.0,
                        )
                        status = order_info.get("status", "")
                        avg_fill = float(order_info.get("average", 0) or 0)
                        if status in ("closed", "filled") and avg_fill > 0:
                            await self._close_positions(session, strategy, symbol, auth_binance, open_positions, eng, avg_entry, pos_side, "take_profit", current_price, pre_exit_price=avg_fill)
                            return
                    except (Exception, asyncio.TimeoutError):
                        pass  # fetch_order failed, skip — will check next tick

        # --- Check SL / TP by price ---
        close_reason = None
        exit_price_override = current_price
        if strategy.stop_loss_enabled and self.risk_mgr.check_stop_loss(avg_entry, current_price, strategy.stop_loss_pct, pos_side):
            close_reason = "stop_loss"
        elif eng.check_take_profit(avg_entry, current_price, pos_side):
            close_reason = "take_profit"

        if close_reason:
            await self._close_positions(session, strategy, symbol, auth_binance, open_positions, eng, avg_entry, pos_side, close_reason, exit_price_override)
            return

        # --- Check martingale add ---
        last_entry = max(open_positions, key=lambda p: p.layer).entry_price
        result = eng.should_add_position(current_layer, last_entry, current_price, pos_side)
        if result.should_add:
            await self._martingale_add(session, strategy, symbol, auth_binance, open_positions, eng, result, avg_entry, total_qty, pos_side, current_price, klines, public_binance)
            return

        await session.flush()

    async def check_tp_fills(self, session, strategy, auth_binance, current_price: float):
        from ..models.position import Position
        strategy_id = strategy.id
        stmt = select(Position).where(
            Position.strategy_id == strategy_id, Position.closed_at.is_(None)
        )
        result = await session.execute(stmt)
        open_positions = list(result.scalars().all())

        processed_symbols: set[tuple[str, str]] = set()
        for p in open_positions:
            if not p.tp_limit_order_id or not p.take_profit_price:
                continue
            symbol_side_key = (p.symbol, p.side)
            if symbol_side_key in processed_symbols:
                continue
            try:
                order_info = await asyncio.wait_for(
                    auth_binance.exchange.fetch_order(
                        p.tp_limit_order_id, auth_binance._format_symbol(p.symbol)
                    ),
                    timeout=2.0,
                )
                status = order_info.get("status", "")
                avg_fill = float(order_info.get("average", 0) or 0)
                if status in ("closed", "filled") and avg_fill > 0:
                    symbol_positions = [op for op in open_positions if op.symbol == p.symbol and op.side == p.side]
                    if not symbol_positions:
                        continue
                    positions_data = [{"quantity": op.quantity, "entry_price": op.entry_price} for op in symbol_positions]
                    eng = MartingaleEngine(base_quantity=symbol_positions[0].quantity, multiplier=strategy.martingale_mult,
                                           max_layers=strategy.max_layers, take_profit_pct=strategy.take_profit_pct)
                    avg_entry, _ = eng.get_avg_entry_price(positions_data)
                    await self._close_positions(session, strategy, p.symbol, auth_binance, symbol_positions,
                                                eng, avg_entry, p.side, "take_profit", current_price, pre_exit_price=avg_fill)
                    logger.info("Strategy %d: TP fill detected mid-candle for %s @%.4f", strategy_id, p.symbol, avg_fill)
                    processed_symbols.add(symbol_side_key)
            except (Exception, asyncio.TimeoutError):
                logger.warning("Strategy %d: TP order check failed for %s %s, retrying next cycle", strategy_id, p.symbol, p.side)

    async def _close_positions(self, session, strategy, symbol, auth_binance, open_positions, eng, avg_entry, pos_side, close_reason, current_price, pre_exit_price: float = 0.0):
        strategy_id = strategy.id

        exit_price = 0.0

        if pre_exit_price > 0:
            exit_price = pre_exit_price
            strategy_log_service.success(strategy_id, f"{symbol} 止盈平仓 — 限价单已成交 @{exit_price:.4f}")
            logger.info("Strategy %d: TP limit filled for %s @%.4f", strategy_id, symbol, exit_price)
        elif close_reason == "take_profit" and strategy.take_profit_limit_order:
            has_tp_order = any(p.tp_limit_order_id for p in open_positions)
            if not has_tp_order:
                strategy_log_service.warning(strategy_id, f"{symbol} 止盈条件触发但无限价单ID — 兜底市价平仓")
                logger.warning("Strategy %d: %s TP triggered but no tp_limit_order_id, falling back to market close", strategy_id, symbol)
            else:
                tp_order_id = None
                for p in open_positions:
                    if p.tp_limit_order_id:
                        tp_order_id = p.tp_limit_order_id
                        break
                if tp_order_id:
                    try:
                        order_info = await asyncio.wait_for(
                            auth_binance.exchange.fetch_order(
                                tp_order_id, auth_binance._format_symbol(symbol)
                            ),
                            timeout=3.0,
                        )
                        order_status = order_info.get("status", "")
                        avg_fill = float(order_info.get("average", 0) or 0)
                        if order_status in ("closed", "filled") and avg_fill > 0:
                            exit_price = avg_fill
                            strategy_log_service.success(strategy_id, f"{symbol} 止盈限价单已成交 @{exit_price:.4f}")
                            logger.info("Strategy %d: TP limit already filled for %s @%.4f", strategy_id, symbol, exit_price)
                        elif order_status in ("canceled", "cancelled", "expired"):
                            strategy_log_service.warning(strategy_id, f"{symbol} 止盈限价单已取消/过期 — 兜底市价平仓")
                            logger.warning("Strategy %d: %s TP order %s, falling back to market close", strategy_id, symbol, order_status)
                            for p in open_positions:
                                p.tp_limit_order_id = None
                        else:
                            strategy_log_service.info(strategy_id, f"{symbol} 止盈限价单状态={order_status}，等待成交")
                            return
                    except (Exception, asyncio.TimeoutError) as e:
                        logger.warning("Strategy %d: TP order check failed for %s: %s — waiting for check_tp_fills", strategy_id, symbol, e)
                        return
                else:
                    strategy_log_service.warning(strategy_id, f"{symbol} 止盈条件触发但无限价单 — 兜底市价平仓")

            if exit_price <= 0:
                for p in open_positions:
                    if p.tp_limit_order_id:
                        try:
                            await auth_binance.cancel_order(p.tp_limit_order_id, symbol)
                        except Exception:
                            pass
                        p.tp_limit_order_id = None
                try:
                    result = await auth_binance.close_position(symbol, pos_side)
                    if result and result.get("id"):
                        exit_price = float(result.get("average", 0) or result.get("price", 0) or current_price)
                        close_reason = "take_profit"
                        strategy_log_service.success(strategy_id, f"{symbol} 兜底市价止盈 @{exit_price:.4f}")
                    else:
                        strategy_log_service.warning(strategy_id, f"{symbol} 兜底平仓失败 — 未找到交易所仓位")
                        return
                except Exception as e:
                    logger.error("Strategy %d: fallback market close failed: %s", strategy_id, e)
                    strategy_log_service.error(strategy_id, f"{symbol} 兜底平仓异常 — {e}")
                    return
        else:
            for p in open_positions:
                if p.tp_limit_order_id:
                    try:
                        order_info = await asyncio.wait_for(
                            auth_binance.exchange.fetch_order(
                                p.tp_limit_order_id, auth_binance._format_symbol(symbol)
                            ),
                            timeout=2.0,
                        )
                        order_status = order_info.get("status", "")
                        avg_fill = float(order_info.get("average", 0) or 0)
                        if order_status in ("closed", "filled") and avg_fill > 0:
                            exit_price = avg_fill
                            strategy_log_service.success(strategy_id, f"{symbol} 止盈限价单已成交 @{exit_price:.4f}（止损检查时发现）")
                            logger.info("Strategy %d: TP limit already filled during SL check for %s @%.4f", strategy_id, symbol, exit_price)
                            close_reason = "take_profit"
                            break
                    except (Exception, asyncio.TimeoutError):
                        pass
                    try:
                        await auth_binance.cancel_order(p.tp_limit_order_id, symbol)
                    except Exception:
                        pass
                    p.tp_limit_order_id = None

            if exit_price <= 0:
                try:
                    result = await auth_binance.close_position(symbol, pos_side)
                    if result and result.get("id"):
                        exit_price = float(result.get("average", 0) or result.get("price", 0) or current_price)
                    else:
                        strategy_log_service.warning(strategy_id, f"{symbol} 平仓失败 — 未找到交易所仓位")
                        return
                except Exception as e:
                    logger.error("Strategy %d: close position failed: %s", strategy_id, e)
                    strategy_log_service.error(strategy_id, f"{symbol} 平仓异常 — {e}")
                    return

        # Common: create Trade records and mark positions closed
        logger.info("Strategy %d: closed %s due to %s", strategy_id, symbol, close_reason)
        now = now_beijing()
        for p in open_positions:
            p.closed_at = now
            exit_pnl = (exit_price - p.entry_price) * p.quantity if p.side == "long" else (p.entry_price - exit_price) * p.quantity
            exit_pnl_pct = (exit_price - p.entry_price) / p.entry_price * 100 if p.side == "long" else (p.entry_price - exit_price) / p.entry_price * 100
            trade = Trade(
                strategy_id=strategy_id, account_id=strategy.account_id, symbol=symbol,
                side=p.side, quantity=p.quantity, entry_price=p.entry_price, exit_price=exit_price,
                realized_pnl=exit_pnl, pnl_pct=exit_pnl_pct,
                entry_time=p.opened_at, exit_time=now, layer=p.layer, close_reason=close_reason,
            )
            session.add(trade)
        await session.flush()

    async def _martingale_add(self, session, strategy, symbol, auth_binance, open_positions, eng, result, avg_entry, total_qty, pos_side, current_price, klines=None, public_binance=None):
        strategy_id = strategy.id
        side = "buy" if pos_side == "long" else "sell"
        ps = "LONG" if pos_side == "long" else "SHORT"

        # Signal re-check for martingale add (if enabled)
        if strategy.martingale_rsi_enabled and klines is not None and public_binance is not None:
            if strategy.signal_source == "wavetrend":
                wt = calculate_wavetrend(klines, strategy.wt_channel_length, strategy.wt_average_length)
                if wt is not None:
                    confirm = generate_wt_signal(wt, strategy.direction, strategy.wt_os_level, strategy.wt_ob_level)
                    if confirm == Signal.NEUTRAL:
                        strategy_log_service.info(strategy_id, f"{symbol} 马丁加仓跳过 — WT1={wt['wt1']} 信号已消失")
                        return
                    strategy_log_service.info(strategy_id, f"{symbol} 马丁加仓WT确认 — WT1={wt['wt1']} WT2={wt['wt2']}")
            else:
                rsi_val = calculate_rsi(klines, strategy.rsi_period)
                if rsi_val is not None:
                    confirm = generate_signal(rsi_val, strategy.direction, strategy.rsi_entry_threshold)
                    if confirm == Signal.NEUTRAL:
                        strategy_log_service.info(strategy_id, f"{symbol} 马丁加仓跳过 — RSI={round(rsi_val,1)} 信号已消失")
                        return
                    strategy_log_service.info(strategy_id, f"{symbol} 马丁加仓RSI确认 — RSI={round(rsi_val,1)}")

        # Step 1: execute add order FIRST
        try:
            order = await auth_binance.create_market_order(
                symbol, side, result.next_quantity, position_side=ps,
            )
            new_avg = float(order.get("average") or order.get("price") or 0)
            if new_avg <= 0:
                new_avg = current_price
                logger.warning("Strategy %d: %s martingale order filled but no average/price in response, using kline close", strategy_id, symbol)
            filled_qty = float(order.get("filled") or order.get("amount") or result.next_quantity)
        except Exception as e:
            logger.error("Strategy %d: martingale add failed: %s", strategy_id, e)
            strategy_log_service.error(strategy_id, f"{symbol} 马丁加仓失败 — {e}")
            return

        # Step 2: record in DB
        try:
            new_total = total_qty + filled_qty
            new_avg_entry = (avg_entry * total_qty + new_avg * filled_qty) / new_total
            tp_price = eng.get_take_profit_price(new_avg_entry, pos_side)

            pos = Position(
                strategy_id=strategy_id, account_id=strategy.account_id,
                symbol=symbol, side=pos_side, quantity=filled_qty,
                entry_price=new_avg, mark_price=current_price, layer=result.next_layer,
                take_profit_price=tp_price, exchange_order_id=order.get("id", ""),
            )
            session.add(pos)
            await session.flush()
        except Exception as e:
            logger.critical("Strategy %d: %s martingale order filled but DB record failed: %s", strategy_id, symbol, e)
            strategy_log_service.error(strategy_id, f"{symbol} 马丁加仓已成交但DB记录失败 — 请手动检查交易所仓位!")
            return

        # Step 3: cancel old TP orders AFTER add is secured
        if strategy.take_profit_limit_order:
            for p in open_positions:
                if p.tp_limit_order_id:
                    try:
                        await auth_binance.cancel_order(p.tp_limit_order_id, symbol)
                        strategy_log_service.info(strategy_id, f"{symbol} 取消旧止盈单 {p.tp_limit_order_id}")
                    except Exception:
                        logger.warning("Strategy %d: failed to cancel old TP %s for %s", strategy_id, p.tp_limit_order_id, symbol)
                    p.tp_limit_order_id = None

        # Step 4: place new combined TP order (best-effort)
        if strategy.take_profit_limit_order:
            tp_placed = False
            close_side = "sell" if pos_side == "long" else "buy"
            for attempt in range(2):
                try:
                    tp_order = await auth_binance.create_limit_order(symbol, close_side, new_total, tp_price, reduce_only=False, position_side=ps)
                    tp_order_id = tp_order.get("id", "")
                    if tp_order_id:
                        pos.tp_limit_order_id = tp_order_id
                        await session.flush()
                        strategy_log_service.info(strategy_id, f"{symbol} 更新止盈挂单 @{tp_price:.6f} qty={new_total:.4f}")
                        tp_placed = True
                        break
                    else:
                        strategy_log_service.warning(strategy_id, f"{symbol} 更新止盈单异常 — 返回无id: {tp_order}")
                except Exception as tp_err:
                    logger.error("Strategy %d: TP limit update failed for %s (attempt %d): %s", strategy_id, symbol, attempt + 1, tp_err)
                    if attempt == 0:
                        await asyncio.sleep(0.5)
            if not tp_placed:
                strategy_log_service.warning(strategy_id, f"{symbol} 止盈挂单更新失败(已重试) — 下次tick将用市价止盈兜底")

        logger.info("Strategy %d: martingale add layer %d for %s qty=%.4f price=%.4f drop=%.1f%%",
                    strategy_id, result.next_layer, symbol, result.next_quantity, new_avg, result.price_drop_from_last)
        strategy_log_service.info(strategy_id, f"{symbol} 马丁加仓 L{result.next_layer} qty={result.next_quantity:.4f} 跌幅={result.price_drop_from_last:.1f}%")

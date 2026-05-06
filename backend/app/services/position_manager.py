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
_SIGNAL_COOLDOWN_SECONDS = 60


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
        exchange_open_set: set = None,
    ):
        strategy_id = strategy.id

        # --- Fetch klines with cache ---
        klines = _get_cached_klines(symbol, strategy.timeframe)
        if klines is None:
            # WaveTrend needs more klines than RSI (33+ vs 15+)
            limit = 50 if strategy.signal_source == "wavetrend" else 21
            klines = await public_binance.fetch_klines(symbol, strategy.timeframe, limit=limit)
            _set_cached_klines(symbol, strategy.timeframe, klines)

        if len(klines) > 1:
            klines = klines[:-1]

        # --- Generate signal based on source ---
        rsi = 0.0  # always defined, used for logging
        if strategy.signal_source == "wavetrend":
            wt = calculate_wavetrend(klines, strategy.wt_channel_length, strategy.wt_average_length)
            if wt is None:
                return
            signal = generate_wt_signal(wt, strategy.direction)
            strategy.last_rsi = wt["wt1"]  # reuse field for WT1 display
            strategy.last_signal = signal.value
            strategy.last_signal_at = now_beijing()
            rsi = wt["wt1"]
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
            strategy_log_service.info(strategy_id, f"{symbol} 已有{len(open_positions)}个持仓，跳过开仓检查")

        # --- Current price ---
        try:
            current_price = float(klines[-1][4])
        except (TypeError, ValueError, IndexError):
            return

        # --- Base quantity ---
        base_qty = strategy.base_qty_value
        if strategy.base_qty_type == "margin_pct":
            if total_margin <= 0:
                logger.warning("Strategy %d: cannot open %s — total_margin=%.2f", strategy_id, symbol, total_margin)
                strategy_log_service.warning(strategy_id, f"{symbol} 无法开仓 — 余额为0，请检查API密钥")
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
            # Skip if exchange already has this position (dedup — only prevents duplicate opens)
            if exchange_open_set and (symbol, signal.value) in exchange_open_set:
                strategy_log_service.info(strategy_id, f"{symbol} 交易所已有仓位，跳过开仓")
                return
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

        # Risk check
        all_positions_stmt = select(Position).where(Position.closed_at.is_(None))
        all_positions_result = await session.execute(all_positions_stmt)
        all_open_positions = list(all_positions_result.scalars().all())

        position_value = base_qty * current_price / leverage
        risk_check = self.risk_mgr.can_open_position(all_open_positions, symbol, total_margin, position_value)
        if not risk_check.passed:
            logger.info("Strategy %d: risk check failed for %s: %s", strategy_id, symbol, risk_check.reason)
            strategy_log_service.warning(strategy_id, f"{symbol} 风控拦截 — {risk_check.reason}")
            _clear_cooldown(strategy_id, symbol)
            return

        side = "buy" if signal == Signal.LONG else "sell"
        ps = "LONG" if signal == Signal.LONG else "SHORT"
        position_side = "long" if side == "buy" else "short"

        try:
            order = await auth_binance.create_market_order(
                symbol, side, base_qty, position_side=ps,
                slippage_pct=strategy.slippage_pct if strategy.slippage_pct > 0 else None,
            )
            avg_price = float(order.get("average", current_price))

            eng = MartingaleEngine(base_quantity=base_qty, multiplier=strategy.martingale_mult, max_layers=strategy.max_layers, take_profit_pct=strategy.take_profit_pct)
            tp_price = eng.get_take_profit_price(avg_price, position_side)

            pos = Position(
                strategy_id=strategy_id, account_id=strategy.account_id,
                symbol=symbol, side=position_side, quantity=base_qty,
                entry_price=avg_price, mark_price=current_price, layer=0,
                take_profit_price=tp_price, exchange_order_id=order.get("id", ""),
            )
            session.add(pos)
            await session.flush()

            # Place limit TP order
            if strategy.take_profit_limit_order and tp_price > 0:
                tp_placed = False
                close_side = "sell" if position_side == "long" else "buy"
                for attempt in range(2):  # retry once
                    try:
                        tp_order = await auth_binance.create_limit_order(symbol, close_side, base_qty, tp_price, reduce_only=True, position_side=ps)
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
                            await asyncio.sleep(0.5)  # brief wait before retry
                if not tp_placed:
                    strategy_log_service.warning(strategy_id, f"{symbol} 止盈挂单失败(已重试) — 下次tick将用市价止盈兜底")

            logger.info("Strategy %d: opened %s %s qty=%.4f price=%.4f RSI=%.1f", strategy_id, side, symbol, base_qty, avg_price, rsi)
            strategy_log_service.success(strategy_id, f"{symbol} 开{position_side}成功 qty={base_qty:.4f} price={avg_price:.4f} RSI={round(rsi,1)}")
        except Exception as e:
            _clear_cooldown(strategy_id, symbol)
            logger.error("Strategy %d: failed to open %s: %s", strategy_id, symbol, e)
            strategy_log_service.error(strategy_id, f"{symbol} 开仓失败 — {e}")
            raise

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

        # --- Check SL / TP ---
        close_reason = None
        if strategy.stop_loss_enabled and self.risk_mgr.check_stop_loss(avg_entry, current_price, strategy.stop_loss_pct, pos_side):
            close_reason = "stop_loss"
        elif eng.check_take_profit(avg_entry, current_price, pos_side):
            close_reason = "take_profit"

        if close_reason:
            await self._close_positions(session, strategy, symbol, auth_binance, open_positions, eng, avg_entry, pos_side, close_reason, current_price)
            return

        # --- Check martingale add ---
        last_entry = max(open_positions, key=lambda p: p.layer).entry_price
        result = eng.should_add_position(current_layer, last_entry, current_price, pos_side)
        if result.should_add:
            await self._martingale_add(session, strategy, symbol, auth_binance, open_positions, eng, result, avg_entry, total_qty, pos_side, current_price, klines, public_binance)
            return

        await session.flush()

    async def _close_positions(self, session, strategy, symbol, auth_binance, open_positions, eng, avg_entry, pos_side, close_reason, current_price):
        strategy_id = strategy.id

        # For limit TP: the initial TP order on exchange already handles the close.
        # Just check if position still exists on exchange. If gone, TP filled → success.
        if close_reason == "take_profit" and strategy.take_profit_limit_order:
            exchange_positions = await auth_binance.fetch_positions([symbol])
            pos_side_cap = "LONG" if pos_side == "long" else "SHORT"
            pos_side_lower = pos_side.lower()
            still_open = False
            for ep in exchange_positions:
                ep_side = (ep.get("side") or "").lower()
                if ep_side == pos_side_lower and float(ep.get("contracts", 0)) > 0:
                    still_open = True
                    break

            if not still_open:
                # TP limit order already filled — get real fill price, fallback to limit price
                limit_price = eng.get_take_profit_price(avg_entry, pos_side)
                fill_price = limit_price  # best guess: the order was placed at this price
                for p in open_positions:
                    if p.tp_limit_order_id:
                        for attempt in range(2):
                            try:
                                order_info = await auth_binance.exchange.fetch_order(p.tp_limit_order_id, auth_binance._format_symbol(symbol))
                                avg_fill = float(order_info.get("average", 0) or 0)
                                if avg_fill > 0:
                                    fill_price = avg_fill
                                    break
                            except Exception:
                                if attempt == 0:
                                    await asyncio.sleep(0.3)
                        break
                exit_price = fill_price
                strategy_log_service.success(strategy_id, f"{symbol} 止盈平仓 — 限价单已成交 @{fill_price:.4f}")
                logger.info("Strategy %d: TP limit filled for %s (exchange position closed)", strategy_id, symbol)
            else:
                # TP limit order still on exchange — it will fill naturally
                strategy_log_service.info(strategy_id, f"{symbol} 止盈限价单已挂交易所，等待成交")
                return
        else:
            # Market close for stop loss or market TP
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

        # RSI re-check for martingale add (if enabled)
        if strategy.martingale_rsi_enabled and klines is not None and public_binance is not None:
            rsi_val = calculate_rsi(klines, strategy.rsi_period)
            if rsi_val is not None:
                signal = generate_signal(rsi_val, strategy.direction, strategy.rsi_entry_threshold)
                from .strategy_engine import Signal
                if signal == Signal.NEUTRAL:
                    strategy_log_service.info(strategy_id, f"{symbol} 马丁加仓跳过 — RSI={round(rsi_val,1)} 信号已消失")
                    return
                strategy_log_service.info(strategy_id, f"{symbol} 马丁加仓RSI确认 — RSI={round(rsi_val,1)}")

        # Cancel old TP orders before adding
        if strategy.take_profit_limit_order:
            for p in open_positions:
                if p.tp_limit_order_id:
                    try:
                        await auth_binance.cancel_order(p.tp_limit_order_id, symbol)
                        strategy_log_service.info(strategy_id, f"{symbol} 取消旧止盈单 {p.tp_limit_order_id}")
                    except Exception:
                        pass
                    p.tp_limit_order_id = None

        try:
            order = await auth_binance.create_market_order(
                symbol, side, result.next_quantity, position_side=ps,
                slippage_pct=strategy.slippage_pct if strategy.slippage_pct > 0 else None,
            )
            new_avg = float(order.get("average", current_price))
            new_total = total_qty + result.next_quantity
            new_avg_entry = (avg_entry * total_qty + new_avg * result.next_quantity) / new_total
            tp_price = eng.get_take_profit_price(new_avg_entry, pos_side)

            pos = Position(
                strategy_id=strategy_id, account_id=strategy.account_id,
                symbol=symbol, side=pos_side, quantity=result.next_quantity,
                entry_price=new_avg, mark_price=current_price, layer=result.next_layer,
                take_profit_price=tp_price, exchange_order_id=order.get("id", ""),
            )
            session.add(pos)
            await session.flush()

            # Place new TP order
            if strategy.take_profit_limit_order:
                tp_placed = False
                close_side = "sell" if pos_side == "long" else "buy"
                for attempt in range(2):
                    try:
                        tp_order = await auth_binance.create_limit_order(symbol, close_side, new_total, tp_price, reduce_only=True, position_side=ps)
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
        except Exception as e:
            logger.error("Strategy %d: martingale add failed: %s", strategy_id, e)
            strategy_log_service.error(strategy_id, f"{symbol} 马丁加仓失败 — {e}")
            raise

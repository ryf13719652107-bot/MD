"""Position synchronization between local DB and exchange."""
import time
import logging
from sqlalchemy import select
from ..database import async_session
from ..models.position import Position
from ..models.trade import Trade
from ..config import now_beijing

logger = logging.getLogger(__name__)

_POSITION_SYNC_INTERVAL = 300  # 5 minutes


class PositionSyncService:
    def __init__(self):
        self._sync_timestamps: dict[str, float] = {}

    async def sync(self, auth_binance, account_id: int, binance_service=None):
        sync_key = f"sync_{account_id}"
        now = time.time()
        if now - self._sync_timestamps.get(sync_key, 0) < _POSITION_SYNC_INTERVAL:
            return
        self._sync_timestamps[sync_key] = now

        try:
            exchange_positions = await auth_binance.fetch_positions()
            async with async_session() as session:
                result = await session.execute(
                    select(Position).where(
                        Position.closed_at.is_(None),
                        Position.account_id == account_id,
                    )
                )
                local_positions = list(result.scalars().all())

                # Build map of exchange positions: (symbol, side) -> data
                exchange_map: dict[tuple[str, str], dict] = {}
                for ep in exchange_positions:
                    if float(ep.get("contracts", 0)) <= 0:
                        continue
                    sym = (ep.get("symbol") or "").replace("/", "").replace(":USDT", "")
                    side = (ep.get("side") or "").lower()
                    exchange_map[(sym, side)] = ep

                # Close local positions not on exchange — create Trade records
                sync_now = now_beijing()
                for lp in local_positions:
                    lp_key = (lp.symbol.replace("/", "").replace(":USDT", ""), lp.side.lower())
                    if lp_key not in exchange_map:
                        # Try to get real fill price, fallback to TP price > mark_price
                        close_reason = "sync"
                        exit_price = lp.mark_price or lp.entry_price
                        if lp.tp_limit_order_id and lp.take_profit_price:
                            close_reason = "take_profit"
                            exit_price = lp.take_profit_price  # best estimate
                            if binance_service:
                                try:
                                    formatted = binance_service._format_symbol(lp.symbol)
                                    oi = await binance_service.exchange.fetch_order(lp.tp_limit_order_id, formatted)
                                    avg = float(oi.get("average", 0) or 0)
                                    if avg > 0:
                                        exit_price = avg
                                except Exception:
                                    pass
                        else:
                            exit_price = lp.mark_price or lp.entry_price
                        exit_pnl = (exit_price - lp.entry_price) * lp.quantity if lp.side == "long" else (lp.entry_price - exit_price) * lp.quantity
                        pnl_pct = 0
                        if lp.entry_price > 0:
                            pnl_pct = ((exit_price - lp.entry_price) / lp.entry_price * 100) if lp.side == "long" else ((lp.entry_price - exit_price) / lp.entry_price * 100)
                        trade = Trade(
                            strategy_id=lp.strategy_id,
                            account_id=lp.account_id,
                            symbol=lp.symbol,
                            side=lp.side,
                            quantity=lp.quantity,
                            entry_price=lp.entry_price,
                            exit_price=exit_price,
                            realized_pnl=exit_pnl,
                            pnl_pct=round(pnl_pct, 2),
                            entry_time=lp.opened_at,
                            exit_time=sync_now,
                            layer=lp.layer,
                            close_reason=close_reason,
                        )
                        session.add(trade)
                        lp.closed_at = sync_now
                        logger.warning("Sync: closed local position %d (%s %s) exit_price=%.4f pnl=%.2f reason=%s", lp.id, lp.symbol, lp.side, exit_price, exit_pnl, close_reason)

                # Create local records for exchange-only positions
                local_keys = {(lp.symbol.replace("/", "").replace(":USDT", ""), lp.side.lower()) for lp in local_positions}
                for (sym, side), ep in exchange_map.items():
                    if (sym, side) not in local_keys:
                        mark_price = float(ep.get("markPrice", 0) or 0)
                        entry_price = float(ep.get("entryPrice", 0) or 0)
                        contracts = float(ep.get("contracts", 0) or 0)
                        pos = Position(
                            strategy_id=None,
                            account_id=account_id,
                            symbol=sym,
                            side=side,
                            quantity=contracts,
                            entry_price=entry_price,
                            mark_price=mark_price,
                            layer=0,
                        )
                        session.add(pos)
                        logger.info("Sync: created local record for exchange position %s %s contracts=%s", sym, side, contracts)

                await session.commit()
        except Exception as e:
            logger.error("Position sync for account %d failed: %s", account_id, e)

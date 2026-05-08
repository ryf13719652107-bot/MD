"""Position synchronization between local DB and exchange."""
import time
import logging
from sqlalchemy import select
from ..database import async_session
from ..models.position import Position
from ..models.trade import Trade
from ..config import now_beijing

logger = logging.getLogger(__name__)

_POSITION_SYNC_INTERVAL = 60  # 1 minute


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

                exchange_map: dict[tuple[str, str], dict] = {}
                for ep in exchange_positions:
                    if float(ep.get("contracts", 0)) <= 0:
                        continue
                    sym = (ep.get("symbol") or "").replace("/", "").replace(":USDT", "")
                    side = (ep.get("side") or "").lower()
                    exchange_map[(sym, side)] = ep

                sync_now = now_beijing()
                for lp in local_positions:
                    lp_key = (lp.symbol.replace("/", "").replace(":USDT", ""), lp.side.lower())
                    if lp_key not in exchange_map:
                        exit_price = lp.mark_price or lp.entry_price
                        close_reason = "sync"
                        if lp.tp_limit_order_id and binance_service:
                            try:
                                formatted = binance_service._format_symbol(lp.symbol)
                                oi = await binance_service.exchange.fetch_order(lp.tp_limit_order_id, formatted)
                                avg = float(oi.get("average", 0) or 0)
                                if oi.get("status", "") in ("closed", "filled") and avg > 0:
                                    exit_price = avg
                                    close_reason = "take_profit"
                                    logger.info("Sync: position %d TP order filled @%.4f", lp.id, avg)
                            except Exception:
                                pass
                        exit_pnl = (exit_price - lp.entry_price) * lp.quantity if lp.side == "long" else (lp.entry_price - exit_price) * lp.quantity
                        exit_pnl_pct = ((exit_price - lp.entry_price) / lp.entry_price * 100) if lp.side == "long" and lp.entry_price > 0 else ((lp.entry_price - exit_price) / lp.entry_price * 100) if lp.entry_price > 0 else 0
                        trade = Trade(
                            strategy_id=lp.strategy_id, account_id=lp.account_id,
                            symbol=lp.symbol, side=lp.side, quantity=lp.quantity,
                            entry_price=lp.entry_price, exit_price=exit_price,
                            realized_pnl=exit_pnl, pnl_pct=round(exit_pnl_pct, 2),
                            entry_time=lp.opened_at or sync_now, exit_time=sync_now,
                            layer=lp.layer, close_reason=close_reason,
                        )
                        session.add(trade)
                        lp.closed_at = sync_now
                        logger.warning("Sync: position %d (%s %s) missing on exchange — marked closed with trade record", lp.id, lp.symbol, lp.side)

                local_keys = {(lp.symbol.replace("/", "").replace(":USDT", ""), lp.side.lower()) for lp in local_positions}
                for (sym, side), ep in exchange_map.items():
                    if (sym, side) not in local_keys:
                        logger.warning("Sync: exchange position %s %s not in DB — no local record created", sym, side)

                await session.commit()
        except Exception as e:
            logger.error("Position sync for account %d failed: %s", account_id, e)

"""Position synchronization between local DB and exchange."""
import time
import logging
from sqlalchemy import select
from ..database import async_session
from ..models.position import Position
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

                # Build map of exchange positions: (symbol, side) -> data
                exchange_map: dict[tuple[str, str], dict] = {}
                for ep in exchange_positions:
                    if float(ep.get("contracts", 0)) <= 0:
                        continue
                    sym = (ep.get("symbol") or "").replace("/", "").replace(":USDT", "")
                    side = (ep.get("side") or "").lower()
                    exchange_map[(sym, side)] = ep

                # Close local positions not on exchange — skip if has TP order (let tick handle it)
                sync_now = now_beijing()
                for lp in local_positions:
                    lp_key = (lp.symbol.replace("/", "").replace(":USDT", ""), lp.side.lower())
                    if lp_key not in exchange_map:
                        if lp.tp_limit_order_id:
                            logger.info("Sync: position %d (%s %s) missing on exchange but has TP order — skip, tick will detect", lp.id, lp.symbol, lp.side)
                            continue
                        lp.closed_at = sync_now
                        logger.warning("Sync: position %d (%s %s) missing on exchange — marked closed (no trade record)", lp.id, lp.symbol, lp.side)

                # Log exchange-only positions (don't create orphans — let strategy tick handle)
                local_keys = {(lp.symbol.replace("/", "").replace(":USDT", ""), lp.side.lower()) for lp in local_positions}
                for (sym, side), ep in exchange_map.items():
                    if (sym, side) not in local_keys:
                        logger.warning("Sync: exchange position %s %s not in DB — no local record created", sym, side)

                await session.commit()
        except Exception as e:
            logger.error("Position sync for account %d failed: %s", account_id, e)

"""Position synchronization between local DB and exchange."""
import time
import logging
from collections import defaultdict
from sqlalchemy import select
from ..database import async_session
from ..models.position import Position
from ..models.trade import Trade
from ..config import now_beijing
from ..services.binance_service import BinanceService
from ..services.backup_service import backup_trade

logger = logging.getLogger(__name__)

_POSITION_SYNC_INTERVAL = 60  # 1 minute


def _norm_leg_symbol(sym: str) -> str:
    return (sym or "").replace("/", "").replace(":USDT", "")


def _order_filled(oi: dict) -> bool:
    st = (oi.get("status") or "").lower()
    if st in ("closed", "filled"):
        return True
    if float(oi.get("filled", 0) or 0) > 0 and st not in ("open", "new", "canceled", "cancelled", "expired"):
        return True
    return False


def _parse_order_exit_price(oi: dict) -> float:
    avg = float(oi.get("average", 0) or 0)
    if avg > 0:
        return avg
    info = oi.get("info") or {}
    for k in ("avgPrice", "averagePrice", "price"):
        v = info.get(k)
        if v is not None and str(v):
            try:
                f = float(v)
                if f > 0:
                    return f
            except (TypeError, ValueError):
                pass
    return float(oi.get("price", 0) or 0)


async def _exit_price_from_tp_orders(
    binance_service: BinanceService, symbol: str, order_ids: list[str]
) -> tuple[float | None, str]:
    """Return exit price and close_reason if any TP order id is a filled reduce-only/limit close."""
    formatted = binance_service._format_symbol(symbol)
    for oid in order_ids:
        if not oid:
            continue
        try:
            oi = await binance_service.exchange.fetch_order(oid, formatted)
            if not _order_filled(oi):
                continue
            px = _parse_order_exit_price(oi)
            if px > 0:
                logger.info("Sync: TP order %s filled @%.8f (from exchange)", oid, px)
                return px, "take_profit"
        except Exception as e:
            logger.debug("Sync: fetch_order %s for %s: %s", oid, symbol, e)
    return None, "sync"


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
                    if float(ep.get("contracts", 0) or 0) <= 0:
                        continue
                    sym = _norm_leg_symbol(ep.get("symbol") or "")
                    side = (ep.get("side") or "").lower()
                    exchange_map[(sym, side)] = ep

                sync_now = now_beijing()
                trades_to_backup: list[Trade] = []
                # Group local open rows by (normalized symbol, side) so Martin layers share one TP order id lookup
                by_leg: dict[tuple[str, str], list[Position]] = defaultdict(list)
                for lp in local_positions:
                    sk = (_norm_leg_symbol(lp.symbol), lp.side.lower())
                    by_leg[sk].append(lp)

                for (sym_key, side_low), legs in by_leg.items():
                    if (sym_key, side_low) in exchange_map:
                        continue
                    order_ids: list[str] = []
                    seen: set[str] = set()
                    for lp in legs:
                        oid = (lp.tp_limit_order_id or "").strip()
                        if oid and oid not in seen:
                            seen.add(oid)
                            order_ids.append(oid)

                    exit_price: float | None = None
                    close_reason = "sync"
                    ref = legs[0]
                    if order_ids and binance_service:
                        exit_price, close_reason = await _exit_price_from_tp_orders(
                            binance_service, ref.symbol, order_ids
                        )
                        if exit_price is None or exit_price <= 0:
                            close_reason = "sync"
                    if exit_price is None or exit_price <= 0:
                        exit_price = float(ref.mark_price or ref.entry_price or 0)
                        close_reason = "sync"

                    for lp in legs:
                        exit_pnl = (
                            (exit_price - lp.entry_price) * lp.quantity
                            if lp.side == "long"
                            else (lp.entry_price - exit_price) * lp.quantity
                        )
                        exit_pnl_pct = (
                            ((exit_price - lp.entry_price) / lp.entry_price * 100)
                            if lp.side == "long" and lp.entry_price > 0
                            else ((lp.entry_price - exit_price) / lp.entry_price * 100)
                            if lp.entry_price > 0
                            else 0
                        )
                        trade = Trade(
                            strategy_id=lp.strategy_id,
                            account_id=lp.account_id,
                            symbol=lp.symbol,
                            side=lp.side,
                            quantity=lp.quantity,
                            entry_price=lp.entry_price,
                            exit_price=exit_price,
                            realized_pnl=exit_pnl,
                            pnl_pct=round(exit_pnl_pct, 2),
                            entry_time=lp.opened_at or sync_now,
                            exit_time=sync_now,
                            layer=lp.layer,
                            close_reason=close_reason,
                        )
                        session.add(trade)
                        trades_to_backup.append(trade)
                        lp.closed_at = sync_now
                    logger.warning(
                        "Sync: leg %s %s (%d DB rows) missing on exchange — closed with %s exit=%.8f",
                        sym_key,
                        side_low,
                        len(legs),
                        close_reason,
                        exit_price,
                    )

                local_keys = {(_norm_leg_symbol(lp.symbol), lp.side.lower()) for lp in local_positions}
                for (sym, side), ep in exchange_map.items():
                    if (sym, side) not in local_keys:
                        logger.warning("Sync: exchange position %s %s not in DB — no local record created", sym, side)

                await session.commit()
                for t in trades_to_backup:
                    backup_trade(t)
        except Exception as e:
            logger.error("Position sync for account %d failed: %s", account_id, e)

"""Position synchronization between local DB and exchange."""
import time
import logging
from datetime import datetime
from collections import defaultdict
from sqlalchemy import select
from ..database import async_session
from ..models.position import Position
from ..models.trade import Trade
from ..config import now_beijing
from ..services.binance_service import BinanceService
from ..services.backup_service import backup_trade
from ..services.order_times import exit_time_from_order
from ..services.account_concurrency import hold_account_sync

logger = logging.getLogger(__name__)

_POSITION_SYNC_INTERVAL = 60  # 1 minute


def _norm_leg_symbol(sym: str) -> str:
    return (sym or "").replace("/", "").replace(":USDT", "").replace("_", "").upper()


def _order_filled(oi: dict) -> bool:
    st = (oi.get("status") or "").lower()
    if st in ("closed", "filled"):
        return True
    if float(oi.get("filled", 0) or 0) > 0 and st not in ("open", "new", "canceled", "cancelled", "expired"):
        return True
    return False


def _parse_order_exit_price(oi: dict) -> float:
    """止盈出场价：只认成交均价，禁止回退到限价挂单价 ``price``。

    历史 bug：average 缺失时用 order.price（挂单价）写 Trade，
    空单会出现出场=理论止盈价、盈亏被夸大（如 BLESS 0.018519 vs 真实 0.019019）。
    """
    from .position_manager import _order_fill_avg_price

    return _order_fill_avg_price(oi, 0.0, allow_order_price=False)


async def _exit_from_tp_orders(
    binance_service: BinanceService, symbol: str, order_ids: list[str]
) -> tuple[float | None, str, datetime | None]:
    """Return (exit_price, close_reason, exit_time|None) from filled TP orders."""
    formatted = binance_service._format_symbol(symbol)
    for oid in order_ids:
        if not oid:
            continue
        try:
            if callable(getattr(binance_service, "fetch_order", None)):
                oi = await binance_service.fetch_order(oid, symbol)
            else:
                oi = await binance_service.exchange.fetch_order(oid, formatted)
            if not _order_filled(oi):
                continue
            px = _parse_order_exit_price(oi)
            if px > 0:
                exit_time = exit_time_from_order(oi, fallback=None)
                logger.info("Sync: TP order %s filled @%.8f (from exchange)", oid, px)
                return px, "take_profit", exit_time
            # 已成交但无均价：勿用挂单价；交给上层 mark 兜底并打警告
            logger.warning(
                "Sync: TP order %s filled for %s but no avg/cumQuote (limit price ignored)",
                oid,
                symbol,
            )
            exit_time = exit_time_from_order(oi, fallback=None)
            return None, "take_profit", exit_time
        except Exception as e:
            logger.debug("Sync: fetch_order %s for %s: %s", oid, symbol, e)
    return None, "sync", None


async def _exit_price_from_tp_orders(
    binance_service: BinanceService, symbol: str, order_ids: list[str]
) -> tuple[float | None, str]:
    px, reason, _ = await _exit_from_tp_orders(binance_service, symbol, order_ids)
    return px, reason


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

                # 空列表保护：本地有 open 仓位但交易所返回空，可能是 API 异常
                # （IP 白名单/权限/限频）。用 fetch_balance 二次确认，余额也拉不到则跳过，
                # 避免把仍在交易所的仓位误判为已平仓（"同步平仓"）。
                if not exchange_positions and local_positions:
                    try:
                        balance = await auth_binance.fetch_balance()
                    except Exception:
                        logger.warning(
                            "Sync: fetch_positions empty + fetch_balance failed — "
                            "skip sync (possible API error) account=%d",
                            account_id,
                        )
                        return
                    from .exchange_factory import extract_margin_balance

                    if extract_margin_balance(auth_binance, balance) <= 0:
                        logger.warning(
                            "Sync: fetch_positions empty + margin missing — "
                            "skip sync (possible API error) account=%d",
                            account_id,
                        )
                        return

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
                    # 防与 check_tp_fills 竞态：写 Trade 前按 id 重查仍 open 的行
                    leg_ids = [int(lp.id) for lp in legs if lp.id is not None]
                    if leg_ids:
                        fresh = await session.execute(
                            select(Position).where(
                                Position.id.in_(leg_ids),
                                Position.closed_at.is_(None),
                            )
                        )
                        legs = list(fresh.scalars().all())
                    else:
                        legs = [lp for lp in legs if lp.closed_at is None]
                    if not legs:
                        continue

                    # 与 check_tp 一致：折叠符号竞态重复 L0，只对真实层写 Trade
                    from .position_manager import (
                        _collapse_phantom_l0_duplicates,
                        claim_open_position_close,
                        find_existing_leg_close_trade,
                    )

                    original_legs = list(legs)
                    legs = _collapse_phantom_l0_duplicates(original_legs, now=sync_now)
                    kept_ids = {
                        int(lp.id)
                        for lp in legs
                        if lp.id is not None and lp.closed_at is None
                    }
                    legs = [lp for lp in legs if lp.closed_at is None]
                    if not legs:
                        async with hold_account_sync(account_id):
                            for lp in original_legs:
                                if lp.id is None or lp.closed_at is None:
                                    continue
                                await claim_open_position_close(
                                    session, int(lp.id), sync_now, symbol_norm=sym_key
                                )
                        continue

                    order_ids: list[str] = []
                    sl_ids: list[str] = []
                    seen: set[str] = set()
                    sl_seen: set[str] = set()
                    for lp in legs:
                        oid = (lp.tp_limit_order_id or "").strip()
                        if oid and oid not in seen:
                            seen.add(oid)
                            order_ids.append(oid)
                        sl_oid = str(getattr(lp, "sl_stop_order_id", None) or "").strip()
                        if sl_oid and sl_oid not in sl_seen:
                            sl_seen.add(sl_oid)
                            sl_ids.append(sl_oid)

                    exit_price: float | None = None
                    close_reason = "sync"
                    exit_time = sync_now
                    ref = legs[0]
                    if order_ids and binance_service:
                        exit_price, close_reason, order_exit_time = await _exit_from_tp_orders(
                            binance_service, ref.symbol, order_ids
                        )
                        if order_exit_time is not None:
                            exit_time = order_exit_time
                        if exit_price is None or exit_price <= 0:
                            close_reason = "sync"
                    if (exit_price is None or exit_price <= 0) and sl_ids and binance_service:
                        sl_px, sl_reason, sl_exit_time = await _exit_from_tp_orders(
                            binance_service, ref.symbol, sl_ids
                        )
                        if sl_px and sl_px > 0:
                            exit_price = sl_px
                            close_reason = "stop_loss"
                            if sl_exit_time is not None:
                                exit_time = sl_exit_time
                        elif sl_reason == "take_profit":
                            close_reason = "sync"
                    if exit_price is None or exit_price <= 0:
                        exit_price = float(ref.mark_price or ref.entry_price or 0)
                        close_reason = "sync"
                    leftover = (
                        sl_ids
                        if close_reason == "take_profit"
                        else (order_ids if close_reason == "stop_loss" else order_ids + sl_ids)
                    )
                    if leftover and binance_service:
                        for oid in leftover:
                            try:
                                await binance_service.cancel_order(oid, ref.symbol)
                            except Exception:
                                pass

                    closed_n = 0
                    async with hold_account_sync(account_id):
                        for lp in original_legs:
                            if lp.id is None or int(lp.id) in kept_ids:
                                continue
                            if lp.closed_at is not None:
                                await claim_open_position_close(
                                    session, int(lp.id), exit_time, symbol_norm=sym_key
                                )
                        for lp in legs:
                            if lp.closed_at is not None:
                                continue
                            if lp.id is not None:
                                won = await claim_open_position_close(
                                    session, int(lp.id), exit_time, symbol_norm=sym_key
                                )
                                if not won:
                                    logger.warning(
                                        "Sync: skip Trade for %s %s pos=%s — already claimed",
                                        sym_key,
                                        side_low,
                                        lp.id,
                                    )
                                    continue
                            lp.closed_at = exit_time
                            lp.symbol = sym_key
                            entry_ts = lp.opened_at or sync_now
                            dup = await find_existing_leg_close_trade(
                                session,
                                strategy_id=int(lp.strategy_id or 0),
                                symbol=sym_key,
                                side=lp.side,
                                layer=int(lp.layer or 0),
                                entry_time=entry_ts,
                                entry_price=float(lp.entry_price or 0),
                                quantity=float(lp.quantity or 0),
                            )
                            if dup is not None:
                                logger.warning(
                                    "Sync: skip duplicate Trade for %s %s "
                                    "(existing trade id=%s)",
                                    sym_key,
                                    side_low,
                                    getattr(dup, "id", None),
                                )
                                continue
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
                                symbol=sym_key,
                                side=lp.side,
                                quantity=lp.quantity,
                                entry_price=lp.entry_price,
                                exit_price=exit_price,
                                realized_pnl=exit_pnl,
                                pnl_pct=round(exit_pnl_pct, 2),
                                entry_time=entry_ts,
                                exit_time=exit_time,
                                layer=lp.layer,
                                close_reason=close_reason,
                            )
                            session.add(trade)
                            trades_to_backup.append(trade)
                            closed_n += 1
                    if closed_n:
                        logger.warning(
                            "Sync: leg %s %s (%d DB rows) missing on exchange — closed with %s exit=%.8f",
                            sym_key,
                            side_low,
                            closed_n,
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

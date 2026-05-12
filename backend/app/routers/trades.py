import csv
import io
import logging
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from sqlalchemy import select, func, delete
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from ..database import get_db
from ..models.trade import Trade
from ..schemas.trade import TradeResponse, TradeListResponse

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/trades", tags=["trades"])


def _parse_backup_datetime(val) -> datetime | None:
    """JSONL backup stores times as ISO strings; ORM needs naive datetime objects."""
    if val is None:
        return None
    if isinstance(val, datetime):
        return val.replace(tzinfo=None) if val.tzinfo else val
    if isinstance(val, str):
        s = val.strip()
        if not s:
            return None
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(s)
        except ValueError:
            try:
                dt = datetime.strptime(s[:19], "%Y-%m-%d %H:%M:%S")
            except ValueError:
                return None
        return dt.replace(tzinfo=None) if dt.tzinfo else dt
    return None


def _restore_pk(val) -> int | None:
    if val is None:
        return None
    try:
        return int(val)
    except (TypeError, ValueError):
        return None


@router.get("", response_model=TradeListResponse)
async def list_trades(
    symbol: str | None = None,
    strategy_id: int | None = None,
    account_id: int | None = None,
    limit: int = Query(default=50, le=500),
    offset: int = Query(default=0, ge=0),
    db: AsyncSession = Depends(get_db),
):
    stmt = select(Trade).order_by(Trade.exit_time.desc())
    count_stmt = select(func.count(Trade.id))

    if symbol:
        stmt = stmt.where(Trade.symbol == symbol)
        count_stmt = count_stmt.where(Trade.symbol == symbol)

    if strategy_id is not None:
        stmt = stmt.where(Trade.strategy_id == strategy_id)
        count_stmt = count_stmt.where(Trade.strategy_id == strategy_id)

    if account_id is not None:
        stmt = stmt.where(Trade.account_id == account_id)
        count_stmt = count_stmt.where(Trade.account_id == account_id)

    stmt = stmt.offset(offset).limit(limit)
    result = await db.execute(stmt)
    trades = result.scalars().all()

    count_result = await db.execute(count_stmt)
    total = count_result.scalar() or 0

    return TradeListResponse(
        trades=[TradeResponse.model_validate(t) for t in trades],
        total=total,
    )


@router.delete("/{trade_id}", status_code=204)
async def delete_trade(trade_id: int, db: AsyncSession = Depends(get_db)):
    trade = await db.get(Trade, trade_id)
    if not trade:
        raise HTTPException(status_code=404, detail="Trade not found")
    await db.delete(trade)
    await db.commit()


@router.delete("", status_code=204)
async def delete_all_trades(db: AsyncSession = Depends(get_db)):
    await db.execute(delete(Trade))
    await db.commit()


@router.get("/backup-stats")
async def get_backup_stats(account_id: int = Query(..., ge=1, description="只统计该账户在备份中的条数")):
    from ..services.backup_service import backup_stats

    return backup_stats(account_id)


@router.post("/restore")
async def restore_trades(
    db: AsyncSession = Depends(get_db),
    account_id: int = Query(..., ge=1, description="只恢复该账户对应的备份行，不影响其他账户"),
):
    """Re-insert backed-up trades for one account only. Skips rows whose id already exists."""
    from ..services.backup_service import restore_trades_from_backup

    backups = restore_trades_from_backup(account_id)
    if not backups:
        return {"restored": 0, "skipped": 0, "account_id": account_id, "message": "No backup records found for this account"}

    restored = 0
    skipped = 0
    invalid = 0
    for d in backups:
        row_account = _restore_pk(d.get("account_id"))
        if row_account != account_id:
            invalid += 1
            continue
        pk = _restore_pk(d.get("id"))
        if pk is not None:
            existing = await db.get(Trade, pk)
            if existing:
                skipped += 1
                continue
        entry_time = _parse_backup_datetime(d.get("entry_time"))
        exit_time = _parse_backup_datetime(d.get("exit_time"))
        symbol = d.get("symbol")
        side = d.get("side")
        if not symbol or not side or row_account is None or entry_time is None or exit_time is None:
            invalid += 1
            logger.warning("Restore skip: missing fields or bad times in backup row id=%r", d.get("id"))
            continue

        strategy_raw = d.get("strategy_id")
        strategy_id = None if strategy_raw is None else _restore_pk(strategy_raw)

        kwargs = dict(
            strategy_id=strategy_id,
            account_id=row_account,
            symbol=str(symbol),
            side=str(side),
            quantity=float(d.get("quantity") or 0),
            entry_price=float(d.get("entry_price") or 0),
            exit_price=float(d.get("exit_price") or 0),
            realized_pnl=float(d.get("realized_pnl") or 0),
            pnl_pct=float(d.get("pnl_pct") or 0),
            entry_time=entry_time,
            exit_time=exit_time,
            layer=int(d.get("layer") or 0),
            close_reason=str(d.get("close_reason") or "sync")[:50],
        )
        if pk is not None:
            kwargs["id"] = pk
        trade = Trade(**kwargs)
        db.add(trade)
        restored += 1

    try:
        await db.commit()
    except IntegrityError as e:
        await db.rollback()
        logger.exception("Trade restore commit failed")
        orig = getattr(e, "orig", None)
        raise HTTPException(
            status_code=400,
            detail=f"恢复写入失败（数据库约束）：{orig or e}",
        ) from e
    out = {"restored": restored, "skipped": skipped, "total": len(backups), "account_id": account_id}
    if invalid:
        out["invalid"] = invalid
    return out


@router.get("/export")
async def export_trades(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Trade).order_by(Trade.exit_time.desc()).limit(10000))
    trades = result.scalars().all()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "ID", "Symbol", "Side", "Quantity", "Entry Price", "Exit Price",
        "Realized PnL", "PnL %", "Entry Time", "Exit Time", "Layer", "Close Reason"
    ])
    for t in trades:
        writer.writerow([
            t.id, t.symbol, t.side, t.quantity, t.entry_price, t.exit_price,
            t.realized_pnl, t.pnl_pct, t.entry_time, t.exit_time, t.layer, t.close_reason
        ])

    output.seek(0)
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=trades.csv"},
    )

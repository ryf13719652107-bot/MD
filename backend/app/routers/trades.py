import csv
import io
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from sqlalchemy import select, func, delete
from sqlalchemy.ext.asyncio import AsyncSession
from ..database import get_db
from ..models.trade import Trade
from ..schemas.trade import TradeResponse, TradeListResponse

router = APIRouter(prefix="/api/trades", tags=["trades"])


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

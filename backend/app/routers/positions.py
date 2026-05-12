from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from datetime import datetime
from ..database import get_db
from ..config import now_beijing
from ..models.position import Position
from ..models.account import Account
from ..schemas.position import PositionResponse
from ..services.encryption import decrypt
from ..services.binance_service import get_binance_service
from ..services.backup_service import backup_trade

router = APIRouter(prefix="/api/positions", tags=["positions"])


@router.get("", response_model=list[PositionResponse])
async def list_positions(
    strategy_id: int | None = None,
    symbol: str | None = None,
    account_id: int | None = None,
    db: AsyncSession = Depends(get_db),
):
    stmt = select(Position).where(Position.closed_at.is_(None))
    if strategy_id is not None:
        stmt = stmt.where(Position.strategy_id == strategy_id)
    if symbol:
        stmt = stmt.where(Position.symbol == symbol)
    if account_id is not None:
        stmt = stmt.where(Position.account_id == account_id)
    result = await db.execute(stmt)
    positions = list(result.scalars().all())

    now = now_beijing()
    dirty = False
    for p in positions:
        if p.opened_at is None:
            p.opened_at = now
            dirty = True
    if dirty:
        await db.commit()

    return [PositionResponse.model_validate(p) for p in positions]


@router.post("/{position_id}/close")
async def close_position(position_id: int, db: AsyncSession = Depends(get_db)):
    position = await db.get(Position, position_id)
    if not position or position.closed_at:
        raise HTTPException(status_code=404, detail="Position not found or already closed")

    account = await db.get(Account, position.account_id)
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")

    api_key = decrypt(account.api_key_encrypted)
    api_secret = decrypt(account.api_secret_encrypted)
    binance = await get_binance_service(api_key, api_secret, account.testnet, account.hedge_mode)

    # Cancel existing TP limit order before closing
    if position.tp_limit_order_id:
        try:
            await binance.cancel_order(position.tp_limit_order_id, position.symbol)
        except Exception:
            pass

    result = await binance.close_position(position.symbol, position.side)
    if not result or not result.get("id"):
        raise HTTPException(status_code=500, detail="Exchange did not confirm the close order")

    exit_price = float(result.get("average", 0) or result.get("price", 0) or 0)
    if exit_price <= 0:
        exit_price = position.mark_price or position.entry_price

    from ..models.trade import Trade
    trade = Trade(
        strategy_id=position.strategy_id,
        account_id=position.account_id,
        symbol=position.symbol,
        side=position.side,
        quantity=position.quantity,
        entry_price=position.entry_price,
        exit_price=exit_price,
        realized_pnl=(exit_price - position.entry_price) * position.quantity if position.side == "long" else (position.entry_price - exit_price) * position.quantity,
        pnl_pct=round(((exit_price - position.entry_price) / position.entry_price * 100) if position.side == "long" else ((position.entry_price - exit_price) / position.entry_price * 100), 2),
        entry_time=position.opened_at or now_beijing(),
        exit_time=now_beijing(),
        layer=position.layer,
        close_reason="manual",
    )
    db.add(trade)
    backup_trade(trade)
    position.closed_at = now_beijing()
    await db.commit()

    return {"status": "closed", "id": position_id}

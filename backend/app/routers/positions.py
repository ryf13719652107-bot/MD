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
    return [PositionResponse.model_validate(p) for p in result.scalars().all()]


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
    binance = get_binance_service(api_key, api_secret, account.testnet, account.hedge_mode)

    result = await binance.close_position(position.symbol, position.side)
    if not result or not result.get("id"):
        raise HTTPException(status_code=500, detail="Exchange did not confirm the close order")

    position.closed_at = now_beijing()
    await db.commit()

    return {"status": "closed", "id": position_id}

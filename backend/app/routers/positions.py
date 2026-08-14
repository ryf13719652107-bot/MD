from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from datetime import datetime
from ..database import get_db
from ..config import now_beijing
from ..models.position import Position
from ..models.account import Account
from ..schemas.position import PositionResponse
from ..services.exchange_factory import get_exchange_for_account
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

    binance = await get_exchange_for_account(account)

    from ..services.position_manager import _order_fill_avg_price

    bot_owned = bool((position.exchange_order_id or "").strip())
    result = None
    exit_price = 0.0

    if bot_owned:
        # 只撤机器人自己的止盈单号
        if position.tp_limit_order_id:
            try:
                await binance.cancel_order(position.tp_limit_order_id, position.symbol)
            except Exception:
                pass
        close_qty = float(position.quantity or 0)
        if close_qty <= 0:
            raise HTTPException(status_code=400, detail="持仓数量无效")
        result = await binance.close_position_qty(
            position.symbol, position.side, close_qty
        )
        if not result or not result.get("id"):
            raise HTTPException(
                status_code=500, detail="Exchange did not confirm the close order"
            )
        exit_price = _order_fill_avg_price(result, 0.0, allow_order_price=False)
        try:
            from ..services.account_position_stream import account_position_stream

            account_position_stream.apply_local_close(
                int(position.account_id or 0),
                position.symbol,
                position.side,
                close_qty,
            )
        except Exception:
            pass
    # 无 exchange_order_id：视为非机器人仓，只清本地记录，不碰交易所
    if exit_price <= 0:
        exit_price = position.mark_price or position.entry_price

    from ..models.trade import Trade
    from ..services.order_times import exit_time_from_order

    exit_time = exit_time_from_order(result)
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
        entry_time=position.opened_at or exit_time,
        exit_time=exit_time,
        layer=position.layer,
        close_reason="manual",
    )
    db.add(trade)
    position.closed_at = exit_time
    await db.commit()
    backup_trade(trade)

    return {"status": "closed", "id": position_id}

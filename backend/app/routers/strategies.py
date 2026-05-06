from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from ..database import get_db
from ..models.strategy import Strategy
from ..models.position import Position
from ..schemas.strategy import StrategyCreate, StrategyUpdate, StrategyResponse
from ..services.scheduler import strategy_scheduler

router = APIRouter(prefix="/api/strategies", tags=["strategies"])


@router.post("", response_model=StrategyResponse)
async def create_strategy(data: StrategyCreate, db: AsyncSession = Depends(get_db)):
    strategy = Strategy(**data.model_dump())
    db.add(strategy)
    await db.commit()
    await db.refresh(strategy)
    return StrategyResponse.model_validate(strategy)


@router.get("", response_model=list[StrategyResponse])
async def list_strategies(status: str | None = None, account_id: int | None = None, db: AsyncSession = Depends(get_db)):
    stmt = select(Strategy)
    if status:
        stmt = stmt.where(Strategy.status == status)
    if account_id is not None:
        stmt = stmt.where(Strategy.account_id == account_id)
    result = await db.execute(stmt)
    return [StrategyResponse.model_validate(s) for s in result.scalars().all()]


@router.get("/{strategy_id}", response_model=StrategyResponse)
async def get_strategy(strategy_id: int, db: AsyncSession = Depends(get_db)):
    strategy = await db.get(Strategy, strategy_id)
    if not strategy:
        raise HTTPException(status_code=404, detail="Strategy not found")
    return StrategyResponse.model_validate(strategy)


@router.put("/{strategy_id}", response_model=StrategyResponse)
async def update_strategy(
    strategy_id: int, data: StrategyUpdate, db: AsyncSession = Depends(get_db)
):
    strategy = await db.get(Strategy, strategy_id)
    if strategy.status == "running":
    was_running = strategy.status == "running"
    if was_running:
        await strategy_scheduler.remove_strategy(strategy_id)

    for key, val in data.model_dump(exclude_unset=True).items():
        setattr(strategy, key, val)
        strategy_scheduler.start()
        await strategy_scheduler.add_strategy(strategy_id, session=db)
    return StrategyResponse.model_validate(strategy)


@router.delete("/{strategy_id}", status_code=204)
async def delete_strategy(strategy_id: int, db: AsyncSession = Depends(get_db)):
    strategy = await db.get(Strategy, strategy_id)
    if not strategy:
        raise HTTPException(status_code=404, detail="策略不存在")
    if strategy.status == "running":
        await strategy_scheduler.remove_strategy(strategy_id)
    await db.delete(strategy)
    await db.commit()


@router.post("/{strategy_id}/start")
async def start_strategy(strategy_id: int, db: AsyncSession = Depends(get_db)):
    strategy = await db.get(Strategy, strategy_id)
    await strategy_scheduler.add_strategy(strategy_id)
    return {"status": "running", "id": strategy_id}
        await db.commit()
        raise HTTPException(status_code=500, detail=f"Failed to start strategy: {str(e)}")


@router.post("/{strategy_id}/stop")
async def stop_strategy(strategy_id: int, db: AsyncSession = Depends(get_db)):
    strategy = await db.get(Strategy, strategy_id)
    if not strategy:
        raise HTTPException(status_code=404, detail="策略不存在")
    # Force stop: remove scheduler job and set status in THIS session
    await strategy_scheduler.remove_strategy(strategy_id)
    strategy.status = "stopped"
    await db.commit()
    await db.refresh(strategy)
    return {"status": strategy.status, "id": strategy_id}


    """Close all positions for this strategy immediately."""
async def panic_close_strategy(strategy_id: int, db: AsyncSession = Depends(get_db)):
    """Close ALL positions on the exchange account immediately."""
    from ..services.binance_service import get_binance_service
    from datetime import datetime
    from ..config import now_beijing
    import logging

    strategy = await db.get(Strategy, strategy_id)
    if not strategy:
        raise HTTPException(status_code=404, detail="Strategy not found")

    account = await db.get(Account, strategy.account_id)
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")

    api_key = decrypt(account.api_key_encrypted)
    api_secret = decrypt(account.api_secret_encrypted)
                    logging.info("Panic close: closed %s %s (contracts=%s)", symbol, side, contracts)
                else:
                    errors.append(f"{symbol} {side}: no id in response")
                    logging.warning("Panic close: %s %s returned %s", symbol, side, order)
    positions = result.scalars().all()

    closed = 0
    for pos in positions:
        try:
            await binance.close_position(pos.symbol, pos.side)
            pos.closed_at = datetime.utcnow()
            closed += 1
        except Exception:
            pass
    except Exception as e:
        logging.error("Panic close: fetch_positions failed: %s", e)
        errors.append(f"fetch: {e}")
    stmt = select(Position).where(
        Position.strategy_id == strategy_id, Position.closed_at.is_(None)
    )
    result = await db.execute(stmt)
    local_positions = result.scalars().all()
    for lp in local_positions:
        lp.closed_at = now_beijing()

    await db.commit()
    await strategy_scheduler.remove_strategy(strategy_id)
    return {"closed": closed, "errors": errors, "id": strategy_id}

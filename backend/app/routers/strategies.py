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
    from ..models.account import Account
    account = await db.get(Account, data.account_id)
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")
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
    if not strategy:
        raise HTTPException(status_code=404, detail="Strategy not found")

    was_running = strategy.status == "running"
    if was_running:
        await strategy_scheduler.remove_strategy(strategy_id)

    for key, val in data.model_dump(exclude_unset=True).items():
        setattr(strategy, key, val)
    await db.commit()
    await db.refresh(strategy)

    if was_running:
        strategy_scheduler.start()
        await strategy_scheduler.add_strategy(strategy_id, session=db)

    return StrategyResponse.model_validate(strategy)


@router.delete("/{strategy_id}", status_code=204)
async def delete_strategy(strategy_id: int, db: AsyncSession = Depends(get_db)):
    strategy = await db.get(Strategy, strategy_id)
    if not strategy:
        raise HTTPException(status_code=404, detail="Strategy not found")
    if strategy.status == "running":
        await strategy_scheduler.remove_strategy(strategy_id)
    await db.delete(strategy)
    await db.commit()


@router.post("/{strategy_id}/start")
async def start_strategy(strategy_id: int, db: AsyncSession = Depends(get_db)):
    strategy = await db.get(Strategy, strategy_id)
    if not strategy:
        raise HTTPException(status_code=404, detail="Strategy not found")

    from ..services.coin_pool_service import coin_pool_service
    from ..services.binance_service import get_public_binance

    # Immediate coin pool refresh if configured
    if strategy.use_coin_pool and strategy.coin_pool_fetch_mode == "immediate":
        try:
            public_binance = get_public_binance()
            await coin_pool_service.refresh_pool(public_binance)
        except Exception:
            pass

    await strategy_scheduler.add_strategy(strategy_id, session=db)
    return {"status": "running", "id": strategy_id}


@router.post("/{strategy_id}/stop")
async def stop_strategy(strategy_id: int, db: AsyncSession = Depends(get_db)):
    strategy = await db.get(Strategy, strategy_id)
    if not strategy:
        raise HTTPException(status_code=404, detail="Strategy not found")
    await strategy_scheduler.remove_strategy(strategy_id)
    strategy.status = "stopped"
    await db.commit()
    return {"status": "stopped", "id": strategy_id}


@router.post("/{strategy_id}/panic-close")
async def panic_close_strategy(strategy_id: int, db: AsyncSession = Depends(get_db)):
    """Close ALL positions on the exchange account immediately."""
    from ..services.binance_service import get_binance_service
    from ..services.encryption import decrypt
    from ..models.account import Account
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
    binance = get_binance_service(api_key, api_secret, account.testnet, account.hedge_mode)

    closed = 0
    errors = []
    try:
        exchange_positions = await binance.fetch_positions()
        logging.info("Panic close: found %d raw positions", len(exchange_positions))

        # Group by (symbol, side) and sum contracts
        grouped: dict[tuple[str, str], float] = {}
        for ep in exchange_positions:
            contracts = float(ep.get("contracts", 0) or 0)
            if contracts <= 0:
                continue
            symbol = (ep.get("symbol") or "").replace("/", "").replace(":USDT", "")
            side = (ep.get("side") or "").lower()
            key = (symbol, side)
            grouped[key] = grouped.get(key, 0) + contracts

        for (symbol, side), contracts in grouped.items():
            try:
                result = await binance.close_position(symbol, side)
                if result and result.get("id"):
                    closed += 1
                    logging.info("Panic close: closed %s %s (contracts=%s)", symbol, side, contracts)
                else:
                    errors.append(f"{symbol} {side}: no id in response")
            except Exception as e:
                errors.append(f"{symbol} {side}: {e}")
    except Exception as e:
        logging.error("Panic close: fetch_positions failed: %s", e)
        errors.append(f"fetch: {e}")

    # Also close local DB positions for this strategy
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


@router.get("/{strategy_id}/exchange-positions")
async def get_exchange_positions(strategy_id: int, db: AsyncSession = Depends(get_db)):
    from ..services.binance_service import get_binance_service
    from ..services.encryption import decrypt
    from ..models.account import Account

    strategy = await db.get(Strategy, strategy_id)
    if not strategy:
        raise HTTPException(status_code=404, detail="Strategy not found")

    account = await db.get(Account, strategy.account_id)
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")

    api_key = decrypt(account.api_key_encrypted)
    api_secret = decrypt(account.api_secret_encrypted)
    binance = get_binance_service(api_key, api_secret, account.testnet, account.hedge_mode)

    try:
        positions = await binance.fetch_positions()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to fetch positions: {e}")

    result = []
    for p in positions:
        contracts = float(p.get("contracts", 0) or 0)
        if contracts > 0:
            symbol = (p.get("symbol") or "").replace("/", "").replace(":USDT", "")
            side = (p.get("side") or "").lower()
            entry_price = float(p.get("entryPrice", 0) or 0)
            mark_price = float(p.get("markPrice", 0) or 0)
            notional = float(p.get("notional", 0) or 0)
            pnl = float(p.get("unrealizedPnl", 0) or 0)
            pnl_pct = ((entry_price - mark_price) / entry_price * 100) if side == "short" and entry_price > 0 else ((mark_price - entry_price) / entry_price * 100) if entry_price > 0 else 0
            result.append({
                "symbol": symbol,
                "side": side,
                "usdt": round(notional, 0),
                "entry_price": round(entry_price, 4),
                "mark_price": round(mark_price, 4),
                "unrealized_pnl": round(pnl, 2),
                "pnl_pct": round(pnl_pct, 2),
            })
    return result


@router.get("/{strategy_id}/logs")
async def get_strategy_logs(strategy_id: int, limit: int = 50):
    from ..services.log_service import strategy_log_service
    return strategy_log_service.get(strategy_id, limit)

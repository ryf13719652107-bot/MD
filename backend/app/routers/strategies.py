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
            public_binance = await get_public_binance()
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
    # remove_strategy sets status in its own session; refresh and commit here too for consistency
    await db.refresh(strategy)
    await db.commit()
    return {"status": "stopped", "id": strategy_id}


@router.post("/{strategy_id}/panic-close")
async def panic_close_strategy(strategy_id: int, db: AsyncSession = Depends(get_db)):
    """Emergency close: close ALL exchange positions for this strategy's account at market price."""
    from ..services.binance_service import get_binance_service
    from ..services.encryption import decrypt
    from ..models.account import Account
    from ..models.trade import Trade
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
    binance = await get_binance_service(api_key, api_secret, account.testnet, account.hedge_mode)

    # Get actual exchange positions — the source of truth (do this FIRST, regardless of DB state)
    try:
        raw_positions = await binance.fetch_positions()
    except Exception as e:
        logging.error("Panic close: fetch_positions failed: %s", e)
        raise HTTPException(status_code=502, detail=f"无法获取交易所持仓: {e}")

    # Build map of (symbol_no_slash, side) → exchange contracts
    exchange_map: dict[tuple[str, str], float] = {}
    for ep in raw_positions:
        contracts = float(ep.get("contracts", 0) or 0)
        if contracts <= 0:
            continue
        sym = (ep.get("symbol") or "").replace("/", "").replace(":USDT", "")
        sd = (ep.get("side") or "").lower()
        exchange_map[(sym, sd)] = exchange_map.get((sym, sd), 0) + contracts

    if not exchange_map:
        await strategy_scheduler.remove_strategy(strategy_id)
        return {"closed": 0, "failed": 0, "results": [], "id": strategy_id}

    # Load this strategy's open DB positions (for record keeping only)
    stmt = select(Position).where(
        Position.strategy_id == strategy_id, Position.closed_at.is_(None)
    )
    result = await db.execute(stmt)
    db_positions = list(result.scalars().all())

    results = []
    now = now_beijing()

    # Close each exchange position
    for (symbol, side), contracts in exchange_map.items():
        try:
            close_side = "sell" if side == "long" else "buy"
            ps = "LONG" if side == "long" else "SHORT"
            order = await binance.create_market_order(
                symbol, close_side, contracts,
                reduce_only=True, position_side=ps,
            )
            exit_price = float(order.get("average", 0) or order.get("price", 0) or 0)
            results.append({"symbol": symbol, "side": side, "status": "ok", "exit_price": exit_price})
            logging.info("Panic close: closed %s %s contracts=%.4f", symbol, side, contracts)
        except Exception as e:
            results.append({"symbol": symbol, "side": side, "status": "failed", "error": str(e)})
            logging.error("Panic close: failed %s %s: %s", symbol, side, e)

    # Match to DB positions and mark closed
    for (symbol, side), contracts in exchange_map.items():
        matching = [p for p in db_positions if p.symbol == symbol and p.side == side]
        for p in matching:
            result = next((r for r in results if r["symbol"] == symbol and r["side"] == side), None)
            ep = (result and result.get("exit_price", 0)) or 0
            ep = ep if ep > 0 else (p.mark_price or p.entry_price)
            pnl = (ep - p.entry_price) * p.quantity if p.side == "long" else (p.entry_price - ep) * p.quantity
            pct = ((ep - p.entry_price) / p.entry_price * 100) if p.side == "long" else ((p.entry_price - ep) / p.entry_price * 100)
            trade = Trade(
                strategy_id=strategy_id, account_id=account.id,
                symbol=symbol, side=p.side, quantity=p.quantity,
                entry_price=p.entry_price, exit_price=ep,
                realized_pnl=pnl, pnl_pct=round(pct, 2),
                entry_time=p.opened_at, exit_time=now,
                layer=p.layer, close_reason="panic",
            )
            db.add(trade)
            p.closed_at = now

    # Also close any DB-only positions (not on exchange)
    for p in db_positions:
        if p.closed_at is None:
            p.closed_at = now

    await db.commit()
    await strategy_scheduler.remove_strategy(strategy_id)

    closed_count = sum(1 for r in results if r["status"] == "ok")
    failed_count = sum(1 for r in results if r["status"] == "failed")
    return {"closed": closed_count, "failed": failed_count, "results": results, "id": strategy_id}


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
    binance = await get_binance_service(api_key, api_secret, account.testnet, account.hedge_mode)

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

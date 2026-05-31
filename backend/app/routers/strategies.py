from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from ..database import get_db
from ..models.strategy import Strategy
from ..models.position import Position
from ..schemas.strategy import StrategyCreate, StrategyUpdate, StrategyResponse
from ..schemas.coin_pool import CoinPoolResponse
from ..services.scheduler import strategy_scheduler
from ..services.backup_service import backup_trade
from ..services.coin_pool_service import coin_pool_service
from ..services.strategy_flags import (
    exclude_delisting_enabled,
    exclude_mainstream_enabled,
    exclude_funding_enabled,
    funding_rate_threshold_pct,
    normalize_coin_pool_source,
)

router = APIRouter(prefix="/api/strategies", tags=["strategies"])


def _panic_symbol_key(sym: str) -> str:
    return (sym or "").replace("/", "").replace(":USDT", "").upper()


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


@router.get("/{strategy_id}/effective-coin-pool", response_model=list[CoinPoolResponse])
async def get_strategy_effective_coin_pool(strategy_id: int, db: AsyncSession = Depends(get_db)):
    """本策略实盘用于新开仓的选币池（成交量 + top_n + TradFi 与调度器一致）。"""
    strategy = await db.get(Strategy, strategy_id)
    if not strategy:
        raise HTTPException(status_code=404, detail="Strategy not found")
    if not strategy.use_coin_pool:
        return []

    from ..services.binance_service import (
        get_public_binance,
        get_strategy_pool_exclude_symbols,
        filter_pool_symbols_by_funding,
    )

    public = await get_public_binance()
    exclude_norm = await get_strategy_pool_exclude_symbols(
        public,
        exclude_tradefi=bool(strategy.exclude_tradefi),
        exclude_delisting=exclude_delisting_enabled(strategy),
        exclude_mainstream=exclude_mainstream_enabled(strategy),
    )

    entries = await coin_pool_service.get_effective_pool_entries(
        source=normalize_coin_pool_source(strategy.coin_pool_source),
        limit=strategy.coin_pool_top_n,
        min_volume_24h=float(strategy.coin_pool_min_volume_24h or 0),
        exclude_symbols_norm=set(exclude_norm) if exclude_norm else None,
    )
    if exclude_funding_enabled(strategy):
        rates_map = {
            _panic_symbol_key(c.symbol): c
            for c in entries
        }
        allowed = await filter_pool_symbols_by_funding(
            public,
            [c.symbol for c in entries],
            direction=strategy.direction,
            threshold_pct=funding_rate_threshold_pct(strategy),
        )
        allowed_norm = {_panic_symbol_key(s) for s in allowed}
        entries = [rates_map[k] for k in allowed_norm if k in rates_map]

    from ..services.coin_pool_presenter import coin_pool_responses_with_funding

    return await coin_pool_responses_with_funding(public, entries)


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

    await coin_pool_service.sync_config_from_running_strategies()

    if strategy.use_coin_pool and strategy.coin_pool_fetch_mode == "immediate":
        try:
            public_binance = await get_public_binance()
            await coin_pool_service.refresh_pool(
                public_binance,
                source=normalize_coin_pool_source(strategy.coin_pool_source),
                limit=strategy.coin_pool_top_n,
            )
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
    """Emergency close: close exchange positions belonging to THIS strategy only."""
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

    stmt_open = select(Position).where(
        Position.strategy_id == strategy_id,
        Position.closed_at.is_(None),
    )
    open_rows = list((await db.execute(stmt_open)).scalars().all())

    if not open_rows:
        await strategy_scheduler.remove_strategy(strategy_id)
        return {
            "closed": 0,
            "failed": 0,
            "results": [],
            "id": strategy_id,
        }

    legs_to_close: dict[tuple[str, str], list[Position]] = {}
    for p in open_rows:
        key = (_panic_symbol_key(p.symbol), p.side.lower())
        legs_to_close.setdefault(key, []).append(p)

    results = []
    now = now_beijing()

    for (sym_key, side), positions in legs_to_close.items():
        try:
            order = await binance.close_position(sym_key, side)
        except Exception as e1:
            results.append({"symbol": sym_key, "side": side, "status": "failed", "error": str(e1)})
            logging.error("Panic close: failed %s %s: %s", sym_key, side, e1)
            continue
        if not order:
            results.append({"symbol": sym_key, "side": side, "status": "failed", "error": "empty_order_response"})
            logging.error("Panic close: empty response %s %s", sym_key, side)
            continue
        exit_price = float(order.get("average", 0) or order.get("price", 0) or 0)
        results.append({"symbol": sym_key, "side": side, "status": "ok", "exit_price": exit_price})
        logging.info("Panic close: closed %s %s", sym_key, side)

    trades_to_backup: list[Trade] = []
    for r in results:
        if r["status"] != "ok":
            continue
        sym_key = r["symbol"]
        side = r["side"]
        exit_price = r.get("exit_price", 0) or 0
        positions = legs_to_close.get((sym_key, side), [])
        for p in positions:
            ep = exit_price if exit_price > 0 else (p.mark_price or p.entry_price)
            pnl = (ep - p.entry_price) * p.quantity if p.side == "long" else (p.entry_price - ep) * p.quantity
            pct = ((ep - p.entry_price) / p.entry_price * 100) if p.side == "long" and p.entry_price > 0 else ((p.entry_price - ep) / p.entry_price * 100) if p.entry_price > 0 else 0
            trade = Trade(
                strategy_id=strategy_id,
                account_id=account.id,
                symbol=p.symbol,
                side=p.side,
                quantity=p.quantity,
                entry_price=p.entry_price,
                exit_price=ep,
                realized_pnl=pnl,
                pnl_pct=round(pct, 2),
                entry_time=p.opened_at or now,
                exit_time=now,
                layer=p.layer,
                close_reason="panic_close",
            )
            db.add(trade)
            trades_to_backup.append(trade)
            p.closed_at = now

    await db.commit()
    for t in trades_to_backup:
        backup_trade(t)
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

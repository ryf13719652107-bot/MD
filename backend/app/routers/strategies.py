from fastapi import APIRouter, Depends, HTTPException, Body
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from ..database import get_db
from ..models.strategy import Strategy
from ..models.strategy_blacklist import StrategySymbolBlacklist
from ..models.position import Position
from ..schemas.strategy import StrategyCreate, StrategyUpdate, StrategyResponse
from ..schemas.coin_pool import CoinPoolResponse
from ..config import now_beijing
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


async def _load_blacklist_map(db: AsyncSession, strategy_ids: list[int]) -> dict[int, list[str]]:
    if not strategy_ids:
        return {}
    rows = (
        await db.execute(
            select(StrategySymbolBlacklist).where(
                StrategySymbolBlacklist.strategy_id.in_(strategy_ids)
            )
        )
    ).scalars().all()
    out: dict[int, list[str]] = {}
    for row in rows:
        out.setdefault(row.strategy_id, []).append(row.symbol)
    return out


async def _to_strategy_response(db: AsyncSession, strategy: Strategy) -> StrategyResponse:
    blacklist_map = await _load_blacklist_map(db, [strategy.id])
    payload = StrategyResponse.model_validate(strategy).model_dump()
    payload["blacklisted_symbols"] = blacklist_map.get(strategy.id, [])
    return StrategyResponse.model_validate(payload)


async def _to_strategy_response_list(db: AsyncSession, strategies: list[Strategy]) -> list[StrategyResponse]:
    blacklist_map = await _load_blacklist_map(db, [s.id for s in strategies])
    out: list[StrategyResponse] = []
    for strategy in strategies:
        payload = StrategyResponse.model_validate(strategy).model_dump()
        payload["blacklisted_symbols"] = blacklist_map.get(strategy.id, [])
        out.append(StrategyResponse.model_validate(payload))
    return out


def _normalize_symbol_input(symbol: str) -> str:
    return _panic_symbol_key(symbol)


async def _panic_exchange_leg_contracts(binance, symbol: str, side: str) -> float:
    """Best-effort exchange check for one leg after emergency close."""
    side_low = (side or "").lower()
    target = _panic_symbol_key(symbol)
    positions = await binance.fetch_positions([symbol])
    total = 0.0
    for pos in positions:
        pos_symbol = _panic_symbol_key(pos.get("symbol") or "")
        pos_side = (pos.get("side") or "").lower()
        if pos_symbol == target and pos_side == side_low:
            total += float(pos.get("contracts", 0) or 0)
    return total


@router.post("", response_model=StrategyResponse)
async def create_strategy(data: StrategyCreate, db: AsyncSession = Depends(get_db)):
    from ..models.account import Account
    account = await db.get(Account, data.account_id)
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")
    strategy = Strategy(**data.model_dump())
    if strategy.use_coin_pool and strategy.coin_pool_fetch_mode == "scheduled":
        strategy.coin_pool_schedule_started_at = now_beijing()
    db.add(strategy)
    await db.commit()
    await db.refresh(strategy)
    return await _to_strategy_response(db, strategy)


@router.get("", response_model=list[StrategyResponse])
async def list_strategies(status: str | None = None, account_id: int | None = None, db: AsyncSession = Depends(get_db)):
    stmt = select(Strategy)
    if status:
        stmt = stmt.where(Strategy.status == status)
    if account_id is not None:
        stmt = stmt.where(Strategy.account_id == account_id)
    result = await db.execute(stmt)
    return await _to_strategy_response_list(db, list(result.scalars().all()))


@router.get("/{strategy_id}", response_model=StrategyResponse)
async def get_strategy(strategy_id: int, db: AsyncSession = Depends(get_db)):
    strategy = await db.get(Strategy, strategy_id)
    if not strategy:
        raise HTTPException(status_code=404, detail="Strategy not found")
    return await _to_strategy_response(db, strategy)


@router.get("/{strategy_id}/effective-coin-pool", response_model=list[CoinPoolResponse])
async def get_strategy_effective_coin_pool(strategy_id: int, db: AsyncSession = Depends(get_db)):
    """????????????????? top_n ???????"""
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
    blacklist_rows = (
        await db.execute(
            select(StrategySymbolBlacklist).where(
                StrategySymbolBlacklist.strategy_id == strategy.id
            )
        )
    ).scalars().all()
    blacklist_norm = {_panic_symbol_key(row.symbol_norm) for row in blacklist_rows if row.symbol_norm}
    merged_exclude = set(exclude_norm) if exclude_norm else set()
    merged_exclude.update(blacklist_norm)

    entries = await coin_pool_service.get_effective_pool_entries(
        source=normalize_coin_pool_source(strategy.coin_pool_source),
        limit=strategy.coin_pool_top_n,
        min_volume_24h=float(strategy.coin_pool_min_volume_24h or 0),
        exclude_symbols_norm=merged_exclude if merged_exclude else None,
        strategy=strategy,
    )
    if exclude_funding_enabled(strategy):
        allowed = await filter_pool_symbols_by_funding(
            public,
            [c.symbol for c in entries],
            direction=strategy.direction,
            threshold_pct=funding_rate_threshold_pct(strategy),
        )
        allowed_norm = {_panic_symbol_key(s) for s in allowed}
        entries = [c for c in entries if _panic_symbol_key(c.symbol) in allowed_norm]

    from ..services.coin_pool_presenter import coin_pool_responses_with_funding

    return await coin_pool_responses_with_funding(public, entries)


@router.post("/{strategy_id}/blacklist", response_model=StrategyResponse)
async def add_blacklist_symbol(
    strategy_id: int,
    payload: dict = Body(...),
    db: AsyncSession = Depends(get_db),
):
    strategy = await db.get(Strategy, strategy_id)
    if not strategy:
        raise HTTPException(status_code=404, detail="Strategy not found")
    raw_symbol = str(payload.get("symbol") or "").strip()
    if not raw_symbol:
        raise HTTPException(status_code=400, detail="symbol is required")
    symbol_norm = _normalize_symbol_input(raw_symbol)
    existing = (
        await db.execute(
            select(StrategySymbolBlacklist.id).where(
                StrategySymbolBlacklist.strategy_id == strategy_id,
                StrategySymbolBlacklist.symbol_norm == symbol_norm,
            )
        )
    ).first()
    if not existing:
        db.add(
            StrategySymbolBlacklist(
                strategy_id=strategy_id,
                symbol=symbol_norm,
                symbol_norm=symbol_norm,
                reason="manual",
            )
        )
        await db.commit()
    return await _to_strategy_response(db, strategy)


@router.delete("/{strategy_id}/blacklist/{symbol}", response_model=StrategyResponse)
async def remove_blacklist_symbol(
    strategy_id: int,
    symbol: str,
    db: AsyncSession = Depends(get_db),
):
    strategy = await db.get(Strategy, strategy_id)
    if not strategy:
        raise HTTPException(status_code=404, detail="Strategy not found")
    symbol_norm = _normalize_symbol_input(symbol)
    rows = (
        await db.execute(
            select(StrategySymbolBlacklist).where(
                StrategySymbolBlacklist.strategy_id == strategy_id,
                StrategySymbolBlacklist.symbol_norm == symbol_norm,
            )
        )
    ).scalars().all()
    for row in rows:
        await db.delete(row)
    if rows:
        await db.commit()
    return await _to_strategy_response(db, strategy)


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

    patch = data.model_dump(exclude_unset=True)
    schedule_keys = {
        "coin_pool_fetch_mode",
        "coin_pool_anchor_hour",
        "coin_pool_anchor_minute",
        "coin_pool_refresh_seconds",
        "coin_pool_source",
        "use_coin_pool",
    }
    for key, val in patch.items():
        setattr(strategy, key, val)
    if (
        strategy.use_coin_pool
        and strategy.coin_pool_fetch_mode == "scheduled"
        and schedule_keys.intersection(patch.keys())
    ):
        strategy.coin_pool_schedule_started_at = now_beijing()
    await db.commit()
    await db.refresh(strategy)

    if was_running:
        from ..services.coin_pool_service import coin_pool_service

        await coin_pool_service.sync_config_from_running_strategies()
        strategy_scheduler.start()
        await strategy_scheduler.add_strategy(strategy_id, session=db)

    return await _to_strategy_response(db, strategy)


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
            try:
                remaining = await _panic_exchange_leg_contracts(binance, sym_key, side)
            except Exception as verify_error:
                results.append({"symbol": sym_key, "side": side, "status": "failed", "error": str(e1)})
                logging.error(
                    "Panic close: failed %s %s: %s; verify failed: %s",
                    sym_key,
                    side,
                    e1,
                    verify_error,
                )
                continue
            if remaining > 0:
                results.append({"symbol": sym_key, "side": side, "status": "failed", "error": str(e1)})
                logging.error(
                    "Panic close: failed %s %s: %s; exchange still has %.8f contracts",
                    sym_key,
                    side,
                    e1,
                    remaining,
                )
                continue
            order = None
            logging.warning(
                "Panic close: close order errored but exchange leg is flat %s %s: %s",
                sym_key,
                side,
                e1,
            )
        if not order:
            try:
                remaining = await _panic_exchange_leg_contracts(binance, sym_key, side)
            except Exception as verify_error:
                results.append({"symbol": sym_key, "side": side, "status": "failed", "error": "empty_order_response"})
                logging.error(
                    "Panic close: empty response %s %s; verify failed: %s",
                    sym_key,
                    side,
                    verify_error,
                )
                continue
            if remaining > 0:
                results.append({"symbol": sym_key, "side": side, "status": "failed", "error": "empty_order_response"})
                logging.error(
                    "Panic close: empty response %s %s; exchange still has %.8f contracts",
                    sym_key,
                    side,
                    remaining,
                )
                continue
        exit_price = float((order or {}).get("average", 0) or (order or {}).get("price", 0) or 0)
        results.append({"symbol": sym_key, "side": side, "status": "ok", "exit_price": exit_price, "order": order})
        logging.info("Panic close: closed %s %s", sym_key, side)

    from ..services.order_times import exit_time_from_order

    trades_to_backup: list[Trade] = []
    for r in results:
        if r["status"] != "ok":
            continue
        sym_key = r["symbol"]
        side = r["side"]
        exit_price = r.get("exit_price", 0) or 0
        exit_time = exit_time_from_order(r.get("order"), fallback=now)
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
                entry_time=p.opened_at or exit_time,
                exit_time=exit_time,
                layer=p.layer,
                close_reason="panic_close",
            )
            db.add(trade)
            trades_to_backup.append(trade)
            p.closed_at = exit_time

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



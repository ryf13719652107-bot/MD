from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from datetime import datetime
import asyncio
from ..database import get_db
from ..config import now_beijing
from ..models.position import Position
from ..models.account import Account
from ..schemas.position import CloseLegRequest, PositionResponse
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


@router.post("/close-leg")
async def close_leg(body: CloseLegRequest, db: AsyncSession = Depends(get_db)):
    """市价平掉该账户某交易对+方向的机器人仓（全部马丁层），不碰同向手动仓。"""
    from ..models.strategy import Strategy
    from ..services.account_concurrency import hold_account_sync
    from ..services.martingale_engine import MartingaleEngine
    from ..services.position_manager import (
        PositionManager,
        _bot_owned_positions,
        _norm_sym,
    )
    from ..services.strategy_concurrency import hold_strategy_symbol, normalize_leg_side
    from ..services.log_service import strategy_log_service

    account_id = int(body.account_id or 0)
    if account_id <= 0:
        raise HTTPException(status_code=400, detail="缺少账户")
    side = normalize_leg_side(body.side)
    if side not in ("long", "short"):
        raise HTTPException(status_code=400, detail="方向无效")
    sym_key = _norm_sym(body.symbol)
    if not sym_key:
        raise HTTPException(status_code=400, detail="交易对无效")

    account = await db.get(Account, account_id)
    if not account:
        raise HTTPException(status_code=404, detail="账户不存在")

    rows = list(
        (
            await db.execute(
                select(Position).where(
                    Position.account_id == account_id,
                    Position.closed_at.is_(None),
                )
            )
        ).scalars().all()
    )
    matched = [
        p
        for p in rows
        if _norm_sym(p.symbol) == sym_key
        and (p.side or "").lower() == side
    ]
    bot_rows = _bot_owned_positions(matched)
    if not bot_rows:
        raise HTTPException(
            status_code=404,
            detail="没有可平的机器人持仓（仅交易所仓请在交易所自行处理）",
        )

    by_strategy: dict[int, list[Position]] = {}
    for p in bot_rows:
        sid = int(p.strategy_id or 0)
        if sid <= 0:
            continue
        by_strategy.setdefault(sid, []).append(p)
    if not by_strategy:
        raise HTTPException(status_code=404, detail="持仓未关联策略，无法平仓")

    client = await get_exchange_for_account(account)
    pm = PositionManager()
    closed_layers = 0

    async with hold_account_sync(account_id):
        for strategy_id, positions in by_strategy.items():
            strategy = await db.get(Strategy, strategy_id)
            if strategy is None:
                raise HTTPException(status_code=404, detail="策略不存在")
            try:
                async with hold_strategy_symbol(
                    strategy_id, sym_key, side, timeout=8.0
                ):
                    positions_data = [
                        {
                            "quantity": p.quantity,
                            "entry_price": p.entry_price,
                        }
                        for p in positions
                    ]
                    eng = MartingaleEngine(
                        base_quantity=0,
                        multiplier=strategy.martingale_mult,
                        max_layers=strategy.max_layers,
                        price_drop_pct=strategy.price_drop_pct,
                        price_drop_multiplier=float(
                            strategy.price_drop_multiplier or 1.0
                        ),
                        take_profit_pct=strategy.take_profit_pct,
                    )
                    avg_entry, _total = eng.get_avg_entry_price(positions_data)
                    mark = float(positions[0].mark_price or positions[0].entry_price or 0)
                    await pm._close_positions(
                        db,
                        strategy,
                        positions[0].symbol or sym_key,
                        client,
                        positions,
                        eng,
                        avg_entry,
                        side,
                        "manual",
                        mark,
                    )
                    closed_here = [
                        p
                        for p in positions
                        if getattr(p, "closed_at", None) is not None
                    ]
                    if not closed_here:
                        raise HTTPException(
                            status_code=500,
                            detail="平仓未完成，请查看策略日志或交易所",
                        )
                    closed_layers += len(closed_here)
                    try:
                        from ..services.wick_spike_runner import wick_spike_runner as _runner

                        buckets = _runner._trailing_mems.get(strategy_id)
                        if buckets is not None:
                            buckets.pop(sym_key, None)
                        inflight = _runner._trailing_close_inflight.get(strategy_id)
                        if inflight is not None:
                            inflight.discard(sym_key)
                    except Exception:
                        pass
                    strategy_log_service.info(
                        strategy_id,
                        f"{sym_key} 手动平仓 — {side} {len(positions)} 层",
                    )
            except HTTPException:
                raise
            except asyncio.TimeoutError:
                raise HTTPException(
                    status_code=409,
                    detail="该仓正在被策略处理，请稍后重试",
                )

    await db.commit()
    return {
        "status": "closed",
        "symbol": sym_key,
        "side": side,
        "layers": closed_layers,
    }


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
        sl_oid = str(getattr(position, "sl_stop_order_id", None) or "").strip()
        if sl_oid:
            try:
                await binance.cancel_order(sl_oid, position.symbol)
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

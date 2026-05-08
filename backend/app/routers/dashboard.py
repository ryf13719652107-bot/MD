import asyncio
import logging
from fastapi import APIRouter, Depends, Query
from sqlalchemy import select, func, case
from sqlalchemy.ext.asyncio import AsyncSession
from datetime import timedelta
from ..database import get_db
from ..config import now_beijing
from ..models.strategy import Strategy
from ..models.trade import Trade
from ..models.bot_config import BotConfig
from ..models.account import Account
from ..schemas.dashboard import DashboardSnapshot
from ..services.encryption import decrypt
from ..services.binance_service import get_binance_service

router = APIRouter(prefix="/api/dashboard", tags=["dashboard"])


@router.get("", response_model=DashboardSnapshot)
async def get_dashboard(
    account_id: int | None = Query(default=None),
    db: AsyncSession = Depends(get_db),
):
    total_balance = 0.0
    available_balance = 0.0
    leverage_multiplier = 0.0
    account_name = ""
    balance_status = "no_account"
    account = None
    binance = None
    filter_account_id = account_id

    # Fetch balance and positions from Binance
    try:
        if filter_account_id:
            result = await db.execute(select(Account).where(Account.id == filter_account_id))
            account = result.scalar()
        else:
            result = await db.execute(select(Account).order_by(Account.id).limit(1))
            account = result.scalar()

        if account:
            account_name = account.name
            filter_account_id = account.id
            try:
                api_key = decrypt(account.api_key_encrypted)
                api_secret = decrypt(account.api_secret_encrypted)
                binance = await get_binance_service(api_key, api_secret, account.testnet, account.hedge_mode)
                balance = await asyncio.wait_for(binance.fetch_balance(), timeout=8.0)
                total_balance = float(balance.get("total", {}).get("USDT", 0) or 0)
                available_balance = float(balance.get("free", {}).get("USDT", 0) or 0)
                balance_status = "ok"
            except asyncio.TimeoutError:
                balance_status = "error"
            except Exception as e:
                logging.error("Balance fetch error for account %s: %s", account.name, e)
                balance_status = "error"
    except Exception:
        balance_status = "error"

    # Active strategies count (filtered by account)
    strat_stmt = select(func.count(Strategy.id)).where(Strategy.status == "running")
    if filter_account_id:
        strat_stmt = strat_stmt.where(Strategy.account_id == filter_account_id)
    result = await db.execute(strat_stmt)
    active_strategies = result.scalar() or 0

    # Open positions & unrealized PnL from EXCHANGE
    open_positions = 0
    unrealized_pnl = 0.0
    unrealized_pnl_long = 0.0
    unrealized_pnl_short = 0.0
    total_notional = 0.0
    exchange_positions = []
    if binance:
        try:
            positions = await asyncio.wait_for(binance.fetch_positions(), timeout=8.0)
            for p in positions:
                contracts = float(p.get("contracts", 0) or 0)
                if contracts > 0:
                    open_positions += 1
                    entry_price = float(p.get("entryPrice", 0) or 0)
                    mark_price = float(p.get("markPrice", 0) or 0)
                    side = (p.get("side") or "").lower()
                    symbol = (p.get("symbol") or "").replace("/", "").replace(":USDT", "")
                    upnl = float(p.get("unrealizedPnl", 0) or 0)
                    unrealized_pnl += upnl
                    if side == "short":
                        unrealized_pnl_short += upnl
                    else:
                        unrealized_pnl_long += upnl
                    pnl_pct = 0.0
                    if entry_price > 0:
                        if side == "short":
                            pnl_pct = (entry_price - mark_price) / entry_price * 100
                        else:
                            pnl_pct = (mark_price - entry_price) / entry_price * 100
                    notional = float(p.get("notional", 0) or 0)
                    if abs(notional) < 1e-12 and contracts > 0 and mark_price > 0:
                        cs = float(p.get("contractSize", 1) or 1)
                        notional = abs(contracts * mark_price * cs)
                    total_notional += notional
                    exchange_positions.append({
                        "symbol": symbol,
                        "side": side,
                        "usdt": round(notional, 2),
                        "contracts": contracts,
                        "entry_price": round(entry_price, 4),
                        "mark_price": round(mark_price, 4),
                        "unrealized_pnl": round(float(p.get("unrealizedPnl", 0) or 0), 2),
                        "pnl_pct": round(pnl_pct, 2),
                    })
        except Exception as e:
            logging.error("Position fetch error for dashboard: %s", e)

    # Leverage = total position notional / wallet balance
    if total_balance > 0 and total_notional > 0:
        leverage_multiplier = round(total_notional / total_balance, 2)

    # Daily trades and PnL (today 00:00 Beijing, filtered by account)
    today_start = now_beijing().replace(hour=0, minute=0, second=0, microsecond=0)
    trade_stmt = select(Trade).where(Trade.exit_time >= today_start)
    if filter_account_id:
        trade_stmt = trade_stmt.where(Trade.account_id == filter_account_id)
    result = await db.execute(trade_stmt)
    daily_trades = result.scalars().all()
    daily_trade_count = len(daily_trades)
    daily_pnl = sum(t.realized_pnl for t in daily_trades)
    daily_pnl_long = sum(t.realized_pnl for t in daily_trades if t.side == "long")
    daily_pnl_short = sum(t.realized_pnl for t in daily_trades if t.side == "short")

    # Win rate (today)
    winning = sum(1 for t in daily_trades if t.realized_pnl > 0)
    win_rate = (winning / daily_trade_count * 100) if daily_trade_count > 0 else 0

    # All-time stats from trades (same account filter as daily)
    agg_stmt = select(
        func.count(Trade.id),
        func.coalesce(func.sum(Trade.realized_pnl), 0.0),
        func.coalesce(
            func.sum(case((Trade.realized_pnl > 0, 1), else_=0)),
            0,
        ),
    )
    if filter_account_id:
        agg_stmt = agg_stmt.where(Trade.account_id == filter_account_id)
    agg_row = (await db.execute(agg_stmt)).one()
    total_trades_n = int(agg_row[0] or 0)
    total_realized = float(agg_row[1] or 0)
    total_wins_n = int(agg_row[2] or 0)
    total_win_rate = (total_wins_n / total_trades_n * 100) if total_trades_n > 0 else 0.0

    legs_stmt = select(
        func.coalesce(
            func.sum(case((Trade.side == "long", Trade.realized_pnl), else_=0.0)),
            0.0,
        ),
        func.coalesce(
            func.sum(case((Trade.side == "short", Trade.realized_pnl), else_=0.0)),
            0.0,
        ),
    )
    if filter_account_id:
        legs_stmt = legs_stmt.where(Trade.account_id == filter_account_id)
    legs_row = (await db.execute(legs_stmt)).one()
    total_pnl_long_v = float(legs_row[0] or 0)
    total_pnl_short_v = float(legs_row[1] or 0)

    # Daily PnL %
    daily_pnl_pct = round(daily_pnl / total_balance * 100, 2) if total_balance > 0 else 0.0

    # Master switch
    result = await db.execute(
        select(BotConfig).where(BotConfig.key == "master_switch")
    )
    master_config = result.scalar()
    master_switch = master_config.value == "true" if master_config else False

    return DashboardSnapshot(
        total_balance=round(total_balance, 2),
        available_balance=round(available_balance, 2),
        unrealized_pnl=round(unrealized_pnl, 2),
        unrealized_pnl_long=round(unrealized_pnl_long, 2),
        unrealized_pnl_short=round(unrealized_pnl_short, 2),
        daily_pnl=round(daily_pnl, 2),
        daily_pnl_long=round(daily_pnl_long, 2),
        daily_pnl_short=round(daily_pnl_short, 2),
        daily_pnl_pct=daily_pnl_pct,
        active_strategies=active_strategies,
        open_positions=open_positions,
        daily_trades=daily_trade_count,
        win_rate_pct=round(win_rate, 2),
        total_realized_pnl=round(total_realized, 2),
        total_trades=total_trades_n,
        total_win_rate_pct=round(total_win_rate, 2),
        total_pnl_long=round(total_pnl_long_v, 2),
        total_pnl_short=round(total_pnl_short_v, 2),
        leverage_multiplier=leverage_multiplier,
        master_switch=master_switch,
        account_name=account_name,
        balance_status=balance_status,
        exchange_positions=exchange_positions,
    )

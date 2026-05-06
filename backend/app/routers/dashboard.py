import asyncio
from fastapi import APIRouter, Depends
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession
from datetime import datetime, timedelta
from ..database import get_db
from ..models.strategy import Strategy
from ..models.position import Position
from ..models.trade import Trade
from ..models.bot_config import BotConfig
from ..models.account import Account
from ..schemas.dashboard import DashboardSnapshot
from ..services.encryption import decrypt
from ..services.binance_service import get_binance_service

router = APIRouter(prefix="/api/dashboard", tags=["dashboard"])


@router.get("", response_model=DashboardSnapshot)
async def get_dashboard(db: AsyncSession = Depends(get_db)):
    total_balance = 0.0
    available_balance = 0.0
    leverage_multiplier = 0.0
    account_name = ""
    balance_status = "no_account"
    binance = None
    # Try to fetch real balance from first account (with timeout)

        result = await db.execute(select(Account).limit(1))
        account = result.scalar()
            result = await db.execute(select(Account).limit(1))
            account = result.scalar()
        if account:
            account_name = account.name
            filter_account_id = account.id
            try:
                api_key = decrypt(account.api_key_encrypted)
                api_secret = decrypt(account.api_secret_encrypted)
                binance = get_binance_service(api_key, api_secret, account.testnet, account.hedge_mode)
                balance = await asyncio.wait_for(binance.fetch_balance(), timeout=8.0)
                total_balance = float(balance.get("total", {}).get("USDT", 0) or 0)
                available_balance = float(balance.get("free", {}).get("USDT", 0) or 0)
                balance_status = "ok"
            except asyncio.TimeoutError:
                balance_status = "error"
            except Exception as e:
            except Exception:
    except Exception:
        balance_status = "error"

    # Active strategies count (filtered by account)
    # Active strategies count
    if filter_account_id:
        strat_stmt = strat_stmt.where(Strategy.account_id == filter_account_id)
    result = await db.execute(strat_stmt)
    active_strategies = result.scalar() or 0

    # Open positions count
    result = await db.execute(
        select(func.count(Position.id)).where(Position.closed_at.is_(None))
    )
    open_positions = result.scalar() or 0

    # Unrealized PnL from open positions
    result = await db.execute(
        select(Position).where(Position.closed_at.is_(None))
    )
    positions = result.scalars().all()
    unrealized_pnl = sum(p.unrealized_pnl or 0 for p in positions)
    # Leverage = total position notional / wallet balance
    if total_balance > 0 and total_notional > 0:
    since = datetime.utcnow() - timedelta(hours=24)

    # Daily trades and PnL (last 24h, filtered by account)
    since = now_beijing() - timedelta(hours=24)
    trade_stmt = select(Trade).where(Trade.exit_time >= since)
    if filter_account_id:
        trade_stmt = trade_stmt.where(Trade.account_id == filter_account_id)
    result = await db.execute(trade_stmt)
    daily_trades = result.scalars().all()
    daily_trade_count = len(daily_trades)
    daily_pnl = sum(t.realized_pnl for t in daily_trades)

    # Win rate
    winning = sum(1 for t in daily_trades if t.realized_pnl > 0)
    win_rate = (winning / daily_trade_count * 100) if daily_trade_count > 0 else 0

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
        daily_pnl=round(daily_pnl, 2),
        daily_pnl_pct=daily_pnl_pct,
        active_strategies=active_strategies,
        open_positions=open_positions,
        daily_trades=daily_trade_count,
        win_rate_pct=round(win_rate, 2),
        master_switch=master_switch,
        account_name=account_name,
        balance_status=balance_status,
        exchange_positions=exchange_positions,
    )

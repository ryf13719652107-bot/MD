"""每小时写入账户 total USDT 快照（与仪表盘余额口径一致）。"""
import asyncio
import logging
from sqlalchemy import select

from ..database import async_session
from ..config import now_beijing
from ..models.account import Account
from ..models.equity_curve import AccountBalanceSnapshot
from ..services.encryption import decrypt
from ..services.binance_service import get_binance_service

logger = logging.getLogger(__name__)


async def run_hourly_equity_snapshots() -> None:
    async with async_session() as session:
        accounts = (await session.execute(select(Account).order_by(Account.id))).scalars().all()

    hour_floor = now_beijing().replace(minute=0, second=0, microsecond=0)

    for account in accounts:
        try:
            api_key = decrypt(account.api_key_encrypted)
            api_secret = decrypt(account.api_secret_encrypted)
            binance = await get_binance_service(api_key, api_secret, account.testnet, account.hedge_mode)
            balance = await asyncio.wait_for(binance.fetch_balance(), timeout=15.0)
            total = float(balance.get("total", {}).get("USDT", 0) or 0)
        except Exception as e:
            logger.warning("equity snapshot skip account %s (%s): %s", account.id, account.name, e)
            continue

        async with async_session() as session:
            stmt = select(AccountBalanceSnapshot).where(
                AccountBalanceSnapshot.account_id == account.id,
                AccountBalanceSnapshot.snapshot_at == hour_floor,
            )
            row = (await session.execute(stmt)).scalar_one_or_none()
            if row:
                row.total_usdt = total
            else:
                session.add(
                    AccountBalanceSnapshot(
                        account_id=account.id,
                        snapshot_at=hour_floor,
                        total_usdt=total,
                    )
                )
            await session.commit()

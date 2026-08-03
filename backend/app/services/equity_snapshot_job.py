"""每小时写入账户「保证金余额」快照；币安账户额外同步合约划转流水。"""
import asyncio
import logging
from sqlalchemy import select

from ..database import async_session
from ..config import now_beijing
from ..models.account import Account
from ..models.equity_curve import AccountBalanceSnapshot
from ..services.exchange_factory import (
    account_exchange_id,
    extract_margin_balance,
    get_exchange_for_account,
)
from ..services.equity_cashflow import sync_account_cashflows_for_hour

logger = logging.getLogger(__name__)


async def run_hourly_equity_snapshots() -> None:
    async with async_session() as session:
        accounts = (await session.execute(select(Account).order_by(Account.id))).scalars().all()

    now = now_beijing()
    # 快照时间用实际采样时刻（到秒），勿用整点标签：余额是「现在」的，
    # 若标成 hour_floor，本小时充值会晚一拍才进入校正，隐式基准被抬高后收益率变负。
    snap_at = now.replace(microsecond=0)
    hour_floor = now.replace(minute=0, second=0, microsecond=0)

    for account in accounts:
        try:
            client = await get_exchange_for_account(account)
            balance = await asyncio.wait_for(client.fetch_balance(), timeout=15.0)
            # 币安 = totalMarginBalance（App 保证金余额），非钱包余额
            total = extract_margin_balance(client, balance)
            if total <= 0:
                # fallback ccxt total.USDT
                total = float(balance.get("total", {}).get("USDT", 0) or 0)
        except Exception as e:
            logger.warning("equity snapshot skip account %s (%s): %s", account.id, account.name, e)
            continue

        async with async_session() as session:
            stmt = select(AccountBalanceSnapshot).where(
                AccountBalanceSnapshot.account_id == account.id,
                AccountBalanceSnapshot.snapshot_at == snap_at,
            )
            row = (await session.execute(stmt)).scalar_one_or_none()
            if row:
                row.total_usdt = total
            else:
                session.add(
                    AccountBalanceSnapshot(
                        account_id=account.id,
                        snapshot_at=snap_at,
                        total_usdt=total,
                    )
                )

            acc = (
                await session.execute(select(Account).where(Account.id == account.id))
            ).scalar_one_or_none()
            # 充提/划转现金流：仅币安首期支持
            if acc is not None and account_exchange_id(acc) == "binance":
                try:
                    from ..services.binance_service import get_binance_service
                    from ..services.encryption import decrypt

                    api_key = decrypt(acc.api_key_encrypted)
                    api_secret = decrypt(acc.api_secret_encrypted)
                    binance = await get_binance_service(
                        api_key, api_secret, acc.testnet, acc.hedge_mode
                    )
                    n = await asyncio.wait_for(
                        sync_account_cashflows_for_hour(
                            session, acc, binance, hour_floor, as_of=snap_at
                        ),
                        timeout=30.0,
                    )
                    if n:
                        logger.info(
                            "equity cashflow synced account %s as_of=%s: +%s rows",
                            account.id,
                            snap_at.strftime("%Y-%m-%d %H:%M:%S"),
                            n,
                        )
                except Exception as e:
                    logger.warning(
                        "equity cashflow sync skip account %s (%s): %s",
                        account.id,
                        account.name,
                        e,
                    )

            await session.commit()

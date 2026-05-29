import logging
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from ..database import get_db
from ..models.account import Account
from ..schemas.account import AccountCreate, AccountResponse
from ..services.encryption import encrypt, decrypt, mask_key

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/accounts", tags=["accounts"])


@router.post("", response_model=AccountResponse)
async def create_account(data: AccountCreate, db: AsyncSession = Depends(get_db)):
    try:
        encrypted_key = encrypt(data.api_key)
        encrypted_secret = encrypt(data.api_secret)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"加密失败: {str(e)}")

    account = Account(
        name=data.name,
        api_key_encrypted=encrypted_key,
        api_secret_encrypted=encrypted_secret,
        testnet=data.testnet,
        hedge_mode=data.hedge_mode,
    )
    db.add(account)
    await db.commit()
    await db.refresh(account)

    return AccountResponse(
        id=account.id,
        name=account.name,
        masked_key=mask_key(data.api_key),
        testnet=account.testnet,
        hedge_mode=account.hedge_mode,
        created_at=account.created_at,
        updated_at=account.updated_at,
    )


@router.get("", response_model=list[AccountResponse])
async def list_accounts(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Account))
    accounts = result.scalars().all()
    resp = []
    for a in accounts:
        try:
            key = decrypt(a.api_key_encrypted)
            mk = mask_key(key)
        except Exception:
            mk = "****"
        resp.append(
            AccountResponse(
                id=a.id,
                name=a.name,
                masked_key=mk,
                testnet=a.testnet,
                hedge_mode=a.hedge_mode,
                created_at=a.created_at,
                updated_at=a.updated_at,
            )
        )
    return resp


@router.delete("/{account_id}", status_code=204)
async def delete_account(account_id: int, db: AsyncSession = Depends(get_db)):
    account = await db.get(Account, account_id)
    if not account:
        raise HTTPException(status_code=404, detail="账户不存在")

    from ..models.strategy import Strategy
    from ..services.scheduler import strategy_scheduler
    from ..services.log_service import strategy_log_service
    from ..services.encryption import decrypt

    result = await db.execute(select(Strategy).where(Strategy.account_id == account_id))
    strategies = result.scalars().all()

    # 1. Stop all scheduler jobs for running strategies
    for s in strategies:
        if s.status == "running":
            await strategy_scheduler.remove_strategy(s.id)
        strategy_log_service.clear(s.id)

    # 2. Close all exchange positions for this account before wiping DB
    exchange_close_errors: list[str] = []
    try:
        from ..services.binance_service import get_binance_service

        api_key = decrypt(account.api_key_encrypted)
        api_secret = decrypt(account.api_secret_encrypted)
        binance = await get_binance_service(api_key, api_secret, account.testnet, account.hedge_mode)

        eps = await binance.fetch_positions()
        legs: dict[tuple[str, str], float] = {}
        for ep in eps:
            contracts = float(ep.get("contracts", 0) or 0)
            if contracts <= 0:
                continue
            sym = (ep.get("symbol") or "").replace("/", "").replace(":USDT", "")
            side = (ep.get("side") or "").lower()
            legs[(sym, side)] = legs.get((sym, side), 0) + contracts

        for (sym, side) in legs:
            try:
                await binance.close_position(sym, side)
            except Exception as e:
                exchange_close_errors.append(f"{sym} {side}: {e}")
    except Exception as e:
        exchange_close_errors.append(f"API连接失败: {e}")

    if exchange_close_errors:
        logger.warning(
            "账户 %d 删除时部分仓位未能平仓: %s", account_id, "; ".join(exchange_close_errors)
        )

    # 3. Explicit cleanup — 逐表 DELETE，防御层；CASCADE 兜底
    from ..models.position import Position
    from ..models.trade import Trade
    from ..models.equity_curve import AccountBalanceSnapshot, AccountEquityBaseline
    from sqlalchemy import delete as sqla_delete

    await db.execute(sqla_delete(AccountBalanceSnapshot).where(AccountBalanceSnapshot.account_id == account_id))
    await db.execute(sqla_delete(AccountEquityBaseline).where(AccountEquityBaseline.account_id == account_id))
    await db.execute(sqla_delete(Position).where(Position.account_id == account_id))
    await db.execute(sqla_delete(Trade).where(Trade.account_id == account_id))
    await db.execute(sqla_delete(Strategy).where(Strategy.account_id == account_id))
    await db.execute(sqla_delete(Account).where(Account.id == account_id))
    await db.commit()

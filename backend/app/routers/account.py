import logging
import re
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select, delete as sqla_delete
from sqlalchemy.ext.asyncio import AsyncSession
from ..database import get_db
from ..models.account import Account
from ..schemas.account import AccountCreate, AccountResponse
from ..services.encryption import encrypt, decrypt, mask_key

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/accounts", tags=["accounts"])

_SPAM_NAME = re.compile(
    r"^(race_|proto_test$|test$|.*\.php$)",
    re.IGNORECASE,
)


def is_spam_account_name(name: str) -> bool:
    return bool(_SPAM_NAME.search((name or "").strip()))


def _client_ip(request: Request) -> str:
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()
    if request.client:
        return request.client.host or "?"
    return "?"


@router.post("", response_model=AccountResponse)
async def create_account(data: AccountCreate, request: Request, db: AsyncSession = Depends(get_db)):
    logger.info(
        "创建账户 name=%s testnet=%s ip=%s",
        data.name,
        data.testnet,
        _client_ip(request),
    )
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


async def _delete_account_record(account_id: int, db: AsyncSession) -> None:
    from ..models.strategy import Strategy
    from ..services.scheduler import strategy_scheduler
    from ..services.log_service import strategy_log_service

    account = await db.get(Account, account_id)
    if not account:
        return

    result = await db.execute(select(Strategy).where(Strategy.account_id == account_id))
    strategies = result.scalars().all()

    for s in strategies:
        if s.status == "running":
            await strategy_scheduler.remove_strategy(s.id)
        strategy_log_service.clear(s.id)

    exchange_close_errors: list[str] = []
    api_key: str | None = None
    api_secret: str | None = None
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

    from ..models.position import Position
    from ..models.trade import Trade
    from ..models.equity_curve import AccountBalanceSnapshot, AccountEquityBaseline
    from ..models.strategy_blacklist import StrategySymbolBlacklist

    strategy_ids = [s.id for s in strategies]
    if strategy_ids:
        await db.execute(
            sqla_delete(StrategySymbolBlacklist).where(
                StrategySymbolBlacklist.strategy_id.in_(strategy_ids)
            )
        )

    await db.execute(sqla_delete(AccountBalanceSnapshot).where(AccountBalanceSnapshot.account_id == account_id))
    await db.execute(sqla_delete(AccountEquityBaseline).where(AccountEquityBaseline.account_id == account_id))
    await db.execute(sqla_delete(Position).where(Position.account_id == account_id))
    await db.execute(sqla_delete(Trade).where(Trade.account_id == account_id))
    await db.execute(sqla_delete(Strategy).where(Strategy.account_id == account_id))
    await db.execute(sqla_delete(Account).where(Account.id == account_id))

    if api_key and api_secret:
        try:
            from ..services.binance_service import clear_private_binance_service

            await clear_private_binance_service(
                api_key, api_secret, account.testnet, account.hedge_mode
            )
        except Exception as e:
            logger.warning("账户 %d 删除后清理 Binance 缓存失败: %s", account_id, e)


@router.post("/purge-spam")
async def purge_spam_accounts(request: Request, db: AsyncSession = Depends(get_db)):
    """批量删除名称匹配垃圾模式的账户（race_* / proto_test / test / *.php）。"""
    result = await db.execute(select(Account))
    accounts = list(result.scalars().all())
    targets = [a for a in accounts if is_spam_account_name(a.name)]
    deleted: list[dict] = []
    for a in targets:
        aid = a.id
        name = a.name
        await _delete_account_record(aid, db)
        deleted.append({"id": aid, "name": name})
    await db.commit()
    logger.info(
        "purge-spam deleted %d accounts ip=%s names=%s",
        len(deleted),
        _client_ip(request),
        [d["name"] for d in deleted[:20]],
    )
    return {"deleted_count": len(deleted), "deleted": deleted}


@router.delete("/{account_id}", status_code=204)
async def delete_account(account_id: int, db: AsyncSession = Depends(get_db)):
    account = await db.get(Account, account_id)
    if not account:
        raise HTTPException(status_code=404, detail="账户不存在")

    await _delete_account_record(account_id, db)
    await db.commit()

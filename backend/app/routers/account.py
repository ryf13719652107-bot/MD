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
    exchange = data.exchange
    testnet = data.testnet
    hedge_mode = data.hedge_mode
    if exchange == "gate":
        testnet = False
        hedge_mode = True
    logger.info(
        "创建账户 name=%s exchange=%s testnet=%s ip=%s",
        data.name,
        exchange,
        testnet,
        _client_ip(request),
    )
    try:
        encrypted_key = encrypt(data.api_key)
        encrypted_secret = encrypt(data.api_secret)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"加密失败: {str(e)}")

    account = Account(
        name=data.name,
        exchange=exchange,
        api_key_encrypted=encrypted_key,
        api_secret_encrypted=encrypted_secret,
        testnet=testnet,
        hedge_mode=hedge_mode,
    )
    db.add(account)
    await db.commit()
    await db.refresh(account)

    # 立刻打快照+基准=当前余额；不拉充提（建账前资金算本金）。游标拨到下一整点，从下小时起再同步划转。
    try:
        await _seed_account_equity_snapshot(db, account)
        await db.commit()
    except Exception as e:
        logger.warning("seed equity after create account %s failed: %s", account.id, e)
        await db.rollback()

    return AccountResponse(
        id=account.id,
        name=account.name,
        exchange=account.exchange or "binance",
        masked_key=mask_key(data.api_key),
        testnet=account.testnet,
        hedge_mode=account.hedge_mode,
        created_at=account.created_at,
        updated_at=account.updated_at,
    )


async def _seed_account_equity_snapshot(db: AsyncSession, account: Account) -> None:
    """新建账户：只写当前余额快照与基准，不请求充提流水。"""
    from ..config import now_beijing
    from ..models.equity_curve import AccountBalanceSnapshot, AccountEquityBaseline
    from ..services.exchange_factory import extract_margin_balance, get_exchange_for_account
    from ..services.equity_cashflow import beijing_naive_to_ms

    now = now_beijing()
    snap_at = now.replace(microsecond=0)
    # 游标=建账时刻：此刻不拉流水；之后同步从该时刻起算。
    # 勿拨到下一整点，否则建账→下一整点之间的充提会漏同步，被当成交易盈利。
    account.cashflow_sync_cursor_ms = beijing_naive_to_ms(snap_at)

    client = await get_exchange_for_account(account)
    balance = await client.fetch_balance()
    total = extract_margin_balance(client, balance)
    if total <= 0:
        total = float(balance.get("total", {}).get("USDT", 0) or 0)

    existing = (
        await db.execute(
            select(AccountBalanceSnapshot).where(
                AccountBalanceSnapshot.account_id == account.id,
                AccountBalanceSnapshot.snapshot_at == snap_at,
            )
        )
    ).scalar_one_or_none()
    if existing:
        existing.total_usdt = total
    else:
        db.add(
            AccountBalanceSnapshot(
                account_id=account.id,
                snapshot_at=snap_at,
                total_usdt=total,
            )
        )

    bl = (
        await db.execute(
            select(AccountEquityBaseline).where(AccountEquityBaseline.account_id == account.id)
        )
    ).scalar_one_or_none()
    if bl is None:
        db.add(
            AccountEquityBaseline(
                account_id=account.id,
                baseline_total_usdt=float(total),
                set_at=now,
            )
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
                exchange=getattr(a, "exchange", None) or "binance",
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
        from ..services.exchange_factory import get_exchange_for_account

        api_key = decrypt(account.api_key_encrypted)
        api_secret = decrypt(account.api_secret_encrypted)
        client = await get_exchange_for_account(account)

        eps = await client.fetch_positions()
        legs: dict[tuple[str, str], float] = {}
        for ep in eps:
            contracts = float(ep.get("contracts", 0) or 0)
            if contracts <= 0:
                continue
            sym = (
                (ep.get("symbol") or "")
                .replace("/", "")
                .replace(":USDT", "")
                .replace("_", "")
                .upper()
            )
            side = (ep.get("side") or "").lower()
            legs[(sym, side)] = legs.get((sym, side), 0) + contracts

        for (sym, side) in legs:
            try:
                await client.close_position(sym, side)
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
    from ..models.equity_curve import AccountBalanceSnapshot, AccountEquityBaseline, AccountCashflow
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
    await db.execute(sqla_delete(AccountCashflow).where(AccountCashflow.account_id == account_id))
    await db.execute(sqla_delete(Position).where(Position.account_id == account_id))
    await db.execute(sqla_delete(Trade).where(Trade.account_id == account_id))
    await db.execute(sqla_delete(Strategy).where(Strategy.account_id == account_id))
    await db.execute(sqla_delete(Account).where(Account.id == account_id))

    if api_key and api_secret:
        try:
            from ..services.exchange_factory import clear_private_exchange_for_account

            await clear_private_exchange_for_account(account)
        except Exception as e:
            logger.warning("账户 %d 删除后清理交易所缓存失败: %s", account_id, e)


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

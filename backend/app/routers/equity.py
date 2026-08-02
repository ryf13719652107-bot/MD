import logging
from datetime import timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..database import get_db
from ..config import now_beijing
from ..models.account import Account
from ..models.equity_curve import AccountBalanceSnapshot, AccountEquityBaseline, AccountCashflow
from ..schemas.equity import EquityPointOut, EquitySeriesResponse, EquitySummaryOut
from ..services.encryption import decrypt
from ..services.exchange_factory import (
    account_exchange_id,
    extract_wallet_balance,
    get_exchange_for_account,
)
from ..services.binance_service import get_binance_service
from ..services.equity_cashflow import (
    build_adjusted_points,
    window_deposit_withdraw,
    sync_account_cashflows_for_hour,
    cum_net_external,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/equity", tags=["equity"])

BJ_OFFSET = timezone(timedelta(hours=8))


def _bj_naive_to_unix(dt) -> int:
    return int(dt.replace(tzinfo=BJ_OFFSET).timestamp())


def _max_drawdown_pct(balances: list[float]) -> float:
    if not balances:
        return 0.0
    peak = balances[0]
    max_dd = 0.0
    for x in balances:
        if x > peak:
            peak = x
        if peak <= 0:
            continue
        dd = (peak - x) / peak * 100.0
        if dd > max_dd:
            max_dd = dd
    return round(max_dd, 2)


def _fmt_ts(dt) -> str:
    if dt is None:
        return ""
    return dt.strftime("%Y-%m-%d %H:%M:%S")


@router.get("/series", response_model=EquitySeriesResponse)
async def get_equity_series(
    account_id: int = Query(...),
    days: int = Query(30, ge=1, le=366),
    db: AsyncSession = Depends(get_db),
):
    """收益序列：余额快照 − 已落库划转后计算回报/回撤；划转仅由整点任务按「前一小时」写入。"""
    result = await db.execute(select(Account).where(Account.id == account_id))
    account = result.scalar()
    if not account:
        raise HTTPException(status_code=404, detail="账户不存在")

    start = now_beijing() - timedelta(days=days)
    snaps = (
        (
            await db.execute(
                select(AccountBalanceSnapshot)
                .where(
                    AccountBalanceSnapshot.account_id == account_id,
                    AccountBalanceSnapshot.snapshot_at >= start,
                )
                .order_by(AccountBalanceSnapshot.snapshot_at.asc())
            )
        )
        .scalars()
        .all()
    )

    all_cfs = (
        (
            await db.execute(
                select(AccountCashflow)
                .where(AccountCashflow.account_id == account_id)
                .order_by(AccountCashflow.occurred_at.asc())
            )
        )
        .scalars()
        .all()
    )
    cf_pairs = [(c.occurred_at, float(c.amount)) for c in all_cfs]

    baseline_row = (
        await db.execute(select(AccountEquityBaseline).where(AccountEquityBaseline.account_id == account_id))
    ).scalar_one_or_none()

    # 充提展示：有显式基准时从重置时刻起算，避免重置后仍显示旧划转
    dep_start = start
    if baseline_row and baseline_row.set_at is not None:
        dep_start = max(start, baseline_row.set_at) if start else baseline_row.set_at
    deposit_usdt, withdraw_usdt = window_deposit_withdraw(cf_pairs, dep_start)

    snaps_raw = [(s.snapshot_at, float(s.total_usdt)) for s in snaps]
    adjusted = build_adjusted_points(snaps_raw, cf_pairs)

    if baseline_row:
        baseline = float(baseline_row.baseline_total_usdt)
        implicit = False
        baseline_set_at = _fmt_ts(baseline_row.set_at)
    elif adjusted:
        baseline = float(adjusted[0][2])
        implicit = True
        baseline_set_at = None
    else:
        baseline = 0.0
        implicit = True
        baseline_set_at = None

    points_out: list[EquityPointOut] = []
    balances_for_dd: list[float] = []
    for t, tot, adj in adjusted:
        pnl = adj - baseline
        ret = (pnl / baseline * 100.0) if baseline > 1e-12 else 0.0
        points_out.append(
            EquityPointOut(
                t_unix=_bj_naive_to_unix(t),
                total_usdt=round(tot, 2),
                return_pct=round(ret, 4),
                pnl_usdt=round(pnl, 2),
            )
        )
        balances_for_dd.append(adj)

    max_dd = _max_drawdown_pct(balances_for_dd)
    cur_bal = float(adjusted[-1][1]) if adjusted else 0.0
    cur_adj = float(adjusted[-1][2]) if adjusted else 0.0
    pnl = cur_adj - baseline
    ret_pct = round((pnl / baseline * 100.0) if baseline > 1e-12 else 0.0, 2)
    ratio = round(ret_pct / max_dd, 2) if max_dd > 1e-6 else None

    summary = EquitySummaryOut(
        total_balance=round(cur_bal, 2),
        pnl_usdt=round(pnl, 2),
        return_pct=ret_pct,
        max_drawdown_pct=max_dd,
        return_drawdown_ratio=ratio,
        baseline_total_usdt=round(baseline, 2),
        baseline_set_at=baseline_set_at or None,
        implicit_baseline=implicit,
        deposit_usdt=round(deposit_usdt, 2),
        withdraw_usdt=round(withdraw_usdt, 2),
    )

    return EquitySeriesResponse(points=points_out, summary=summary)


@router.post("/baseline-reset")
async def reset_equity_baseline(
    account_id: int = Query(...),
    db: AsyncSession = Depends(get_db),
):
    """清空快照并以当前「校正权益」写入基准；划转流水保留，整点继续按前一小时同步。"""
    result = await db.execute(select(Account).where(Account.id == account_id))
    account = result.scalar()
    if not account:
        raise HTTPException(status_code=404, detail="账户不存在")

    hour_floor = now_beijing().replace(minute=0, second=0, microsecond=0)
    try:
        client = await get_exchange_for_account(account)
        # 仅币安同步划转现金流
        if account_exchange_id(account) == "binance":
            api_key = decrypt(account.api_key_encrypted)
            api_secret = decrypt(account.api_secret_encrypted)
            binance = await get_binance_service(
                api_key, api_secret, account.testnet, account.hedge_mode
            )
            await sync_account_cashflows_for_hour(db, account, binance, hour_floor)
        balance = await client.fetch_balance()
        cur_total = extract_wallet_balance(client, balance)
        if cur_total <= 0:
            cur_total = float(balance.get("total", {}).get("USDT", 0) or 0)
    except Exception as e:
        logger.warning("baseline-reset live balance/cashflow failed account %s: %s", account_id, e)
        last = (
            await db.execute(
                select(AccountBalanceSnapshot)
                .where(AccountBalanceSnapshot.account_id == account_id)
                .order_by(AccountBalanceSnapshot.snapshot_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        cur_total = float(last.total_usdt) if last else 0.0

    all_cfs = (
        (
            await db.execute(
                select(AccountCashflow)
                .where(AccountCashflow.account_id == account_id)
                .order_by(AccountCashflow.occurred_at.asc())
            )
        )
        .scalars()
        .all()
    )
    cum = cum_net_external((c.occurred_at, float(c.amount)) for c in all_cfs)
    adjusted_baseline = cur_total - cum
    now = now_beijing()

    r_snaps = await db.execute(
        delete(AccountBalanceSnapshot).where(AccountBalanceSnapshot.account_id == account_id)
    )
    deleted_snaps = int(r_snaps.rowcount or 0)

    existing_bl = (
        await db.execute(select(AccountEquityBaseline).where(AccountEquityBaseline.account_id == account_id))
    ).scalar_one_or_none()
    if existing_bl:
        existing_bl.baseline_total_usdt = adjusted_baseline
        existing_bl.set_at = now
    else:
        db.add(
            AccountEquityBaseline(
                account_id=account_id,
                baseline_total_usdt=adjusted_baseline,
                set_at=now,
            )
        )

    await db.commit()

    return {
        "ok": True,
        "deleted_snapshots": deleted_snaps,
        "baseline_total_usdt": round(adjusted_baseline, 2),
        "message": "已清空历史快照，并以当前校正权益设为新基准；划转仅由整点任务按前一小时继续记录。",
    }

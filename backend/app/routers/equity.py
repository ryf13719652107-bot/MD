import asyncio
import logging
from datetime import timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..database import get_db
from ..config import now_beijing
from ..models.account import Account
from ..models.equity_curve import AccountBalanceSnapshot, AccountEquityBaseline
from ..schemas.equity import EquityPointOut, EquitySeriesResponse, EquitySummaryOut
from ..services.encryption import decrypt
from ..services.binance_service import get_binance_service

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

    baseline_row = (
        await db.execute(select(AccountEquityBaseline).where(AccountEquityBaseline.account_id == account_id))
    ).scalar_one_or_none()

    live_total = 0.0
    balance_ok = False
    try:
        api_key = decrypt(account.api_key_encrypted)
        api_secret = decrypt(account.api_secret_encrypted)
        binance = await get_binance_service(api_key, api_secret, account.testnet, account.hedge_mode)
        balance = await asyncio.wait_for(binance.fetch_balance(), timeout=8.0)
        live_total = float(balance.get("total", {}).get("USDT", 0) or 0)
        balance_ok = True
    except Exception as e:
        logger.warning("equity series live balance failed account %s: %s", account_id, e)

    if baseline_row:
        baseline = float(baseline_row.baseline_total_usdt)
        implicit = False
        baseline_set_at = _fmt_ts(baseline_row.set_at)
    elif snaps:
        baseline = float(snaps[0].total_usdt)
        implicit = True
        baseline_set_at = None
    else:
        baseline = live_total if balance_ok else 0.0
        implicit = True
        baseline_set_at = None

    points_raw: list[tuple] = [(s.snapshot_at, float(s.total_usdt)) for s in snaps]
    hour_floor = now_beijing().replace(minute=0, second=0, microsecond=0)

    if balance_ok:
        if points_raw and points_raw[-1][0] == hour_floor:
            points_raw[-1] = (hour_floor, live_total)
        else:
            points_raw.append((hour_floor, live_total))

    points_out: list[EquityPointOut] = []
    balances_for_dd: list[float] = []
    for t, tot in points_raw:
        pnl = tot - baseline
        ret = (pnl / baseline * 100.0) if baseline > 1e-12 else 0.0
        points_out.append(
            EquityPointOut(
                t_unix=_bj_naive_to_unix(t),
                total_usdt=round(tot, 2),
                return_pct=round(ret, 4),
                pnl_usdt=round(pnl, 2),
            )
        )
        balances_for_dd.append(tot)

    max_dd = _max_drawdown_pct(balances_for_dd)
    cur_bal = live_total if balance_ok else (points_raw[-1][1] if points_raw else 0.0)
    pnl = cur_bal - baseline
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
    )

    return EquitySeriesResponse(points=points_out, summary=summary)


@router.post("/baseline-reset")
async def reset_equity_baseline(
    account_id: int = Query(...),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(Account).where(Account.id == account_id))
    account = result.scalar()
    if not account:
        raise HTTPException(status_code=404, detail="账户不存在")

    try:
        api_key = decrypt(account.api_key_encrypted)
        api_secret = decrypt(account.api_secret_encrypted)
        binance = await get_binance_service(api_key, api_secret, account.testnet, account.hedge_mode)
        balance = await asyncio.wait_for(binance.fetch_balance(), timeout=8.0)
        live_total = float(balance.get("total", {}).get("USDT", 0) or 0)
    except Exception as e:
        logger.error("baseline-reset balance error account %s: %s", account_id, e)
        raise HTTPException(status_code=502, detail="无法从交易所读取余额")

    now = now_beijing()
    row = (
        await db.execute(select(AccountEquityBaseline).where(AccountEquityBaseline.account_id == account_id))
    ).scalar_one_or_none()
    if row:
        row.baseline_total_usdt = live_total
        row.set_at = now
    else:
        db.add(
            AccountEquityBaseline(
                account_id=account_id,
                baseline_total_usdt=live_total,
                set_at=now,
            )
        )
    await db.commit()

    return {
        "ok": True,
        "baseline_total_usdt": round(live_total, 2),
        "set_at": _fmt_ts(now),
    }

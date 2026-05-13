import logging
from datetime import timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..database import get_db
from ..config import now_beijing
from ..models.account import Account
from ..models.equity_curve import AccountBalanceSnapshot, AccountEquityBaseline
from ..schemas.equity import EquityPointOut, EquitySeriesResponse, EquitySummaryOut

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
    """收益序列与汇总仅使用库内按整点写入的小时快照；刷新页面不会拉交易所实时余额。"""
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

    if baseline_row:
        baseline = float(baseline_row.baseline_total_usdt)
        implicit = False
        baseline_set_at = _fmt_ts(baseline_row.set_at)
    elif snaps:
        baseline = float(snaps[0].total_usdt)
        implicit = True
        baseline_set_at = None
    else:
        baseline = 0.0
        implicit = True
        baseline_set_at = None

    points_raw: list[tuple] = [(s.snapshot_at, float(s.total_usdt)) for s in snaps]

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
    cur_bal = float(points_raw[-1][1]) if points_raw else 0.0
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
    """清空该账户收益曲线历史快照与手动基准；下个整点由定时任务重新写入第一条快照。"""
    result = await db.execute(select(Account).where(Account.id == account_id))
    account = result.scalar()
    if not account:
        raise HTTPException(status_code=404, detail="账户不存在")

    r_snaps = await db.execute(
        delete(AccountBalanceSnapshot).where(AccountBalanceSnapshot.account_id == account_id)
    )
    deleted_snaps = int(r_snaps.rowcount or 0)

    await db.execute(delete(AccountEquityBaseline).where(AccountEquityBaseline.account_id == account_id))
    await db.commit()

    return {
        "ok": True,
        "deleted_snapshots": deleted_snaps,
        "message": "已清空历史快照与收益基准，将在下一北京时间整点重新记录。",
    }

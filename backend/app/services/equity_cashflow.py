"""合约划转流水同步与收益曲线校正。

整点任务仍补齐「已完成小时」划转；快照写入时额外同步本小时已发生的划转，
且快照时间戳用实际采样时刻，避免「余额已含充值、校正却滞后」把收益率打成负数。
校正规则：充值、提现均从回报率/盈亏/回撤中剔除；余额曲线仍用真实钱包余额。
若中间整点任务漏跑，最多补齐最近 48 小时内未同步的小时窗，不回溯更早历史。
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Iterable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models.account import Account
from ..models.equity_curve import AccountCashflow

logger = logging.getLogger(__name__)

EXTERNAL_INCOME_TYPES: tuple[str, ...] = (
    "TRANSFER",
    "INTERNAL_TRANSFER",
    "CROSS_COLLATERAL_TRANSFER",
    "COIN_SWAP_DEPOSIT",
    "COIN_SWAP_WITHDRAW",
)

# 漏跑整点时最多补多少个小时窗（防止异常游标一次拉太久）
MAX_CATCHUP_HOURS = 48

BJ_OFFSET = timezone(timedelta(hours=8))


def ms_to_beijing_naive(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000.0, tz=BJ_OFFSET).replace(tzinfo=None)


def beijing_naive_to_ms(dt: datetime) -> int:
    return int(dt.replace(tzinfo=BJ_OFFSET).timestamp() * 1000)


def hour_cashflow_window(hour_floor: datetime) -> tuple[datetime, datetime]:
    """22:00 快照任务 → 半开区间 [21:00, 22:00)，避免整点边界双计。"""
    end = hour_floor.replace(minute=0, second=0, microsecond=0)
    start = end - timedelta(hours=1)
    return start, end


def cashflow_external_id(row: dict) -> str:
    """币安流水去重键：优先 tranId，否则 time+type+amount+asset。"""
    tran = row.get("tranId") or row.get("tranID") or row.get("id")
    if tran is not None and str(tran).strip() != "":
        return f"tran:{tran}"
    t = int(row.get("time") or 0)
    typ = str(row.get("incomeType") or "")
    income = str(row.get("income") or "")
    asset = str(row.get("asset") or "")
    return f"fb:{t}:{typ}:{income}:{asset}"


def build_adjusted_points(
    snaps: list[tuple[datetime, float]],
    cashflows: list[tuple[datetime, float]],
) -> list[tuple[datetime, float, float]]:
    """
    返回 (t, total_usdt, adjusted_usdt)。
    adjusted = total − 累计净划转(≤t)（充值+、提现− 都剔除）。
    余额视图用 total；回报率/盈亏/回撤用 adjusted。
    """
    cfs = sorted(cashflows, key=lambda x: x[0])
    out: list[tuple[datetime, float, float]] = []
    i = 0
    cum_external = 0.0
    for t, tot in snaps:
        while i < len(cfs) and cfs[i][0] <= t:
            cum_external += cfs[i][1]
            i += 1
        out.append((t, tot, tot - cum_external))
    return out


def cum_net_external(cashflows: Iterable[tuple[datetime, float] | float]) -> float:
    """累计净划转（充值正、提现负都计入）。"""
    total = 0.0
    for item in cashflows:
        amt = float(item[1]) if isinstance(item, tuple) else float(item)
        total += amt
    return total


def window_deposit_withdraw(
    cashflows: Iterable[tuple[datetime, float]],
    start: datetime | None,
    end: datetime | None = None,
) -> tuple[float, float]:
    """窗口内充值(=划入合计)、提现(=划出绝对值合计)。"""
    dep = 0.0
    wdr = 0.0
    for t, amt in cashflows:
        if start is not None and t < start:
            continue
        if end is not None and t > end:
            continue
        if amt > 0:
            dep += amt
        elif amt < 0:
            wdr += -amt
    return dep, wdr


def _hours_to_sync(hour_floor: datetime, cursor_ms: int | None) -> list[datetime]:
    """
    需要同步的快照整点列表（每个对应其前一小时窗 [H-1h, H)）。
    - 无游标：只同步当前 hour_floor（不回溯历史）
    - 有游标：从游标所在小时的下一窗补到 hour_floor（最多 MAX_CATCHUP_HOURS）
    - 游标在小时中段（建账 10:40→按 10:00）：当前整点不再重拉；下一整点会拉 [10:00,11:00)，
      其中建账前流水靠收益序列的 set_at 过滤，建账后流水会正确计入
    """
    end = hour_floor.replace(minute=0, second=0, microsecond=0)
    if cursor_ms is None:
        return [end]

    cursor_at = ms_to_beijing_naive(int(cursor_ms)).replace(microsecond=0)
    last_end = cursor_at.replace(minute=0, second=0, microsecond=0)
    if last_end >= end:
        return []

    nxt = last_end + timedelta(hours=1)
    hours: list[datetime] = []
    cur = nxt
    while cur <= end:
        hours.append(cur)
        cur += timedelta(hours=1)
        if len(hours) >= MAX_CATCHUP_HOURS:
            break
    return hours


async def _load_known_external_ids(session: AsyncSession, account_id: int) -> set[str]:
    existing = (
        await session.execute(
            select(AccountCashflow.external_id).where(AccountCashflow.account_id == account_id)
        )
    ).scalars().all()
    return set(existing)


async def _sync_cashflow_window(
    session: AsyncSession,
    account: Account,
    binance,
    window_start: datetime,
    window_end: datetime,
    known: set[str],
) -> tuple[int, bool]:
    """同步半开区间 [window_start, window_end) 的划转。返回 (新写入数, 是否至少一次 API 成功)。"""
    start_ms = beijing_naive_to_ms(window_start)
    # 半开 [start, end)：币安 endTime 含等号，故传 end_ms - 1
    end_ms_inclusive = beijing_naive_to_ms(window_end) - 1
    if end_ms_inclusive < start_ms:
        return 0, True

    inserted = 0
    any_ok = False

    for income_type in EXTERNAL_INCOME_TYPES:
        cursor = start_ms
        while cursor <= end_ms_inclusive:
            try:
                rows = await binance.fetch_income_history(
                    income_type=income_type,
                    start_time_ms=cursor,
                    end_time_ms=end_ms_inclusive,
                    limit=1000,
                )
                any_ok = True
            except Exception as e:
                logger.warning(
                    "cashflow sync account %s type=%s window=%s~%s failed: %s",
                    account.id,
                    income_type,
                    window_start,
                    window_end,
                    e,
                )
                break

            if not rows:
                break

            batch_max = cursor
            end_ms_exclusive = beijing_naive_to_ms(window_end)
            for row in rows:
                if not isinstance(row, dict):
                    continue
                asset = str(row.get("asset") or "").upper()
                if asset and asset != "USDT":
                    continue
                try:
                    amount = float(row.get("income") or 0)
                except (TypeError, ValueError):
                    continue
                if abs(amount) < 1e-12:
                    continue
                t_ms = int(row.get("time") or 0)
                if t_ms < start_ms or t_ms >= end_ms_exclusive:
                    continue
                ext_id = cashflow_external_id(row)
                if ext_id in known:
                    batch_max = max(batch_max, t_ms)
                    continue
                session.add(
                    AccountCashflow(
                        account_id=account.id,
                        amount=amount,
                        occurred_at=ms_to_beijing_naive(t_ms),
                        income_type=str(row.get("incomeType") or income_type),
                        asset="USDT",
                        external_id=ext_id,
                        source="binance_income",
                    )
                )
                known.add(ext_id)
                inserted += 1
                batch_max = max(batch_max, t_ms)

            if len(rows) < 1000:
                break
            next_cursor = batch_max + 1
            if next_cursor <= cursor:
                break
            cursor = next_cursor

    return inserted, any_ok


async def _sync_one_hour_window(
    session: AsyncSession,
    account: Account,
    binance,
    hour_floor: datetime,
    known: set[str],
) -> tuple[int, bool]:
    """同步单个已完成小时窗（hour_floor 对应前一小时）。"""
    window_start, window_end = hour_cashflow_window(hour_floor)
    return await _sync_cashflow_window(session, account, binance, window_start, window_end, known)


async def sync_open_hour_cashflows_until(
    session: AsyncSession,
    account: Account,
    binance,
    as_of: datetime,
    *,
    known: set[str] | None = None,
) -> int:
    """同步本小时已发生划转到 as_of（半开 [start, as_of)），不推进 cursor。

    start = max(hour_floor, cursor精确时刻)：新建账户游标=建账时刻，可跳过建账前流水，
    且不漏掉建账后到下一整点之间的充提。
    """
    as_of = as_of.replace(microsecond=0)
    hour_floor = as_of.replace(minute=0, second=0, microsecond=0)
    start = hour_floor
    if account.cashflow_sync_cursor_ms is not None:
        cursor_at = ms_to_beijing_naive(int(account.cashflow_sync_cursor_ms)).replace(
            microsecond=0
        )
        if cursor_at > start:
            start = cursor_at
    if as_of <= start:
        return 0
    if known is None:
        known = await _load_known_external_ids(session, account.id)
    n, _ok = await _sync_cashflow_window(session, account, binance, start, as_of, known)
    return n


def cashflows_after(
    cashflows: Iterable[tuple[datetime, float]],
    since: datetime | None,
) -> list[tuple[datetime, float]]:
    """只保留 since 之后的划转（since 为 None 则全部保留）。"""
    if since is None:
        return list(cashflows)
    return [(t, amt) for t, amt in cashflows if t > since]


async def sync_account_cashflows_for_hour(
    session: AsyncSession,
    account: Account,
    binance,
    hour_floor: datetime,
    *,
    as_of: datetime | None = None,
) -> int:
    """
    同步 hour_floor 对应前一小时划转；若有游标且中间漏跑，补齐中间小时窗（≤48h）。
    若传入 as_of（通常为快照实际时刻），再补齐本小时 [hour_floor, as_of) 已发生划转（不推进 cursor）。
    返回新写入条数。
    """
    hour_floor = hour_floor.replace(minute=0, second=0, microsecond=0)
    hours = _hours_to_sync(hour_floor, account.cashflow_sync_cursor_ms)

    known = await _load_known_external_ids(session, account.id)

    inserted_total = 0
    last_ok_end: datetime | None = None

    for hf in hours:
        n, ok = await _sync_one_hour_window(session, account, binance, hf, known)
        inserted_total += n
        if not ok:
            # 本小时 API 全失败：不推进游标越过它，避免永久漏窗
            break
        last_ok_end = hf

    if last_ok_end is not None:
        account.cashflow_sync_cursor_ms = beijing_naive_to_ms(last_ok_end)

    if as_of is not None:
        inserted_total += await sync_open_hour_cashflows_until(
            session, account, binance, as_of, known=known
        )

    return inserted_total

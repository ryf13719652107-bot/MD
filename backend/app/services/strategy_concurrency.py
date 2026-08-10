"""策略内并发锁（细于整段调度任务锁）。

分层：
  - scheduler 策略任务锁：同一策略的 :30/:40 调度互斥，避免两轮 manage/TP 重叠
  - 本模块「策略×币种×方向」锁：接针开仓 ↔ 同腿 manage/止盈成交处理 互斥

互斥粒度 = 同策略 + 同币种 + 同方向（long/short）。
账户上另一条反向策略（不同 strategy_id）互不影响；同策略异币也不影响。

接针开仓只抢腿锁、不抢任务锁，因此 :40 在管其他币/腿时，新腿仍可开仓。
注意：异币可并行开仓/管理，保证金会并发占用（方案2取舍）。
"""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

_leg_locks: dict[tuple[int, str, str], asyncio.Lock] = {}


def _norm_sym_key(symbol: str | None) -> str:
    """与 position_manager._norm_sym 对齐，避免未规范化符号变成另一把锁。"""
    return (symbol or "").upper().replace("/", "").replace(":USDT", "").replace("_", "")


def normalize_leg_side(side: str | None) -> str:
    s = (side or "").strip().lower()
    if s in ("buy", "long"):
        return "long"
    if s in ("sell", "short"):
        return "short"
    return s


def strategy_leg_lock(
    strategy_id: int, symbol_norm: str, side: str | None
) -> asyncio.Lock:
    key = (
        int(strategy_id),
        _norm_sym_key(symbol_norm),
        normalize_leg_side(side),
    )
    lock = _leg_locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _leg_locks[key] = lock
    return lock


# 兼容旧名
strategy_symbol_lock = strategy_leg_lock


def clear_strategy_symbol_locks(strategy_id: int) -> None:
    """停策略时清理。仍被持有的锁只从字典摘掉引用，不强制 release（避免误释放）。

    注意：若在持锁期间 pop 再创建同 key，会得到另一把锁而失去互斥。
    因此仅移除「当前未锁定」的条目；忙着的留给持有方释放后自然不再被索引到
    （下次同 key 会新建——故调用方应先停 runner / 等调度退出再 clear）。
    """
    sid = int(strategy_id)
    for key in [k for k in _leg_locks if k[0] == sid]:
        lock = _leg_locks.get(key)
        if lock is not None and lock.locked():
            continue
        _leg_locks.pop(key, None)


@asynccontextmanager
async def hold_strategy_symbol(
    strategy_id: int,
    symbol_norm: str,
    side: str | None,
    *,
    timeout: float | None = None,
) -> AsyncIterator[None]:
    """持有策略×币种×方向锁；timeout 秒内拿不到则抛 asyncio.TimeoutError。"""
    lock = strategy_leg_lock(strategy_id, symbol_norm, side)
    acquired = False
    try:
        if timeout is None:
            await lock.acquire()
        else:
            await asyncio.wait_for(lock.acquire(), timeout=float(timeout))
        acquired = True
        yield
    finally:
        if acquired:
            lock.release()

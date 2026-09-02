"""Account-level concurrency limits (shared by strategies on the same API key)."""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from contextvars import ContextVar

ACCOUNT_ORDER_CONCURRENCY = 3

_account_order_sems: dict[int, asyncio.Semaphore] = {}
_account_sync_locks: dict[int, asyncio.Lock] = {}
# 当前 task 已持有的账户同步锁，避免 check_tp 外包一层后再进 _close_positions 死锁
_held_account_sync: ContextVar[frozenset[int] | None] = ContextVar(
    "held_account_sync", default=None
)


def account_order_sem(account_id: int) -> asyncio.Semaphore:
    if account_id not in _account_order_sems:
        _account_order_sems[account_id] = asyncio.Semaphore(ACCOUNT_ORDER_CONCURRENCY)
    return _account_order_sems[account_id]


def account_sync_lock(account_id: int) -> asyncio.Lock:
    if account_id not in _account_sync_locks:
        _account_sync_locks[account_id] = asyncio.Lock()
    return _account_sync_locks[account_id]


@asynccontextmanager
async def hold_account_sync(account_id: int) -> AsyncIterator[None]:
    """账户级同步/写 Trade 锁；同 task 可重入。account_id<=0 时空操作。"""
    aid = int(account_id or 0)
    if aid <= 0:
        yield
        return
    held = _held_account_sync.get() or frozenset()
    if aid in held:
        yield
        return
    lock = account_sync_lock(aid)
    await lock.acquire()
    try:
        token = _held_account_sync.set(held | {aid})
        try:
            yield
        finally:
            _held_account_sync.reset(token)
    finally:
        lock.release()

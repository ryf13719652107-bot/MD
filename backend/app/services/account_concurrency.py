"""Account-level concurrency limits (shared by strategies on the same API key)."""
import asyncio

ACCOUNT_ORDER_CONCURRENCY = 3

_account_order_sems: dict[int, asyncio.Semaphore] = {}
_account_sync_locks: dict[int, asyncio.Lock] = {}


def account_order_sem(account_id: int) -> asyncio.Semaphore:
    if account_id not in _account_order_sems:
        _account_order_sems[account_id] = asyncio.Semaphore(ACCOUNT_ORDER_CONCURRENCY)
    return _account_order_sems[account_id]


def account_sync_lock(account_id: int) -> asyncio.Lock:
    if account_id not in _account_sync_locks:
        _account_sync_locks[account_id] = asyncio.Lock()
    return _account_sync_locks[account_id]

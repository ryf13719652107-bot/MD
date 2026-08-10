"""策略×币种×方向锁：接针开仓不与整段调度任务锁互斥。"""

import asyncio

import pytest

from app.services.scheduler import StrategyScheduler
from app.services.strategy_concurrency import (
    clear_strategy_symbol_locks,
    hold_strategy_symbol,
    normalize_leg_side,
    strategy_leg_lock,
)


@pytest.mark.asyncio
async def test_job_lock_does_not_block_leg_lock():
    """:40 任务锁被占用时，腿锁仍可立即拿到（方案2核心）。"""
    sched = StrategyScheduler()
    job = sched._get_strategy_lock(13)
    await job.acquire()
    try:
        t0 = asyncio.get_event_loop().time()
        async with hold_strategy_symbol(13, "GRVTUSDT", "short", timeout=0.2):
            elapsed = asyncio.get_event_loop().time() - t0
        assert elapsed < 0.15
    finally:
        job.release()
        clear_strategy_symbol_locks(13)


@pytest.mark.asyncio
async def test_same_leg_serializes():
    """同策略同币同向：manage 持锁时接针短等会超时。"""
    clear_strategy_symbol_locks(13)
    lock = strategy_leg_lock(13, "GRVTUSDT", "short")
    await lock.acquire()
    try:
        with pytest.raises(asyncio.TimeoutError):
            async with hold_strategy_symbol(13, "GRVTUSDT", "sell", timeout=0.05):
                pass
    finally:
        lock.release()
        clear_strategy_symbol_locks(13)


@pytest.mark.asyncio
async def test_opposite_side_parallel():
    """同策略同币反向不互斥（对冲腿）。"""
    clear_strategy_symbol_locks(13)
    async with hold_strategy_symbol(13, "GRVTUSDT", "short"):
        async with hold_strategy_symbol(13, "GRVTUSDT", "long", timeout=0.1):
            assert strategy_leg_lock(13, "GRVTUSDT", "short").locked()
            assert strategy_leg_lock(13, "GRVTUSDT", "long").locked()
    clear_strategy_symbol_locks(13)


@pytest.mark.asyncio
async def test_different_symbols_parallel():
    """异币种可并行持锁。"""
    clear_strategy_symbol_locks(13)
    async with hold_strategy_symbol(13, "BTCUSDT", "short"):
        async with hold_strategy_symbol(13, "GRVTUSDT", "short", timeout=0.1):
            assert strategy_leg_lock(13, "BTCUSDT", "short").locked()
            assert strategy_leg_lock(13, "GRVTUSDT", "short").locked()
    clear_strategy_symbol_locks(13)


def test_normalize_leg_side():
    assert normalize_leg_side("SELL") == "short"
    assert normalize_leg_side("buy") == "long"
    assert normalize_leg_side("SHORT") == "short"


def test_clear_strategy_symbol_locks():
    strategy_leg_lock(9, "AAAUSDT", "long")
    strategy_leg_lock(13, "BBBUSDT", "short")
    clear_strategy_symbol_locks(13)
    assert not strategy_leg_lock(13, "BBBUSDT", "short").locked()
    clear_strategy_symbol_locks(9)


def test_symbol_formats_share_same_lock():
    a = strategy_leg_lock(1, "BTC/USDT:USDT", "short")
    b = strategy_leg_lock(1, "BTCUSDT", "SELL")
    assert a is b


@pytest.mark.asyncio
async def test_clear_skips_held_lock():
    """持锁期间 clear 不得换成另一把锁（否则失去互斥）。"""
    clear_strategy_symbol_locks(7)
    lock = strategy_leg_lock(7, "ETHUSDT", "long")
    await lock.acquire()
    try:
        clear_strategy_symbol_locks(7)
        assert strategy_leg_lock(7, "ETHUSDT", "long") is lock
    finally:
        lock.release()
        clear_strategy_symbol_locks(7)

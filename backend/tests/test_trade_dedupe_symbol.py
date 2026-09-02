"""交易记录去重：符号规范化与同腿合并。"""

from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services.position_manager import (
    _collapse_phantom_l0_duplicates,
    _leg_close_trade_matches,
    _norm_sym,
    claim_open_position_close,
    find_existing_leg_close_trade,
)
from app.services.sync_service import _norm_leg_symbol


def test_norm_sym_unifies_ccxt_and_pool_formats():
    assert _norm_sym("BMTUSDT") == "BMTUSDT"
    assert _norm_sym("BMT/USDT:USDT") == "BMTUSDT"
    assert _norm_sym("bmt/usdt:usdt") == "BMTUSDT"
    assert _norm_leg_symbol("BMT/USDT:USDT") == _norm_sym("BMTUSDT")


def test_check_tp_processed_key_collapses_formats():
    """模拟 check_tp_fills 的 processed key：两种格式应算同一腿。"""
    rows = [
        ("BMTUSDT", "long"),
        ("BMT/USDT:USDT", "long"),
        ("BMTUSDT", "long"),
    ]
    processed: set[tuple[str, str]] = set()
    closed = 0
    for sym, side in rows:
        key = (_norm_sym(sym), side)
        if key in processed:
            continue
        processed.add(key)
        closed += 1
    assert closed == 1
    assert processed == {("BMTUSDT", "long")}


def test_collapse_phantom_l0_keeps_one_full_size_row():
    a = SimpleNamespace(
        id=1, layer=0, quantity=908.0, tp_limit_order_id="tp1",
        symbol="BMTUSDT", closed_at=None,
    )
    b = SimpleNamespace(
        id=2, layer=0, quantity=908.0, tp_limit_order_id="",
        symbol="BMT/USDT:USDT", closed_at=None,
    )
    kept = _collapse_phantom_l0_duplicates([a, b])
    assert len(kept) == 1
    assert kept[0] is a
    assert b.closed_at is not None


def test_collapse_keeps_martingale_layers():
    l0 = SimpleNamespace(
        id=1, layer=0, quantity=100.0, tp_limit_order_id="tp",
        symbol="BMTUSDT", closed_at=None,
    )
    l1 = SimpleNamespace(
        id=2, layer=1, quantity=150.0, tp_limit_order_id="",
        symbol="BMTUSDT", closed_at=None,
    )
    kept = _collapse_phantom_l0_duplicates([l0, l1])
    assert len(kept) == 2
    assert l1.closed_at is None


def test_leg_close_trade_matches_same_fill():
    ts = datetime(2026, 9, 1, 17, 21, 16)
    existing = SimpleNamespace(
        symbol="HEMI/USDT:USDT",
        side="short",
        layer=0,
        entry_time=ts,
        entry_price=0.015754,
        quantity=3536.0,
    )
    assert _leg_close_trade_matches(
        existing,
        symbol="HEMIUSDT",
        side="short",
        layer=0,
        entry_time=ts,
        entry_price=0.015754,
    )
    assert not _leg_close_trade_matches(
        existing,
        symbol="HEMIUSDT",
        side="long",
        layer=0,
        entry_time=ts,
        entry_price=0.015754,
    )
    later = ts + timedelta(seconds=20)
    assert not _leg_close_trade_matches(
        existing,
        symbol="HEMIUSDT",
        side="short",
        layer=0,
        entry_time=later,
        entry_price=0.015754,
    )
    assert not _leg_close_trade_matches(
        existing,
        symbol="HEMIUSDT",
        side="short",
        layer=0,
        entry_time=ts,
        entry_price=0.015754,
        quantity=10.0,
    )


@pytest.mark.asyncio
async def test_claim_open_position_close_uses_rowcount():
    session = AsyncMock()
    result = MagicMock()
    result.rowcount = 1
    session.execute = AsyncMock(return_value=result)
    ts = datetime(2026, 9, 1, 17, 21, 39)
    assert await claim_open_position_close(
        session, 12, ts, symbol_norm="HEMIUSDT"
    )
    result.rowcount = 0
    assert not await claim_open_position_close(session, 12, ts)
    assert await claim_open_position_close(session, None, ts) is False


@pytest.mark.asyncio
async def test_find_existing_leg_close_trade_sees_session_new():
    ts = datetime(2026, 9, 1, 16, 25, 11)
    from app.models.trade import Trade

    pending = Trade(
        strategy_id=1,
        account_id=10,
        symbol="0GUSDT",
        side="long",
        quantity=197.0,
        entry_price=0.2251,
        exit_price=0.2278,
        realized_pnl=0.5319,
        pnl_pct=1.2,
        entry_time=ts,
        exit_time=ts + timedelta(seconds=81),
        layer=0,
        close_reason="take_profit",
    )
    session = AsyncMock()
    empty = MagicMock()
    empty.scalars.return_value.all.return_value = []
    session.execute = AsyncMock(return_value=empty)
    session.new = [pending]
    found = await find_existing_leg_close_trade(
        session,
        strategy_id=1,
        symbol="0G/USDT:USDT",
        side="long",
        layer=0,
        entry_time=ts,
        entry_price=0.2251,
    )
    assert found is pending

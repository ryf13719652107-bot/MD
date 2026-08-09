"""交易记录去重：符号规范化与同腿合并。"""

from types import SimpleNamespace

from app.services.position_manager import _collapse_phantom_l0_duplicates, _norm_sym
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

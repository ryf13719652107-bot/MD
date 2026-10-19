"""接针首仓后针深翻倍加仓追踪：纯判定逻辑。"""

from app.services.position_manager import wick_add_track_decision


def _decision(side="short", **kw):
    base = dict(
        side=side,
        bar_open=100.0,
        entry_price=105.0,
        current_price=110.0,
        threshold_mult=2.0,
        qty_mult=1.5,
        min_depth_pct=0.5,
        base_qty=10.0,
    )
    base.update(kw)
    return wick_add_track_decision(**base)


def test_short_triggered_at_doubled_depth():
    r = _decision(current_price=110.0)
    assert r is not None
    assert r.should_add is True
    assert r.next_layer == 1
    assert r.next_quantity == 15.0  # 10 * 1.5
    assert r.last_entry_price == 105.0
    assert r.price_drop_from_last == 5.0  # (105-100)/100*100


def test_short_not_crossed_returns_none():
    # 现价 109 < 触发价 110 → 未触发，调用方回退原马丁
    assert _decision(current_price=109.0) is None


def test_short_below_min_depth_returns_none():
    # 针深 0.2% < 下限 0.5% → 不启用追踪加仓
    assert _decision(entry_price=100.2, current_price=110.0) is None


def test_long_triggered_down():
    r = _decision(side="long", entry_price=95.0, current_price=90.0)
    assert r is not None
    assert r.next_layer == 1
    assert r.price_drop_from_last == 5.0  # (100-95)/100*100


def test_long_not_crossed_returns_none():
    assert _decision(side="long", entry_price=95.0, current_price=90.1) is None


def test_invalid_inputs_return_none():
    assert _decision(bar_open=0.0) is None
    assert _decision(entry_price=0.0) is None
    assert _decision(current_price=0.0) is None
    assert _decision(base_qty=0.0) is None


def test_min_depth_zero_allows_any_depth():
    # min_depth=0 关闭下限：极小针深也能触发
    r = _decision(entry_price=100.01, current_price=100.03, min_depth_pct=0.0)
    assert r is not None

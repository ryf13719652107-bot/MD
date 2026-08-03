"""开仓质量：最终针尖与盈亏分桶单元测试。"""

from datetime import datetime
from types import SimpleNamespace

from app.services.wick_spike_outcome import (
    OpenOutcome,
    _final_tip_metrics,
    _pnl_buckets,
    match_trade,
)


def test_final_tip_metrics_short():
    # open=100, high=110, entry=108 → 距针尖 2%，针深 10%，捕获 0.8
    ext, gap, wick, cap = _final_tip_metrics("short", 100.0, 108.0, 110.0, 99.0)
    assert abs(ext - 110.0) < 1e-9
    assert abs(gap - 2.0) < 1e-9
    assert abs(wick - 10.0) < 1e-9
    assert abs(cap - 0.8) < 1e-9


def test_final_tip_metrics_long():
    ext, gap, wick, cap = _final_tip_metrics("long", 100.0, 92.0, 101.0, 90.0)
    assert abs(ext - 90.0) < 1e-9
    assert abs(gap - 2.0) < 1e-9
    assert abs(wick - 10.0) < 1e-9
    assert abs(cap - 0.8) < 1e-9


def test_match_trade_prefers_layer0():
    trades = [
        SimpleNamespace(
            id=1,
            strategy_id=9,
            symbol="BLESSUSDT",
            side="short",
            entry_time=datetime(2026, 8, 3, 9, 22, 5),
            layer=1,
            entry_price=1.0,
        ),
        SimpleNamespace(
            id=2,
            strategy_id=9,
            symbol="BLESSUSDT",
            side="short",
            entry_time=datetime(2026, 8, 3, 9, 22, 8),
            layer=0,
            entry_price=1.1,
        ),
    ]
    hit = match_trade(
        trades,
        strategy_id=9,
        symbol="BLESSUSDT",
        side="short",
        event_dt=datetime(2026, 8, 3, 9, 22, 10),
    )
    assert hit is not None
    assert hit.id == 2


def test_pnl_buckets_groups_by_tip_gap():
    rows = [
        OpenOutcome(
            ts="t",
            strategy_id=1,
            symbol="A",
            side="long",
            side_zh="做多",
            entry_px=1,
            bar_open=1,
            trigger_ext=None,
            tip_gap_at_trigger_pct=None,
            final_ext=None,
            final_tip_gap_pct=0.1,
            wick_range_pct=2,
            capture_ratio=0.9,
            trade_id=1,
            trade_entry=1,
            trade_exit=1.01,
            realized_pnl=1,
            pnl_pct=1.0,
            close_reason="take_profit",
            layer=0,
            matched=True,
            kline_ok=True,
        ),
        OpenOutcome(
            ts="t2",
            strategy_id=1,
            symbol="B",
            side="long",
            side_zh="做多",
            entry_px=1,
            bar_open=1,
            trigger_ext=None,
            tip_gap_at_trigger_pct=None,
            final_ext=None,
            final_tip_gap_pct=2.5,
            wick_range_pct=3,
            capture_ratio=0.2,
            trade_id=2,
            trade_entry=1,
            trade_exit=0.99,
            realized_pnl=-1,
            pnl_pct=-0.5,
            close_reason="stop_loss",
            layer=0,
            matched=True,
            kline_ok=True,
        ),
    ]
    buckets = _pnl_buckets(rows)
    by_label = {b["bucket"]: b for b in buckets}
    assert by_label["贴尖 ≤0.3%"]["n"] == 1
    assert by_label["贴尖 ≤0.3%"]["avg_pnl_pct"] == 1.0
    assert by_label["偏离 >2%"]["n"] == 1
    assert by_label["偏离 >2%"]["avg_pnl_pct"] == -0.5

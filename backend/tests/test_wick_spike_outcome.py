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


def test_final_tip_metrics_icnt_chart_fill():
    """实盘 ICNT：开0.1156 低0.1115 成交0.1131 → 最终贴尖≈1.384 捕获≈0.610。"""
    ext, gap, wick, cap = _final_tip_metrics("long", 0.1156, 0.1131, 0.1158, 0.1115)
    assert abs(ext - 0.1115) < 1e-9
    assert abs(gap - 1.384083) < 1e-3
    assert abs(wick - 3.546713) < 1e-3
    assert abs(cap - 0.609756) < 1e-3


def test_order_fill_avg_price_prefers_info_avg():
    from app.services.position_manager import _order_fill_avg_price

    order = {"average": 0, "price": 0, "info": {"avgPrice": "0.1131"}}
    assert abs(_order_fill_avg_price(order, fallback=0.1121) - 0.1131) < 1e-9
    assert abs(_order_fill_avg_price({"average": 0.11}, 0.01) - 0.11) < 1e-9
    # 市价单顶层 price 常是信号/参考价，不得压过 info.avgPrice
    mixed = {
        "average": 0,
        "price": 0.06824,
        "info": {"avgPrice": "0.0683397"},
    }
    assert abs(_order_fill_avg_price(mixed, fallback=0.06824) - 0.0683397) < 1e-9


def test_order_fill_avg_price_tp_never_uses_limit_price():
    """止盈出场：禁止用挂单价；可用 cost/filled 推算。"""
    from app.services.position_manager import _order_fill_avg_price
    from app.services.sync_service import _parse_order_exit_price

    # BLESS 类：average 空，price=理论止盈挂单价 → 不得采用
    limit_only = {
        "average": 0,
        "price": 0.018519,
        "filled": 522,
        "info": {"price": "0.018519", "avgPrice": "0", "executedQty": "522"},
    }
    assert _order_fill_avg_price(limit_only, 0.0, allow_order_price=False) == 0.0
    assert _parse_order_exit_price(limit_only) == 0.0

    # cumQuote/executedQty 推算真实均价
    with_quote = {
        "average": 0,
        "price": 0.018519,
        "filled": 522,
        "info": {
            "price": "0.018519",
            "avgPrice": "0",
            "executedQty": "522",
            "cumQuote": str(522 * 0.019019),
        },
    }
    assert abs(
        _order_fill_avg_price(with_quote, 0.0, allow_order_price=False) - 0.019019
    ) < 1e-12
    assert abs(_parse_order_exit_price(with_quote) - 0.019019) < 1e-12


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

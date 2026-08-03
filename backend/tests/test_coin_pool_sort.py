from types import SimpleNamespace

from app.services.coin_pool_service import (
    apply_coin_pool_top_n,
    sort_coin_pool_by_price_change,
)


def _coin(symbol: str, pct: float, source: str = "gainers"):
    return SimpleNamespace(symbol=symbol, price_change_pct=pct, source=source)


def test_sort_gainers_desc():
    coins = [_coin("A", 10), _coin("B", 30), _coin("C", 20)]
    out = sort_coin_pool_by_price_change(coins, "gainers")
    assert [c.symbol for c in out] == ["B", "C", "A"]


def test_sort_losers_asc():
    coins = [_coin("A", -5, "losers"), _coin("B", -20, "losers"), _coin("C", -10, "losers")]
    out = sort_coin_pool_by_price_change(coins, "losers")
    assert [c.symbol for c in out] == ["B", "C", "A"]


def test_sort_both_groups():
    coins = [
        _coin("G1", 5, "gainers"),
        _coin("L1", -3, "losers"),
        _coin("G2", 15, "gainers"),
        _coin("L2", -10, "losers"),
    ]
    out = sort_coin_pool_by_price_change(coins, "both")
    assert [c.symbol for c in out] == ["G2", "G1", "L2", "L1"]


def test_both_top_n_takes_each_side():
    """both + top_n=2 → 涨2 + 跌2 = 4，不是合并后只取前2。"""
    coins = [
        _coin("G1", 30, "gainers"),
        _coin("G2", 20, "gainers"),
        _coin("G3", 10, "gainers"),
        _coin("L1", -30, "losers"),
        _coin("L2", -20, "losers"),
        _coin("L3", -10, "losers"),
    ]
    out = apply_coin_pool_top_n(coins, "both", 2)
    assert [c.symbol for c in out] == ["G1", "G2", "L1", "L2"]
    assert len(out) == 4


def test_single_source_top_n_unchanged():
    coins = [_coin(f"G{i}", 100 - i, "gainers") for i in range(5)]
    out = apply_coin_pool_top_n(coins, "gainers", 3)
    assert [c.symbol for c in out] == ["G0", "G1", "G2"]

from types import SimpleNamespace

from app.services.coin_pool_service import sort_coin_pool_by_price_change


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

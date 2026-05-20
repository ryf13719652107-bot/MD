"""选币池成交量过滤逻辑（与 DB 无关）。"""


def _filter_by_volume(coins: list[dict], min_volume_24h: float, limit: int) -> list[str]:
    if min_volume_24h > 0:
        coins = [c for c in coins if (c.get("volume_24h") or 0) >= min_volume_24h]
    if limit > 0:
        coins = coins[:limit]
    return [c["symbol"] for c in coins]


def test_min_volume_filters_before_top_n():
    rows = [
        {"symbol": "A", "volume_24h": 1e9, "rank": 1},
        {"symbol": "B", "volume_24h": 5e7, "rank": 2},
        {"symbol": "C", "volume_24h": 2e8, "rank": 3},
    ]
    assert _filter_by_volume(rows, min_volume_24h=1e8, limit=2) == ["A", "C"]


def test_min_volume_zero_no_filter():
    rows = [{"symbol": "X", "volume_24h": 1.0, "rank": 1}]
    assert _filter_by_volume(rows, min_volume_24h=0, limit=0) == ["X"]

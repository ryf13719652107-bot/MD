"""选币池：先榜内 top N，再成交量/TradFi 过滤（与 coin_pool_service 一致）。"""


def _effective(
    coins: list[dict],
    *,
    min_volume_24h: float = 0,
    limit: int = 0,
) -> list[str]:
    if limit > 0:
        coins = coins[:limit]
    if min_volume_24h > 0:
        coins = [c for c in coins if (c.get("volume_24h") or 0) >= min_volume_24h]
    return [c["symbol"] for c in coins]


def test_top_n_before_volume_filter():
    rows = [
        {"symbol": "A", "volume_24h": 1e9, "rank": 1},
        {"symbol": "B", "volume_24h": 5e7, "rank": 2},
        {"symbol": "C", "volume_24h": 2e8, "rank": 3},
    ]
    # 榜内前 2 名再滤成交量：B 被剔除，只剩 A
    assert _effective(rows, min_volume_24h=1e8, limit=2) == ["A"]


def test_rank_beyond_top_n_never_included():
    rows = [{"symbol": f"S{i}", "volume_24h": 1e9, "rank": i} for i in range(1, 31)]
    result = _effective(rows, limit=20)
    assert len(result) == 20
    assert "S29" not in result
    assert "S20" in result


def test_min_volume_zero_keeps_top_n_only():
    rows = [{"symbol": "X", "volume_24h": 1.0, "rank": 1}]
    assert _effective(rows, limit=1) == ["X"]

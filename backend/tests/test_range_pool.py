"""4h 震荡选币指标与过滤。"""

from app.services.range_pool import (
    EXCLUDED_MAJOR_SYMBOLS,
    build_scan_candidates,
    calculate_adx,
    calculate_range_pct,
    oscillation_score,
    _passes_range_filters,
    ADX_MAX,
    RANGE_MIN_PCT,
    RANGE_MAX_PCT,
)


def _flat_ohlcv(n: int, price: float = 100.0) -> list:
    rows = []
    for i in range(n):
        p = price + (i % 3 - 1) * 0.2
        rows.append([i, p, p + 0.5, p - 0.5, p, 1000.0])
    return rows


def test_calculate_range_pct_flat():
    klines = _flat_ohlcv(60, 100.0)
    rp = calculate_range_pct(klines)
    assert rp is not None
    assert rp < RANGE_MIN_PCT + 2


def test_calculate_adx_low_on_flat():
    klines = _flat_ohlcv(60, 100.0)
    adx = calculate_adx(klines, 14)
    assert adx is not None
    assert adx < ADX_MAX


def test_passes_range_filters():
    assert _passes_range_filters(18.0, 12.0, 3.0) is True
    assert _passes_range_filters(25.0, 12.0, 3.0) is False
    assert _passes_range_filters(18.0, 4.0, 3.0) is False
    assert _passes_range_filters(18.0, 45.0, 3.0) is True
    assert _passes_range_filters(18.0, 55.0, 3.0) is False
    assert _passes_range_filters(18.0, 12.0, 40.0) is True
    assert _passes_range_filters(18.0, 12.0, 51.0) is False


def test_oscillation_score_orders_lower_adx_higher():
    high = oscillation_score(15.0, 12.0, 1e8, 2.0)
    low = oscillation_score(21.0, 12.0, 1e8, 2.0)
    assert high > low


def test_build_scan_candidates_excludes_major_symbols():
    items = [
        {"symbol": "BTCUSDT", "volume_24h": 1e10, "price_change_pct": 1.0},
        {"symbol": "ETHUSDT", "volume_24h": 5e9, "price_change_pct": 1.0},
        {"symbol": "ALTUSDT", "volume_24h": 5e7, "price_change_pct": 2.0},
    ]
    scan = build_scan_candidates(items, max_scan=10)
    syms = {x["symbol"] for x in scan}
    assert "BTCUSDT" not in syms
    assert "ETHUSDT" not in syms
    assert "ALTUSDT" in syms


def test_build_scan_candidates_volume_order_skipping_majors():
    items = [{"symbol": s, "volume_24h": 1e9, "price_change_pct": 0} for s in EXCLUDED_MAJOR_SYMBOLS]
    items.append({"symbol": "MEMEUSDT", "volume_24h": 8e7, "price_change_pct": 0})
    items.append({"symbol": "GAMEUSDT", "volume_24h": 6e7, "price_change_pct": 0})
    scan = build_scan_candidates(items, min_volume_24h=20_000_000, max_scan=5)
    assert [x["symbol"] for x in scan] == ["MEMEUSDT", "GAMEUSDT"]

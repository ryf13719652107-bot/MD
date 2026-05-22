"""TradFi / 黄金白银原油等非加密货币排除。"""

from app.services.binance_service import (
    EXCLUDED_COMMODITY_SYMBOLS,
    is_non_crypto_commodity_symbol,
    is_tradefi_or_commodity_symbol,
)
from app.services.range_pool import build_scan_candidates


def test_commodity_symbols_detected():
    for sym in ("XAUUSDT", "XAGUSDT", "USOILUSDT", "UKOILUSDT"):
        assert is_non_crypto_commodity_symbol(sym)
    assert not is_non_crypto_commodity_symbol("PEPEUSDT")
    assert not is_non_crypto_commodity_symbol("GASUSDT")


def test_tradefi_set_union_commodity():
    tradefi = frozenset({"AAPLUSDT"})
    assert is_tradefi_or_commodity_symbol("AAPLUSDT", tradefi)
    assert is_tradefi_or_commodity_symbol("XAUUSDT", tradefi)
    assert not is_tradefi_or_commodity_symbol("ETHUSDT", tradefi)


def test_build_scan_candidates_skips_commodity():
    items = [
        {"symbol": "XAUUSDT", "volume_24h": 1e9, "price_change_pct": 1.0},
        {"symbol": "ALTUSDT", "volume_24h": 5e7, "price_change_pct": 2.0},
    ]
    scan = build_scan_candidates(
        items,
        excluded_symbols=frozenset(),
        tradefi_norm=EXCLUDED_COMMODITY_SYMBOLS,
        min_volume_24h=20_000_000,
        max_scan=10,
    )
    assert [x["symbol"] for x in scan] == ["ALTUSDT"]

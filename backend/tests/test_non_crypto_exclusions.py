"""TradFi / 黄金白银原油等非加密货币排除。"""

from app.services.binance_service import (
    EXCLUDED_COMMODITY_SYMBOLS,
    is_non_crypto_commodity_symbol,
    is_tradefi_or_commodity_symbol,
)


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

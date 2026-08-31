import pytest
from fastapi import HTTPException

from app.routers.strategies import _normalize_symbol_input


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("btcusdt", "BTCUSDT"),
        (" BTCUSDT ", "BTCUSDT"),
        ("btc/usdt:usdt", "BTCUSDT"),
        ("1000pepeusdt", "1000PEPEUSDT"),
        ("btc", "BTCUSDT"),
        ("币安人生USDT", "币安人生USDT"),
        ("币安人生usdt", "币安人生USDT"),
        (" 币安人生 ", "币安人生USDT"),
        ("币安人生/USDT:USDT", "币安人生USDT"),
        ("四USDT", "四USDT"),
        ("四", "四USDT"),
    ],
)
def test_normalize_blacklist_symbol(raw, expected):
    assert _normalize_symbol_input(raw) == expected


@pytest.mark.parametrize("raw", ["BTC-USDT", "USDT", "", "   "])
def test_reject_invalid_blacklist_symbol(raw):
    with pytest.raises(HTTPException) as exc:
        _normalize_symbol_input(raw)

    assert exc.value.status_code == 400
    assert "USDT" in exc.value.detail

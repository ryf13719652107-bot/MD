"""14 天内下架合约识别。"""

import time

from app.services.binance_service import (
    DELIST_LOOKAHEAD_MS,
    _symbol_delisting_soon,
)


def test_non_trading_status():
    now = int(time.time() * 1000)
    assert _symbol_delisting_soon({"status": "SETTLING", "deliveryDate": 9999999999999}, now)
    assert not _symbol_delisting_soon({"status": "TRADING", "deliveryDate": now + 365 * 86400 * 1000}, now)


def test_delivery_within_14_days():
    now = int(time.time() * 1000)
    soon = now + DELIST_LOOKAHEAD_MS - 3600 * 1000
    assert _symbol_delisting_soon({"status": "TRADING", "deliveryDate": soon}, now)
    far = now + DELIST_LOOKAHEAD_MS + 86400 * 1000
    assert not _symbol_delisting_soon({"status": "TRADING", "deliveryDate": far}, now)


def test_delivery_already_past():
    now = int(time.time() * 1000)
    assert _symbol_delisting_soon({"status": "TRADING", "deliveryDate": now - 1000}, now)


def test_delivery_zero_not_treated_as_delisted():
    now = int(time.time() * 1000)
    assert not _symbol_delisting_soon({"status": "TRADING", "deliveryDate": 0}, now)

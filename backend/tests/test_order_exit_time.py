"""Tests for exchange order exit time parsing."""
from datetime import datetime

from app.config import BEIJING_TZ
from app.services.order_times import exit_time_from_order, naive_beijing_from_ms_or_s


def test_naive_beijing_from_ms():
    dt = naive_beijing_from_ms_or_s(1_700_000_000_000)
    assert dt is not None
    assert dt.tzinfo is None


def test_exit_time_from_binance_update_time():
    order = {"info": {"updateTime": "1717758184000", "avgPrice": "0.85324"}}
    dt = exit_time_from_order(order)
    expected = datetime.fromtimestamp(1717758184.0, BEIJING_TZ).replace(tzinfo=None)
    assert dt == expected


def test_exit_time_fallback_when_missing():
    fallback = datetime(2026, 6, 7, 20, 3, 20)
    assert exit_time_from_order({}, fallback=fallback) == fallback

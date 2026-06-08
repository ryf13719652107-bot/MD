"""Coin pool scheduled refresh timing."""
from datetime import datetime

import pytest

from app.services.coin_pool_service import CoinPoolService


def _dt(y, m, d, h, mi=0):
    return datetime(y, m, d, h, mi, 0)


@pytest.fixture
def svc():
    s = CoinPoolService()
    s.update_config(refresh_interval_seconds=4 * 3600, fetch_mode="scheduled", anchor_hour=8)
    return s


def test_scheduled_first_refresh_waits_for_anchor(monkeypatch, svc):
    monkeypatch.setattr("app.services.coin_pool_service.now_beijing", lambda: _dt(2026, 6, 8, 10, 30))
    delay = svc._seconds_until_next_refresh(None)
    # 10:30 → next anchor slot 12:00 = 1.5h
    assert delay == pytest.approx(1.5 * 3600, rel=0.01)


def test_scheduled_respects_last_refresh_on_restart(monkeypatch, svc):
    monkeypatch.setattr("app.services.coin_pool_service.now_beijing", lambda: _dt(2026, 6, 8, 12, 30))
    last = _dt(2026, 6, 8, 12, 0)
    delay = svc._seconds_until_next_refresh(last)
    # last 12:00 + 4h = 16:00 earliest; anchor grid → 16:00 = 3.5h from 12:30
    assert delay == pytest.approx(3.5 * 3600, rel=0.01)


def test_scheduled_grid_continues_before_todays_anchor(monkeypatch, svc):
    """凌晨 03:00 应等 04:00（昨日锚点网格），而非跳到当日 08:00。"""
    monkeypatch.setattr("app.services.coin_pool_service.now_beijing", lambda: _dt(2026, 6, 9, 3, 0))
    delay = svc._seconds_until_next_refresh(None)
    assert delay == pytest.approx(3600, rel=0.01)


def test_interval_aligns_from_last_refresh(monkeypatch):
    svc = CoinPoolService()
    svc.update_config(refresh_interval_seconds=3600, fetch_mode="interval")
    monkeypatch.setattr("app.services.coin_pool_service.now_beijing", lambda: _dt(2026, 6, 8, 10, 30))
    last = _dt(2026, 6, 8, 10, 0)
    delay = svc._seconds_until_next_refresh(last)
    assert delay == pytest.approx(30 * 60, rel=0.01)

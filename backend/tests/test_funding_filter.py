"""资金费率过滤（选币池新开仓）。"""

from app.services.strategy_flags import (
    exclude_funding_enabled,
    funding_rate_blocks_new_entry,
    funding_rate_threshold_pct,
)


class _Strategy:
    def __init__(self, exclude_funding=False, funding_rate_threshold_pct=0.0):
        self.exclude_funding = exclude_funding
        self.funding_rate_threshold_pct = funding_rate_threshold_pct


def test_funding_long_blocks_above_threshold():
    assert funding_rate_blocks_new_entry("long", 0.06, 0.05) is True
    assert funding_rate_blocks_new_entry("long", 0.05, 0.05) is False
    assert funding_rate_blocks_new_entry("long", 0.01, 0.0) is True
    assert funding_rate_blocks_new_entry("long", 0.0, 0.0) is False


def test_funding_short_blocks_below_threshold():
    assert funding_rate_blocks_new_entry("short", -0.06, -0.05) is True
    assert funding_rate_blocks_new_entry("short", -0.05, -0.05) is False
    assert funding_rate_blocks_new_entry("short", -0.01, 0.0) is True
    assert funding_rate_blocks_new_entry("short", 0.0, 0.0) is False


def test_exclude_funding_default_off():
    assert exclude_funding_enabled(_Strategy(exclude_funding=False)) is False
    assert exclude_funding_enabled(_Strategy(exclude_funding=True)) is True


def test_funding_threshold_default_zero():
    assert funding_rate_threshold_pct(_Strategy(funding_rate_threshold_pct=None)) == 0.0

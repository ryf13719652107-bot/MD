"""接针加仓模式：仅涨跌幅 / 涨跌幅+WT 确认。"""

from unittest.mock import patch

from app.services.position_manager import (
    martingale_wt_confirm_allows_add,
    wick_martingale_mode_needs_wt,
)
from app.services.strategy_engine import Signal


def test_wick_martingale_mode_needs_wt():
    # 缺省 / 空 → 默认涨跌幅+WT
    assert wick_martingale_mode_needs_wt(None) is True
    assert wick_martingale_mode_needs_wt("") is True
    assert wick_martingale_mode_needs_wt("price_drop") is False
    assert wick_martingale_mode_needs_wt("price_and_wt") is True


def test_martingale_wt_confirm_neutral_blocks():
    with patch(
        "app.services.position_manager.calculate_wavetrend",
        return_value={"wt1": 10.0, "wt2": 12.0, "cross_above": False, "cross_below": False},
    ), patch(
        "app.services.position_manager.generate_wt_signal",
        return_value=Signal.NEUTRAL,
    ):
        ok, detail = martingale_wt_confirm_allows_add(
            [[]],
            direction="short",
            wt_channel_length=10,
            wt_average_length=21,
            wt_os_level=-60.0,
            wt_ob_level=60.0,
        )
    assert ok is False
    assert "信号已消失" in detail


def test_martingale_wt_confirm_signal_allows():
    with patch(
        "app.services.position_manager.calculate_wavetrend",
        return_value={"wt1": 70.0, "wt2": 65.0, "cross_above": False, "cross_below": True},
    ), patch(
        "app.services.position_manager.generate_wt_signal",
        return_value=Signal.SHORT,
    ):
        ok, detail = martingale_wt_confirm_allows_add(
            [[]],
            direction="short",
            wt_channel_length=10,
            wt_average_length=21,
            wt_os_level=-60.0,
            wt_ob_level=60.0,
        )
    assert ok is True
    assert "WT1=" in detail


def test_martingale_wt_unavailable_allows():
    """与既有 wavetrend 加仓一致：WT 算不出时不拦截。"""
    with patch(
        "app.services.position_manager.calculate_wavetrend",
        return_value=None,
    ):
        ok, detail = martingale_wt_confirm_allows_add(
            [[]],
            direction="long",
            wt_channel_length=10,
            wt_average_length=21,
            wt_os_level=-60.0,
            wt_ob_level=60.0,
        )
    assert ok is True
    assert "跳过确认" in detail

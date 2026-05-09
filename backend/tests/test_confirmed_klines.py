"""已收盘 K 线筛选 (与 TV 收盘确认对齐)。"""
from unittest.mock import patch

from app.services.position_manager import _klines_for_confirmed_signal_only


def test_confirmed_excludes_forming_last_candle():
    t0 = 1_700_000_000_000
    t1 = t0 + 60_000
    klines = [[t0, 1, 1, 1, 1, 1], [t1, 2, 2, 2, 2, 2]]
    # 仍在 t1 这根的前半根内 → 去掉未收盘（周期以传入的 timeframe 为准）
    with patch("time.time", return_value=(t1 + 30_000) / 1000.0):
        out = _klines_for_confirmed_signal_only(klines, "1m")
        assert len(out) == 1
        assert out[-1][0] == t0
    # 已过 t1+60s → t1 已收盘，保留两根
    with patch("time.time", return_value=(t1 + 61_000) / 1000.0):
        out = _klines_for_confirmed_signal_only(klines, "1m")
        assert len(out) == 2


def test_confirmed_respects_strategy_timeframe_not_hardcoded_1m():
    """策略设 5m 时，收盘边界按 5 分钟而非 1 分钟。"""
    t0 = 1_700_000_000_000
    t1 = t0 + 300_000
    klines = [[t0, 1, 1, 1, 1, 1], [t1, 2, 2, 2, 2, 2]]
    # 若误用 1m，会认为 t1 已收盘；5m 下仍应去掉最后一根
    with patch("time.time", return_value=(t1 + 60_000) / 1000.0):
        out = _klines_for_confirmed_signal_only(klines, "5m")
        assert len(out) == 1
    with patch("time.time", return_value=(t1 + 300_001) / 1000.0):
        out = _klines_for_confirmed_signal_only(klines, "5m")
        assert len(out) == 2

"""已收盘 K 线筛选 (与 TV 收盘确认对齐)。"""
from unittest.mock import patch

from app.services.position_manager import (
    _confirmed_klines_fixed,
    _klines_for_confirmed_signal_only,
)
from app.services.strategy_engine import calculate_wavetrend


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


def test_confirmed_klines_fixed_uses_same_tail():
    """缓冲长短不同时，截成相同已收盘窗口后 WT 必须一致。"""
    t0 = 1_700_000_000_000
    bars = []
    px = 100.0
    for i in range(400):
        o = px
        px = px + (1.0 if i % 3 == 0 else -0.6)
        ts = t0 + i * 60_000
        hi, lo = max(o, px) + 0.2, min(o, px) - 0.2
        bars.append([ts, o, hi, lo, px, 10.0])
    now = (bars[-1][0] + 30_000) / 1000.0  # 最后一根未收盘
    with patch("time.time", return_value=now):
        a = _confirmed_klines_fixed(bars, "1m", limit=200)
        b = _confirmed_klines_fixed(bars[-250:], "1m", limit=200)
    assert len(a) == 200
    assert len(b) == 200
    assert a[0][0] == b[0][0]
    wt_a = calculate_wavetrend(a, 10, 21)
    wt_b = calculate_wavetrend(b, 10, 21)
    assert wt_a is not None and wt_b is not None
    assert abs(wt_a["wt1"] - wt_b["wt1"]) < 1e-9

"""并行扫信号用策略快照，避免 ORM MissingGreenlet。"""
import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock

from app.models.strategy import Strategy
from app.services.position_manager import PositionManager, strategy_signal_snapshot
from app.services.strategy_engine import Signal


def test_strategy_signal_snapshot_copies_scalars():
    s = Strategy(
        account_id=1,
        name="t",
        direction="long",
        signal_source="wavetrend",
        timeframe="1h",
        wt_channel_length=10,
        wt_average_length=21,
    )
    snap = strategy_signal_snapshot(s)
    assert isinstance(snap, SimpleNamespace)
    assert snap.direction == "long"
    assert snap.signal_source == "wavetrend"
    assert snap.timeframe == "1h"
    assert snap.wt_channel_length == 10
    snap.timeframe = "5m"
    assert s.timeframe == "1h"


def test_fetch_klines_mutate_false_skips_writes():
    mgr = PositionManager()

    async def _fake_load(*_a, **_k):
        return [[0, 1, 1, 1, 1.5, 10]]

    mgr._load_klines = _fake_load  # type: ignore[method-assign]
    snap = SimpleNamespace(
        id=2,
        signal_source="wick_spike",
        timeframe="1m",
        direction="long",
        last_signal=None,
        last_signal_at=None,
        last_rsi=None,
    )
    out = asyncio.run(
        mgr._fetch_klines_and_signal(
            snap, "BTCUSDT", MagicMock(), mutate_strategy=False
        )
    )
    assert out is not None
    assert out[3] == Signal.NEUTRAL
    assert snap.last_signal is None
    assert snap.last_signal_at is None

"""主流币排除（选币池模式）。"""

from app.services.binance_service import EXCLUDED_MAINSTREAM_SYMBOLS
from app.services.strategy_flags import exclude_mainstream_enabled


class _Strategy:
    def __init__(self, exclude_mainstream=None, use_coin_pool=True):
        self.exclude_mainstream = exclude_mainstream
        self.use_coin_pool = use_coin_pool


def test_mainstream_symbols_count():
    assert len(EXCLUDED_MAINSTREAM_SYMBOLS) == 21
    assert "BTCUSDT" in EXCLUDED_MAINSTREAM_SYMBOLS
    assert "ETHUSDT" in EXCLUDED_MAINSTREAM_SYMBOLS
    assert "POLUSDT" in EXCLUDED_MAINSTREAM_SYMBOLS
    assert "MATICUSDT" in EXCLUDED_MAINSTREAM_SYMBOLS
    assert "PEPEUSDT" not in EXCLUDED_MAINSTREAM_SYMBOLS


def test_exclude_mainstream_enabled_defaults_true():
    assert exclude_mainstream_enabled(_Strategy(exclude_mainstream=None)) is True
    assert exclude_mainstream_enabled(_Strategy(exclude_mainstream=True)) is True
    assert exclude_mainstream_enabled(_Strategy(exclude_mainstream=False)) is False

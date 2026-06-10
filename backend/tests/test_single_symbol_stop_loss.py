from app.services.binance_service import extract_usdt_wallet_balance
from app.services.position_manager import (
    _single_symbol_stop_loss_trigger,
    _symbol_unrealized_pnl_from_exchange,
)


def test_single_symbol_stop_loss_not_triggered_without_loss():
    assert not _single_symbol_stop_loss_trigger(wallet_balance=1000, symbol_floating_loss=0, threshold_pct=10)


def test_single_symbol_stop_loss_triggers_at_10pct():
    assert _single_symbol_stop_loss_trigger(wallet_balance=1000, symbol_floating_loss=100, threshold_pct=10)


def test_single_symbol_stop_loss_not_triggered_below_10pct():
    assert not _single_symbol_stop_loss_trigger(wallet_balance=1000, symbol_floating_loss=99.99, threshold_pct=10)


def test_single_symbol_stop_loss_triggers_when_wallet_non_positive():
    assert _single_symbol_stop_loss_trigger(wallet_balance=0, symbol_floating_loss=1, threshold_pct=10)


def test_extract_usdt_wallet_balance_prefers_futures_wallet_fields():
    balance = {
        "free": {"USDT": 12},
        "total": {"USDT": 20},
        "USDT": {"free": 12, "total": 20},
        "info": {
            "availableBalance": "12",
            "totalWalletBalance": "248.5",
            "totalMarginBalance": "244.1",
        },
    }
    assert extract_usdt_wallet_balance(balance) == 248.5


def test_single_symbol_stop_loss_uses_wallet_not_small_available_balance():
    wallet_balance = extract_usdt_wallet_balance(
        {
            "free": {"USDT": 5},
            "total": {"USDT": 5},
            "info": {"totalWalletBalance": "248"},
        }
    )
    assert not _single_symbol_stop_loss_trigger(
        wallet_balance=wallet_balance,
        symbol_floating_loss=6,
        threshold_pct=10,
    )


def test_symbol_unrealized_pnl_from_exchange_matches_symbol_and_side():
    raw_positions = [
        {"symbol": "BTC/USDT:USDT", "side": "long", "contracts": 1, "unrealizedPnl": "-3.5"},
        {"symbol": "BTCUSDT", "side": "short", "contracts": 1, "unrealizedPnl": "2"},
        {"symbol": "ETHUSDT", "side": "long", "contracts": 1, "unrealizedPnl": "-10"},
    ]
    assert _symbol_unrealized_pnl_from_exchange(raw_positions, "BTCUSDT", "long") == -3.5

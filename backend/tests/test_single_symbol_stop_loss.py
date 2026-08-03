from app.services.binance_service import (
    extract_usdt_margin_balance,
    extract_usdt_pure_wallet_balance,
    extract_usdt_wallet_balance,
)
from app.services.exchange_factory import extract_margin_balance
from app.services.gate_service import extract_gate_usdt_margin_balance, extract_gate_usdt_wallet_balance
from app.services.position_manager import (
    _single_symbol_stop_loss_trigger,
    _symbol_unrealized_pnl_from_exchange,
)
from app.services.risk_manager import RiskManager


def test_single_symbol_stop_loss_not_triggered_without_loss():
    assert not _single_symbol_stop_loss_trigger(margin_balance=1000, symbol_floating_loss=0, threshold_pct=10)


def test_single_symbol_stop_loss_triggers_at_10pct():
    assert _single_symbol_stop_loss_trigger(margin_balance=1000, symbol_floating_loss=100, threshold_pct=10)


def test_single_symbol_stop_loss_not_triggered_below_10pct():
    assert not _single_symbol_stop_loss_trigger(margin_balance=1000, symbol_floating_loss=99.99, threshold_pct=10)


def test_single_symbol_stop_loss_skips_when_margin_non_positive():
    assert not _single_symbol_stop_loss_trigger(
        margin_balance=0,
        symbol_floating_loss=1,
        threshold_pct=10,
    )


def test_single_symbol_stop_loss_600_margin_boundary():
    assert not _single_symbol_stop_loss_trigger(
        margin_balance=600,
        symbol_floating_loss=59.99,
        threshold_pct=10,
    )
    assert _single_symbol_stop_loss_trigger(
        margin_balance=600,
        symbol_floating_loss=60,
        threshold_pct=10,
    )


def test_extract_usdt_wallet_balance_prefers_margin_equity_over_wallet():
    """浮亏时 margin < wallet；策略权益/曲线/止损必须取保证金余额。"""
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
    assert extract_usdt_wallet_balance(balance) == 244.1
    assert extract_usdt_pure_wallet_balance(balance) == 248.5
    assert extract_usdt_margin_balance(balance) == 244.1


def test_extract_usdt_margin_derives_from_wallet_plus_upnl():
    """缺 totalMarginBalance 时用钱包+未实现推导（204.45-37.16=167.29）。"""
    balance = {
        "info": {
            "totalWalletBalance": "204.45",
            "totalUnrealizedProfit": "-37.16",
        },
    }
    assert abs(extract_usdt_margin_balance(balance) - 167.29) < 1e-9


def test_extract_usdt_wallet_balance_falls_back_to_wallet_when_no_margin():
    balance = {
        "free": {"USDT": 5},
        "total": {"USDT": 5},
        "info": {"totalWalletBalance": "248"},
    }
    assert extract_usdt_wallet_balance(balance) == 248.0
    assert extract_usdt_pure_wallet_balance(balance) == 248.0
    assert extract_usdt_margin_balance(balance) == 248.0


def test_single_symbol_stop_loss_uses_margin_not_small_available_balance():
    margin_balance = extract_usdt_wallet_balance(
        {
            "free": {"USDT": 5},
            "total": {"USDT": 5},
            "info": {"totalWalletBalance": "248"},
        }
    )
    assert not _single_symbol_stop_loss_trigger(
        margin_balance=margin_balance,
        symbol_floating_loss=6,
        threshold_pct=10,
    )


def test_gate_stop_loss_denominator_uses_equity_not_wallet():
    """Gate：单币/保证金止损分母 = total+upnl，不能只用 total。"""
    bal = {
        "total": {"USDT": 300.0},
        "info": {"total": "300", "unrealised_pnl": "-40"},
    }
    assert extract_gate_usdt_wallet_balance(bal) == 300.0
    assert extract_gate_usdt_margin_balance(bal) == 260.0

    class _Gate:
        exchange_id = "gate"

    margin = extract_margin_balance(_Gate(), bal)
    assert margin == 260.0
    # 浮亏 26 = 权益 10% → 触发；若误用钱包 300 则不触发
    assert _single_symbol_stop_loss_trigger(
        margin_balance=margin, symbol_floating_loss=26, threshold_pct=10
    )
    assert not _single_symbol_stop_loss_trigger(
        margin_balance=300, symbol_floating_loss=26, threshold_pct=10
    )
    assert RiskManager().check_margin_threshold(margin, 270)
    assert not RiskManager().check_margin_threshold(300, 270)


def test_symbol_unrealized_pnl_from_exchange_matches_symbol_and_side():
    raw_positions = [
        {"symbol": "BTC/USDT:USDT", "side": "long", "contracts": 1, "unrealizedPnl": "-3.5"},
        {"symbol": "BTCUSDT", "side": "short", "contracts": 1, "unrealizedPnl": "2"},
        {"symbol": "ETHUSDT", "side": "long", "contracts": 1, "unrealizedPnl": "-10"},
    ]
    assert _symbol_unrealized_pnl_from_exchange(raw_positions, "BTCUSDT", "long") == -3.5

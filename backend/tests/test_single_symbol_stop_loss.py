from app.services.position_manager import _single_symbol_stop_loss_trigger


def test_single_symbol_stop_loss_not_triggered_without_loss():
    assert not _single_symbol_stop_loss_trigger(wallet_balance=1000, symbol_floating_loss=0, threshold_pct=10)


def test_single_symbol_stop_loss_triggers_at_10pct():
    assert _single_symbol_stop_loss_trigger(wallet_balance=1000, symbol_floating_loss=100, threshold_pct=10)


def test_single_symbol_stop_loss_not_triggered_below_10pct():
    assert not _single_symbol_stop_loss_trigger(wallet_balance=1000, symbol_floating_loss=99.99, threshold_pct=10)


def test_single_symbol_stop_loss_triggers_when_wallet_non_positive():
    assert _single_symbol_stop_loss_trigger(wallet_balance=0, symbol_floating_loss=1, threshold_pct=10)

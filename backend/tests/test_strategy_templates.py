from app.routers.strategy_templates import _strip_identity


def test_strip_identity_drops_name_account_keeps_direction():
    patch = {
        "account_id": 3,
        "name": "空接针",
        "direction": "short",
        "signal_source": "wick_spike",
        "take_profit_pct": 2.0,
    }
    out = _strip_identity(patch)
    assert "account_id" not in out
    assert "name" not in out
    assert out["direction"] == "short"
    assert out["signal_source"] == "wick_spike"
    assert out["take_profit_pct"] == 2.0

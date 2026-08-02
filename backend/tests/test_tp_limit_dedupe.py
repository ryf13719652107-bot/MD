"""币安止盈限价：绑单不强制 reduceOnly；匹配同腿防重复挂。"""

from app.services.position_manager import (
    PositionManager,
    _is_matching_tp_close_limit,
    _order_reduce_only_flag,
)


def test_binance_tp_matches_without_reduce_only():
    order = {
        "id": "1",
        "side": "sell",
        "type": "limit",
        "amount": 10,
        "positionSide": "LONG",
        "price": 100,
    }
    assert _is_matching_tp_close_limit(
        order,
        close_side="sell",
        ps_need="LONG",
        contracts=10,
        hedge_mode=True,
        exchange_id="binance",
    )


def test_binance_tp_rejects_wrong_position_side():
    order = {
        "id": "1",
        "side": "sell",
        "type": "limit",
        "amount": 10,
        "positionSide": "SHORT",
    }
    assert not _is_matching_tp_close_limit(
        order,
        close_side="sell",
        ps_need="LONG",
        contracts=10,
        hedge_mode=True,
        exchange_id="binance",
    )


def test_gate_tp_requires_reduce_only():
    bare = {
        "id": "1",
        "side": "sell",
        "type": "limit",
        "amount": 10,
        "positionSide": "LONG",
    }
    assert not _is_matching_tp_close_limit(
        bare,
        close_side="sell",
        ps_need="LONG",
        contracts=10,
        hedge_mode=True,
        exchange_id="gate",
    )
    ok = {**bare, "reduceOnly": True}
    assert _is_matching_tp_close_limit(
        ok,
        close_side="sell",
        ps_need="LONG",
        contracts=10,
        hedge_mode=True,
        exchange_id="gate",
    )


def test_order_reduce_only_flag_from_info():
    assert _order_reduce_only_flag({"info": {"is_reduce_only": True}}) is True
    assert _order_reduce_only_flag({"reduceOnly": False}) is False
    assert _order_reduce_only_flag({}) is None


def test_list_matching_finds_binance_order():
    pm = PositionManager()
    auth = type("A", (), {"exchange_id": "binance", "hedge_mode": True})()
    orders = [
        {
            "id": "tp1",
            "side": "buy",
            "type": "limit",
            "amount": 5.0,
            "positionSide": "SHORT",
            "price": 0.01,
        }
    ]
    hits = pm._list_matching_tp_close_limits(
        orders, position_side="short", contracts=5.0, auth_binance=auth
    )
    assert len(hits) == 1
    assert hits[0]["id"] == "tp1"


def test_qty_mismatch_rejected_unless_loose():
    order = {
        "id": "old",
        "side": "sell",
        "type": "limit",
        "amount": 5.0,
        "positionSide": "LONG",
    }
    assert not _is_matching_tp_close_limit(
        order,
        close_side="sell",
        ps_need="LONG",
        contracts=15.0,
        hedge_mode=True,
        exchange_id="binance",
        require_qty_match=True,
    )
    assert _is_matching_tp_close_limit(
        order,
        close_side="sell",
        ps_need="LONG",
        contracts=15.0,
        hedge_mode=True,
        exchange_id="binance",
        require_qty_match=False,
    )


def test_pick_best_tp_match_prefers_closer_qty():
    pm = PositionManager()
    a = {"id": "a", "amount": 5.0}
    b = {"id": "b", "amount": 14.8}  # within 2% of 15
    assert pm._pick_best_tp_match([a, b], 15.0)["id"] == "b"
    assert pm._tp_qty_ok(b, 15.0)
    assert not pm._tp_qty_ok(a, 15.0)

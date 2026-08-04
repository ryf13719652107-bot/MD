"""账户持仓流缓存单元测试。"""

from app.services.account_position_stream import AccountPositionStream


def test_leg_qty_fresh_and_stale():
    s = AccountPositionStream()
    s.set_leg(1, "BTCUSDT", "long", 0.5)
    assert s.is_fresh(1)
    assert s.leg_qty(1, "BTCUSDT", "long") == 0.5
    assert s.leg_qty(1, "BTCUSDT", "short") == 0.0

    # 强制过期
    s._updated_at[1] = 0.0
    assert not s.is_fresh(1)
    assert s.leg_qty(1, "BTCUSDT", "long") is None


def test_apply_local_fill_adds():
    s = AccountPositionStream()
    s.set_leg(2, "ETHUSDT", "short", 1.0)
    s.apply_local_fill(2, "ETHUSDT", "short", 0.25)
    assert abs(s.leg_qty(2, "ethusdt", "short") - 1.25) < 1e-9


def test_set_leg_zero_clears():
    s = AccountPositionStream()
    s.set_leg(3, "AAAUSDT", "long", 2.0)
    s.set_leg(3, "AAAUSDT", "long", 0.0)
    assert s.leg_qty(3, "AAAUSDT", "long") == 0.0


def test_empty_ingest_does_not_wipe_legs():
    s = AccountPositionStream()
    s.set_leg(4, "BTCUSDT", "long", 1.0)
    s._ingest_positions(4, [])
    assert s.is_fresh(4)
    assert s.leg_qty(4, "BTCUSDT", "long") == 1.0


def test_nonempty_ingest_replaces_snapshot():
    s = AccountPositionStream()
    s.set_leg(5, "BTCUSDT", "long", 1.0)
    s._ingest_positions(
        5,
        [
            {
                "symbol": "ETH/USDT:USDT",
                "side": "short",
                "contracts": 2.0,
            }
        ],
    )
    assert s.leg_qty(5, "BTCUSDT", "long") == 0.0
    assert s.leg_qty(5, "ETHUSDT", "short") == 2.0

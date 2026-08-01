"""收益曲线划转校正：充值/提现均不计入回报与盈亏；余额曲线用真实余额。"""
from datetime import datetime

from app.services.equity_cashflow import (
    build_adjusted_points,
    cashflow_external_id,
    hour_cashflow_window,
    window_deposit_withdraw,
)


def test_hour_cashflow_window_is_previous_hour():
    floor = datetime(2026, 8, 1, 22, 0, 0)
    start, end = hour_cashflow_window(floor)
    assert start == datetime(2026, 8, 1, 21, 0, 0)
    assert end == datetime(2026, 8, 1, 22, 0, 0)


def test_hours_to_sync_no_cursor_only_current():
    from app.services.equity_cashflow import _hours_to_sync

    floor = datetime(2026, 8, 1, 22, 0, 0)
    assert _hours_to_sync(floor, None) == [floor]


def test_hours_to_sync_catchup_missed():
    from app.services.equity_cashflow import _hours_to_sync, beijing_naive_to_ms

    floor = datetime(2026, 8, 1, 22, 0, 0)
    cursor = beijing_naive_to_ms(datetime(2026, 8, 1, 20, 0, 0))
    hours = _hours_to_sync(floor, cursor)
    assert hours == [
        datetime(2026, 8, 1, 21, 0, 0),
        datetime(2026, 8, 1, 22, 0, 0),
    ]


def test_transfer_in_does_not_change_adjusted():
    t0 = datetime(2026, 7, 1, 10, 0, 0)
    t1 = datetime(2026, 7, 1, 11, 0, 0)
    snaps = [(t0, 300.0), (t1, 350.0)]
    cfs = [(t1, 50.0)]
    pts = build_adjusted_points(snaps, cfs)
    assert pts[0][1] == 300.0
    assert pts[1][1] == 350.0  # 余额上涨
    assert pts[0][2] == 300.0
    assert pts[1][2] == 300.0  # 校正权益不变


def test_transfer_out_balance_drops_but_adjusted_flat():
    """提现：余额下降，回报用的校正权益不降。"""
    t0 = datetime(2026, 7, 1, 10, 0, 0)
    t1 = datetime(2026, 7, 1, 11, 0, 0)
    snaps = [(t0, 300.0), (t1, 200.0)]
    cfs = [(t1, -100.0)]
    pts = build_adjusted_points(snaps, cfs)
    assert pts[1][1] == 200.0  # 余额曲线下降
    assert pts[0][2] == 300.0
    assert pts[1][2] == 300.0  # 校正权益不变 → 收益率/盈亏不降


def test_trading_loss_still_affects_adjusted():
    t0 = datetime(2026, 7, 1, 10, 0, 0)
    t1 = datetime(2026, 7, 1, 11, 0, 0)
    snaps = [(t0, 300.0), (t1, 290.0)]
    cfs: list = []
    pts = build_adjusted_points(snaps, cfs)
    assert pts[1][2] == 290.0


def test_window_deposit_withdraw():
    t0 = datetime(2026, 7, 1, 0, 0, 0)
    t1 = datetime(2026, 7, 15, 0, 0, 0)
    t2 = datetime(2026, 8, 1, 0, 0, 0)
    cfs = [
        (t0, 100.0),
        (t1, -30.0),
        (t2, 50.0),
    ]
    start = datetime(2026, 7, 10, 0, 0, 0)
    dep, wdr = window_deposit_withdraw(cfs, start)
    assert dep == 50.0
    assert wdr == 30.0


def test_cashflow_external_id_prefers_tran_id():
    assert cashflow_external_id({"tranId": 99, "time": 1, "income": "1", "incomeType": "TRANSFER"}) == "tran:99"
    fb = cashflow_external_id(
        {"time": 1000, "income": "5.0", "incomeType": "TRANSFER", "asset": "USDT"}
    )
    assert fb.startswith("fb:")

"""收益曲线划转校正：充值/提现均不计入回报与盈亏；余额曲线用真实余额。"""
from datetime import datetime

from app.services.equity_cashflow import (
    build_adjusted_points,
    cashflow_external_id,
    cashflows_after,
    hour_cashflow_window,
    normalize_baseline_to_gross,
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


def test_hours_to_sync_mid_hour_seed_cursor():
    """建账 10:40：当前 10:00 整点不拉；11:00 会拉 [10:00,11:00)，建账前靠 set_at 过滤。"""
    from app.services.equity_cashflow import _hours_to_sync, beijing_naive_to_ms

    seed = beijing_naive_to_ms(datetime(2026, 8, 1, 10, 40, 0))
    assert _hours_to_sync(datetime(2026, 8, 1, 10, 0, 0), seed) == []
    assert _hours_to_sync(datetime(2026, 8, 1, 11, 0, 0), seed) == [
        datetime(2026, 8, 1, 11, 0, 0)
    ]


def test_cashflows_after_baseline_ignores_prior_deposit():
    t0 = datetime(2026, 8, 1, 10, 15, 0)
    t_set = datetime(2026, 8, 1, 10, 40, 0)
    t1 = datetime(2026, 8, 1, 12, 0, 0)
    cfs = [(t0, 1000.0), (t1, 50.0)]
    assert cashflows_after(cfs, t_set) == [(t1, 50.0)]
    snaps = [(t_set, 1000.0), (t1, 1050.0)]
    pts = build_adjusted_points(snaps, cashflows_after(cfs, t_set))
    assert pts[0][2] == 1000.0
    assert pts[1][2] == 1000.0


def test_post_seed_same_hour_deposit_counts_after_set_at():
    """建账后同小时再充值：必须计入校正，不能当盈利。"""
    t_set = datetime(2026, 8, 1, 10, 40, 0)
    t_dep = datetime(2026, 8, 1, 10, 50, 0)
    t1 = datetime(2026, 8, 1, 11, 0, 25)
    snaps = [(t_set, 1000.0), (t1, 1500.0)]
    cfs = cashflows_after([(t_dep, 500.0)], t_set)
    pts = build_adjusted_points(snaps, cfs)
    assert pts[0][2] == 1000.0
    assert pts[1][2] == 1000.0  # +500 充值被剔除


def test_normalize_old_adjusted_baseline_avoids_fake_thousands_pct():
    """旧基准=扣充提后的小数；新算法若不对齐会算出 +6000% 回报。"""
    t_set = datetime(2026, 8, 1, 10, 0, 0)
    cfs = [(datetime(2026, 8, 1, 9, 0, 0), 200.0)]
    healed, did = normalize_baseline_to_gross(
        3.0, set_at=t_set, cashflows=cfs, first_snap_total=203.0
    )
    assert did is True
    assert abs(healed - 203.0) < 1e-6
    # 新种子毛基准保持不变
    raw, did2 = normalize_baseline_to_gross(
        203.0, set_at=t_set, cashflows=cfs, first_snap_total=203.0
    )
    assert did2 is False
    assert abs(raw - 203.0) < 1e-6


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


def test_hour_floor_label_with_intrabar_deposit_looks_like_loss():
    """旧 bug：快照标整点、余额已含本小时充值 → 下一小时扣充值后校正骤降，隐式基准下收益率变负。"""
    t0 = datetime(2026, 8, 1, 10, 0, 0)  # 标签=整点，余额其实采自 10:25
    t_dep = datetime(2026, 8, 1, 10, 15, 0)
    t1 = datetime(2026, 8, 1, 11, 0, 0)
    snaps = [(t0, 1000.0), (t1, 1000.0)]
    cfs = [(t_dep, 1000.0)]
    pts = build_adjusted_points(snaps, cfs)
    assert pts[0][2] == 1000.0  # 充值时刻 > 标签，未扣
    assert pts[1][2] == 0.0
    baseline = pts[0][2]
    ret = (pts[1][2] - baseline) / baseline * 100.0
    assert ret == -100.0


def test_actual_snap_time_keeps_deposit_neutral():
    """修复后：快照用实际采样时刻，充值 ≤ 快照时刻，校正权益不抬高隐式基准。"""
    t_dep = datetime(2026, 8, 1, 10, 15, 0)
    t_snap = datetime(2026, 8, 1, 10, 25, 0)
    t1 = datetime(2026, 8, 1, 11, 0, 25)
    snaps = [(t_snap, 1000.0), (t1, 1000.0)]
    cfs = [(t_dep, 1000.0)]
    pts = build_adjusted_points(snaps, cfs)
    assert pts[0][2] == 0.0
    assert pts[1][2] == 0.0
    baseline = pts[0][2]
    # baseline≈0 时系列接口回报按 0 处理；此处校正权益保持平坦
    assert pts[-1][2] == baseline


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

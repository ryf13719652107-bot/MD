from pydantic import BaseModel


class EquityPointOut(BaseModel):
    t_unix: int
    total_usdt: float
    return_pct: float
    pnl_usdt: float


class EquitySummaryOut(BaseModel):
    """total_balance 为钱包余额；pnl/return/回撤基于校正权益（扣划转）。"""

    total_balance: float
    pnl_usdt: float
    return_pct: float
    max_drawdown_pct: float
    return_drawdown_ratio: float | None = None
    baseline_total_usdt: float
    baseline_set_at: str | None = None
    implicit_baseline: bool = False
    deposit_usdt: float = 0.0  # 窗口内划入合计
    withdraw_usdt: float = 0.0  # 窗口内划出绝对值合计


class EquitySeriesResponse(BaseModel):
    points: list[EquityPointOut]
    summary: EquitySummaryOut

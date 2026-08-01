from pydantic import BaseModel


class EquityPointOut(BaseModel):
    t_unix: int
    total_usdt: float
    return_pct: float
    pnl_usdt: float


class EquitySummaryOut(BaseModel):
    """total_balance 为真实余额；pnl/return/回撤剔除充值与提现。"""

    total_balance: float
    pnl_usdt: float
    return_pct: float
    max_drawdown_pct: float
    return_drawdown_ratio: float | None = None
    baseline_total_usdt: float
    baseline_set_at: str | None = None
    implicit_baseline: bool = False
    deposit_usdt: float = 0.0  # 窗口内划入合计（不计入盈亏）
    withdraw_usdt: float = 0.0  # 窗口内划出合计（不计入盈亏；余额曲线仍会下降）


class EquitySeriesResponse(BaseModel):
    points: list[EquityPointOut]
    summary: EquitySummaryOut

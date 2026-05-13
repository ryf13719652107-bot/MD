from pydantic import BaseModel


class EquityPointOut(BaseModel):
    t_unix: int
    total_usdt: float
    return_pct: float
    pnl_usdt: float


class EquitySummaryOut(BaseModel):
    """summary.total_balance 与仪表盘 total_balance 同源（当前 fetch 的 total.USDT）。"""

    total_balance: float
    pnl_usdt: float
    return_pct: float
    max_drawdown_pct: float
    return_drawdown_ratio: float | None = None
    baseline_total_usdt: float
    baseline_set_at: str | None = None
    implicit_baseline: bool = False


class EquitySeriesResponse(BaseModel):
    points: list[EquityPointOut]
    summary: EquitySummaryOut

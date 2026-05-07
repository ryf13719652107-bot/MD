from pydantic import BaseModel
from typing import Optional


class DashboardSnapshot(BaseModel):
    total_balance: float = 0.0
    available_balance: float = 0.0
    unrealized_pnl: float = 0.0
    unrealized_pnl_long: float = 0.0
    unrealized_pnl_short: float = 0.0
    daily_pnl: float = 0.0
    daily_pnl_long: float = 0.0
    daily_pnl_short: float = 0.0
    daily_pnl_pct: float = 0.0
    active_strategies: int = 0
    open_positions: int = 0
    daily_trades: int = 0
    win_rate_pct: float = 0.0
    leverage_multiplier: float = 0.0
    master_switch: bool = False
    account_name: str = ""
    balance_status: str = ""  # "ok", "no_account", "error"
    exchange_positions: list[dict] = []

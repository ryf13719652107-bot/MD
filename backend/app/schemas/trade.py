from pydantic import BaseModel
from datetime import datetime
from typing import Optional


class TradeResponse(BaseModel):
    id: int
    strategy_id: Optional[int]
    account_id: int
    symbol: str
    side: str
    quantity: float
    entry_price: float
    exit_price: float
    realized_pnl: float
    pnl_pct: float
    entry_time: datetime
    exit_time: datetime
    layer: int
    close_reason: str

    model_config = {"from_attributes": True}


class TradeListResponse(BaseModel):
    trades: list[TradeResponse]
    total: int

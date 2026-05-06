from pydantic import BaseModel
from datetime import datetime
from typing import Optional


class PositionResponse(BaseModel):
    id: int
    strategy_id: Optional[int]
    account_id: int
    symbol: str
    side: str
    quantity: float
    entry_price: float
    mark_price: Optional[float]
    unrealized_pnl: Optional[float]
    layer: int
    take_profit_price: Optional[float]
    exchange_order_id: Optional[str]
    tp_limit_order_id: Optional[str] = None
    opened_at: datetime
    closed_at: Optional[datetime]

    model_config = {"from_attributes": True}

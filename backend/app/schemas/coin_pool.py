from pydantic import BaseModel, Field
from datetime import datetime
from typing import Literal, Optional


class CoinPoolConfig(BaseModel):
    refresh_interval_seconds: int = Field(default=300, ge=30, le=3600)
    pool_source: Literal["gainers", "losers", "both"] = "both"
    max_symbols: int = Field(default=20, ge=5, le=50)


class CoinPoolResponse(BaseModel):
    id: int
    symbol: str
    rank: int
    price_change_pct: float
    volume_24h: Optional[float]
    source: str
    added_at: datetime
    last_updated: datetime

    model_config = {"from_attributes": True}

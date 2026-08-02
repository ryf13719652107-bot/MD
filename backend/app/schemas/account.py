from pydantic import BaseModel, Field, field_validator
from datetime import datetime


class AccountCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    api_key: str = Field(min_length=1)
    api_secret: str = Field(min_length=1)
    exchange: str = "binance"
    testnet: bool = True
    hedge_mode: bool = True

    @field_validator("exchange")
    @classmethod
    def normalize_exchange(cls, v: str) -> str:
        ex = (v or "binance").strip().lower()
        if ex not in ("binance", "gate"):
            raise ValueError("exchange 仅支持 binance 或 gate")
        return ex


class AccountResponse(BaseModel):
    id: int
    name: str
    exchange: str
    masked_key: str
    testnet: bool
    hedge_mode: bool
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}

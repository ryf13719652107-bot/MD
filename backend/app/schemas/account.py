from pydantic import BaseModel, Field
from datetime import datetime
from typing import Optional


class AccountCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    api_key: str = Field(min_length=1)
    api_secret: str = Field(min_length=1)
    testnet: bool = True
    hedge_mode: bool = True


class AccountResponse(BaseModel):
    id: int
    name: str
    masked_key: str
    testnet: bool
    hedge_mode: bool
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class StrategyParamTemplateCreate(BaseModel):
    name: str = Field(min_length=1, max_length=40)
    patch: dict[str, Any] = Field(default_factory=dict)


class StrategyParamTemplateUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=40)
    patch: dict[str, Any] | None = None


class StrategyParamTemplateResponse(BaseModel):
    id: int
    name: str
    patch: dict[str, Any]
    created_at: datetime
    updated_at: datetime

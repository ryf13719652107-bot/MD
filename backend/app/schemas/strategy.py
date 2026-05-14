from pydantic import BaseModel, Field
from datetime import datetime
from typing import Optional, Literal


class StrategyCreate(BaseModel):
    account_id: int
    name: str = Field(min_length=1, max_length=100)
    direction: Literal["long", "short"]
    symbol: Optional[str] = None  # None = use coin pool
    signal_source: Literal["rsi", "wavetrend"] = "wavetrend"
    rsi_period: int = Field(default=14, ge=5, le=50)
    timeframe: Literal["1m", "5m", "15m", "1h"] = "1m"
    margin_threshold: float = Field(default=0.0, ge=0)
    wt_channel_length: int = Field(default=10, ge=2, le=50)
    wt_average_length: int = Field(default=21, ge=2, le=100)
    wt_ob_level: float = Field(default=60.0, ge=0, le=100)
    wt_os_level: float = Field(default=-60.0, ge=-100, le=0)
    # Entry
    base_qty_type: Literal["margin_pct", "usdt"] = "margin_pct"
    base_qty_value: float = Field(default=6.0, gt=0)
    rsi_entry_threshold: float = Field(default=30.0, ge=0, le=100)
    # Martingale
    price_drop_pct: float = Field(default=30.0, gt=0, le=100)
    martingale_mult: float = Field(default=1.5, ge=1.0, le=10.0)
    max_layers: int = Field(default=8, ge=1, le=200)
    martingale_rsi_enabled: bool = False
    # Take profit
    take_profit_pct: float = Field(default=2.0, gt=0, le=50)
    take_profit_limit_order: bool = True
    # Stop loss
    stop_loss_enabled: bool = False
    stop_loss_pct: float = Field(default=5.0, gt=0, le=100)
    # Slippage protection
    slippage_pct: float = Field(default=0.5, ge=0, le=10)
    # Leverage
    leverage: int = Field(default=20, ge=1, le=125)
    # Coin pool
    use_coin_pool: bool = True
    coin_pool_source: Literal["gainers", "losers", "both"] = "gainers"
    coin_pool_refresh_seconds: int = Field(default=3600, ge=30, le=86400)
    coin_pool_fetch_mode: Literal["immediate", "interval"] = "interval"
    coin_pool_top_n: int = Field(default=20, ge=1, le=50)
    exclude_tradefi: bool = False


class StrategyUpdate(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=100)
    direction: Optional[Literal["long", "short"]] = None
    symbol: Optional[str] = None
    signal_source: Optional[Literal["rsi", "wavetrend"]] = None
    rsi_period: Optional[int] = Field(default=None, ge=5, le=50)
    timeframe: Optional[Literal["1m", "5m", "15m", "1h"]] = None
    margin_threshold: Optional[float] = Field(default=None, ge=0)
    wt_channel_length: Optional[int] = Field(default=None, ge=2, le=50)
    wt_average_length: Optional[int] = Field(default=None, ge=2, le=100)
    wt_ob_level: Optional[float] = Field(default=None, ge=0, le=100)
    wt_os_level: Optional[float] = Field(default=None, ge=-100, le=0)
    base_qty_type: Optional[Literal["margin_pct", "usdt"]] = None
    base_qty_value: Optional[float] = Field(default=None, gt=0)
    rsi_entry_threshold: Optional[float] = Field(default=None, ge=0, le=100)
    price_drop_pct: Optional[float] = Field(default=None, gt=0, le=100)
    martingale_mult: Optional[float] = Field(default=None, ge=1.0, le=10.0)
    max_layers: Optional[int] = Field(default=None, ge=1, le=200)
    martingale_rsi_enabled: Optional[bool] = None
    take_profit_pct: Optional[float] = Field(default=None, gt=0, le=50)
    take_profit_limit_order: Optional[bool] = None
    stop_loss_enabled: Optional[bool] = None
    stop_loss_pct: Optional[float] = Field(default=None, gt=0, le=100)
    slippage_pct: Optional[float] = Field(default=None, ge=0, le=10)
    leverage: Optional[int] = Field(default=None, ge=1, le=125)
    use_coin_pool: Optional[bool] = None
    coin_pool_source: Optional[Literal["gainers", "losers", "both"]] = None
    coin_pool_refresh_seconds: Optional[int] = Field(default=None, ge=30, le=86400)
    coin_pool_fetch_mode: Optional[Literal["immediate", "interval"]] = None
    coin_pool_top_n: Optional[int] = Field(default=None, ge=1, le=50)
    exclude_tradefi: Optional[bool] = None


class StrategyResponse(BaseModel):
    id: int
    account_id: int
    name: str
    direction: str
    symbol: Optional[str]
    signal_source: str
    rsi_period: int
    timeframe: str
    wt_channel_length: int
    wt_average_length: int
    wt_ob_level: float
    wt_os_level: float
    margin_threshold: float
    base_qty_type: str
    base_qty_value: float
    rsi_entry_threshold: float
    price_drop_pct: float
    martingale_mult: float
    max_layers: int
    martingale_rsi_enabled: bool
    take_profit_pct: float
    take_profit_limit_order: bool
    stop_loss_enabled: bool
    stop_loss_pct: float
    slippage_pct: float
    leverage: int
    use_coin_pool: bool
    coin_pool_source: str
    coin_pool_refresh_seconds: int
    coin_pool_fetch_mode: str
    coin_pool_top_n: int
    exclude_tradefi: bool
    status: str
    started_at: Optional[datetime] = None
    last_rsi: Optional[float] = None
    last_signal: Optional[str] = None
    last_signal_at: Optional[datetime] = None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}

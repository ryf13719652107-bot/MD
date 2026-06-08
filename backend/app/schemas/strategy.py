from pydantic import BaseModel, Field
from datetime import datetime
from typing import Optional, Literal


class StrategyCreate(BaseModel):
    account_id: int
    name: str = Field(min_length=1, max_length=100)
    direction: Literal["long", "short"]
    symbol: Optional[str] = None  # None = use coin pool
    signal_source: Literal["rsi", "wavetrend", "martingale_base"] = "wavetrend"
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
    price_drop_multiplier: float = Field(default=1.0, ge=1.0, le=5.0, description="加仓跌幅倍数，每层递增")
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
    leverage: int = Field(default=10, ge=1, le=125)
    # Coin pool
    use_coin_pool: bool = True
    coin_pool_source: Literal["gainers", "losers", "both"] = "gainers"
    coin_pool_refresh_seconds: int = Field(default=3600, ge=3600, le=86400)
    coin_pool_fetch_mode: Literal["interval", "scheduled"] = "interval"
    coin_pool_anchor_hour: int = Field(default=8, ge=0, le=23, description="北京时间整点，scheduled 模式首次开选")
    coin_pool_top_n: int = Field(default=20, ge=1, le=50)
    coin_pool_min_volume_24h: float = Field(
        default=0.0,
        ge=0,
        description="最低 24h 成交额(USDT)，0=不限制；仅过滤本策略可开仓的选币池币种",
    )
    exclude_tradefi: bool = True
    exclude_delisting: bool = Field(
        default=True,
        description="排除 14 天内下架或非 TRADING 的 USDT 永续",
    )
    exclude_mainstream: bool = Field(
        default=True,
        description="选币池模式下排除 BTC/ETH 等主流币；固定交易对不受限",
    )
    exclude_funding: bool = Field(
        default=False,
        description="选币池模式下按最近一次已结算资金费率过滤新开仓",
    )
    funding_rate_threshold_pct: float = Field(
        default=0.0,
        ge=-5.0,
        le=5.0,
        description="最近结算资金费率阈值(%): 做多>阈值、做空<阈值 时不开新仓",
    )


class StrategyUpdate(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=100)
    direction: Optional[Literal["long", "short"]] = None
    symbol: Optional[str] = None
    signal_source: Optional[Literal["rsi", "wavetrend", "martingale_base"]] = None
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
    price_drop_multiplier: Optional[float] = Field(default=None, ge=1.0, le=5.0)
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
    coin_pool_refresh_seconds: Optional[int] = Field(default=None, ge=3600, le=86400)
    coin_pool_fetch_mode: Optional[Literal["interval", "scheduled"]] = None
    coin_pool_anchor_hour: Optional[int] = Field(default=None, ge=0, le=23)
    coin_pool_top_n: Optional[int] = Field(default=None, ge=1, le=50)
    coin_pool_min_volume_24h: Optional[float] = Field(default=None, ge=0)
    exclude_tradefi: Optional[bool] = None
    exclude_delisting: Optional[bool] = None
    exclude_mainstream: Optional[bool] = None
    exclude_funding: Optional[bool] = None
    funding_rate_threshold_pct: Optional[float] = Field(default=None, ge=-5.0, le=5.0)


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
    price_drop_multiplier: float
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
    coin_pool_anchor_hour: int
    coin_pool_top_n: int
    coin_pool_min_volume_24h: float
    exclude_tradefi: bool
    exclude_delisting: bool
    exclude_mainstream: bool
    exclude_funding: bool
    funding_rate_threshold_pct: float
    status: str
    started_at: Optional[datetime] = None
    last_rsi: Optional[float] = None
    last_signal: Optional[str] = None
    last_signal_at: Optional[datetime] = None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}

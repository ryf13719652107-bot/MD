from pydantic import BaseModel, Field
from datetime import datetime
from typing import Optional, Literal


class StrategyCreate(BaseModel):
    account_id: int
    name: str = Field(min_length=1, max_length=100)
    direction: Literal["long", "short"]
    symbol: Optional[str] = None  # None = use coin pool
    signal_source: Literal["rsi", "wavetrend", "trend_wt", "martingale_base", "wick_spike"] = "wavetrend"
    rsi_period: int = Field(default=14, ge=5, le=50)
    timeframe: Literal["1m", "5m", "15m", "1h"] = "1m"
    margin_threshold: float = Field(default=0.0, ge=0)
    wt_channel_length: int = Field(default=10, ge=2, le=50)
    wt_average_length: int = Field(default=21, ge=2, le=100)
    wt_ob_level: float = Field(default=60.0, ge=0, le=100)
    wt_os_level: float = Field(default=-60.0, ge=-100, le=0)
    st_atr_period: int = Field(default=10, ge=1, le=100)
    st_factor: float = Field(default=3.0, ge=0.1, le=20.0)
    st_timeframe_1: Literal["5m", "15m", "30m", "1h", "4h"] = "15m"
    st_timeframe_2: Literal["5m", "15m", "30m", "1h", "4h"] = "30m"
    # Entry
    base_qty_type: Literal["margin_pct", "usdt"] = "margin_pct"
    base_qty_value: float = Field(default=6.0, gt=0)
    skip_min_qty_exceeds: bool = Field(
        default=True,
        description="交易所最小开仓名义大于意图名义时跳过该币",
    )
    rsi_entry_threshold: float = Field(default=30.0, ge=0, le=100)
    # Martingale
    price_drop_pct: float = Field(default=30.0, gt=0, le=100)
    price_drop_multiplier: float = Field(default=1.0, ge=1.0, le=5.0, description="?????????????????")
    martingale_mult: float = Field(default=1.5, ge=1.0, le=10.0)
    max_layers: int = Field(default=8, ge=1, le=200)
    martingale_rsi_enabled: bool = True
    martingale_st_filter_enabled: bool = False
    # Take profit
    take_profit_pct: float = Field(default=2.0, gt=0, le=50)
    take_profit_limit_order: bool = True
    # Stop loss
    stop_loss_enabled: bool = False
    stop_loss_pct: float = Field(default=5.0, gt=0, le=100)
    single_symbol_stop_loss_enabled: bool = False
    single_symbol_stop_loss_pct: float = Field(default=10.0, gt=0, le=100)
    # Slippage protection
    slippage_pct: float = Field(default=0.5, ge=0, le=10)
    # Leverage
    leverage: int = Field(default=10, ge=1, le=125)
    # Coin pool
    use_coin_pool: bool = True
    coin_pool_source: Literal["gainers", "losers", "both"] = "gainers"
    coin_pool_refresh_seconds: int = Field(default=3600, ge=600, le=86400)
    coin_pool_fetch_mode: Literal["immediate", "interval", "scheduled"] = "interval"
    coin_pool_anchor_hour: int = Field(default=8, ge=0, le=23)
    coin_pool_anchor_minute: int = Field(default=0, ge=0, le=59)
    coin_pool_top_n: int = Field(default=20, ge=1, le=50)
    coin_pool_min_volume_24h: float = Field(
        default=0.0,
        ge=0,
        description="??24h???(USDT)?0??????",
    )
    exclude_tradefi: bool = True
    exclude_delisting: bool = Field(
        default=True,
        description="??? 14 ????????? TRADING ??USDT ???",
    )
    exclude_mainstream: bool = Field(
        default=True,
        description="???????? BTC/ETH ?????",
    )
    exclude_funding: bool = Field(
        default=False,
        description="??????????????????????",
    )
    funding_rate_threshold_pct: float = Field(
        default=0.0,
        ge=-5.0,
        le=5.0,
        description="????????????????%): ???>?????????????????????",
    )
    # Wick spike
    wick_volume_mult: float = Field(default=8.0, ge=0, le=100)
    wick_volume_sma_period: int = Field(default=20, ge=2, le=200)
    wick_atr_period: int = Field(default=14, ge=2, le=100)
    wick_spike_atr_mult: float = Field(default=5.0, gt=0, le=50)
    wick_cooldown_sec: int = Field(default=0, ge=0, le=3600)
    wick_amp_vol_relax_enabled: bool = True
    wick_vol_relax_progress_start: float = Field(default=1.0, ge=0, le=10)
    wick_vol_relax_progress_full: float = Field(default=1.5, ge=0, le=10)
    wick_vol_relax_mult: float = Field(default=5.0, ge=0, le=100)
    wick_min_move_pct: float = Field(
        default=3.0,
        ge=0,
        le=50,
        description="本根相对开盘最小涨跌幅%；0=关闭",
    )
    wick_max_retrace_pct: float = Field(
        default=50.0,
        ge=0,
        le=100,
        description="开盘→极值回撤占比上限%；超过则跳过；0=关闭",
    )
    wick_arm_wait_sec: float = Field(
        default=12.0,
        ge=0,
        le=120,
        description="刺破后等量窗口秒数；0=关闭武装",
    )
    wick_arm_retrace_grace_sec: float = Field(
        default=3.0,
        ge=0,
        le=60,
        description="武装时量不够则确认前N秒免回撤",
    )
    wick_arm_grace_max_tip_gap_pct: float = Field(
        default=2.0,
        ge=0,
        le=20,
        description="grace免回撤时tip_gap%上限；0=不限制",
    )


class StrategyUpdate(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=100)
    direction: Optional[Literal["long", "short"]] = None
    symbol: Optional[str] = None
    signal_source: Optional[Literal["rsi", "wavetrend", "trend_wt", "martingale_base", "wick_spike"]] = None
    rsi_period: Optional[int] = Field(default=None, ge=5, le=50)
    timeframe: Optional[Literal["1m", "5m", "15m", "1h"]] = None
    margin_threshold: Optional[float] = Field(default=None, ge=0)
    wt_channel_length: Optional[int] = Field(default=None, ge=2, le=50)
    wt_average_length: Optional[int] = Field(default=None, ge=2, le=100)
    wt_ob_level: Optional[float] = Field(default=None, ge=0, le=100)
    wt_os_level: Optional[float] = Field(default=None, ge=-100, le=0)
    st_atr_period: Optional[int] = Field(default=None, ge=1, le=100)
    st_factor: Optional[float] = Field(default=None, ge=0.1, le=20.0)
    st_timeframe_1: Optional[Literal["5m", "15m", "30m", "1h", "4h"]] = None
    st_timeframe_2: Optional[Literal["5m", "15m", "30m", "1h", "4h"]] = None
    base_qty_type: Optional[Literal["margin_pct", "usdt"]] = None
    base_qty_value: Optional[float] = Field(default=None, gt=0)
    skip_min_qty_exceeds: Optional[bool] = None
    rsi_entry_threshold: Optional[float] = Field(default=None, ge=0, le=100)
    price_drop_pct: Optional[float] = Field(default=None, gt=0, le=100)
    price_drop_multiplier: Optional[float] = Field(default=None, ge=1.0, le=5.0)
    martingale_mult: Optional[float] = Field(default=None, ge=1.0, le=10.0)
    max_layers: Optional[int] = Field(default=None, ge=1, le=200)
    martingale_rsi_enabled: Optional[bool] = None
    martingale_st_filter_enabled: Optional[bool] = None
    take_profit_pct: Optional[float] = Field(default=None, gt=0, le=50)
    take_profit_limit_order: Optional[bool] = None
    stop_loss_enabled: Optional[bool] = None
    stop_loss_pct: Optional[float] = Field(default=None, gt=0, le=100)
    single_symbol_stop_loss_enabled: Optional[bool] = None
    single_symbol_stop_loss_pct: Optional[float] = Field(default=None, gt=0, le=100)
    slippage_pct: Optional[float] = Field(default=None, ge=0, le=10)
    leverage: Optional[int] = Field(default=None, ge=1, le=125)
    use_coin_pool: Optional[bool] = None
    coin_pool_source: Optional[Literal["gainers", "losers", "both"]] = None
    coin_pool_refresh_seconds: Optional[int] = Field(default=None, ge=600, le=86400)
    coin_pool_fetch_mode: Optional[Literal["immediate", "interval", "scheduled"]] = None
    coin_pool_anchor_hour: Optional[int] = Field(default=None, ge=0, le=23)
    coin_pool_anchor_minute: Optional[int] = Field(default=None, ge=0, le=59)
    coin_pool_top_n: Optional[int] = Field(default=None, ge=1, le=50)
    coin_pool_min_volume_24h: Optional[float] = Field(default=None, ge=0)
    exclude_tradefi: Optional[bool] = None
    exclude_delisting: Optional[bool] = None
    exclude_mainstream: Optional[bool] = None
    exclude_funding: Optional[bool] = None
    funding_rate_threshold_pct: Optional[float] = Field(default=None, ge=-5.0, le=5.0)
    wick_volume_mult: Optional[float] = Field(default=None, ge=0, le=100)
    wick_volume_sma_period: Optional[int] = Field(default=None, ge=2, le=200)
    wick_atr_period: Optional[int] = Field(default=None, ge=2, le=100)
    wick_spike_atr_mult: Optional[float] = Field(default=None, gt=0, le=50)
    wick_cooldown_sec: Optional[int] = Field(default=None, ge=0, le=3600)
    wick_amp_vol_relax_enabled: Optional[bool] = None
    wick_vol_relax_progress_start: Optional[float] = Field(default=None, ge=0, le=10)
    wick_vol_relax_progress_full: Optional[float] = Field(default=None, ge=0, le=10)
    wick_vol_relax_mult: Optional[float] = Field(default=None, ge=0, le=100)
    wick_min_move_pct: Optional[float] = Field(default=None, ge=0, le=50)
    wick_max_retrace_pct: Optional[float] = Field(default=None, ge=0, le=100)
    wick_arm_wait_sec: Optional[float] = Field(default=None, ge=0, le=120)
    wick_arm_retrace_grace_sec: Optional[float] = Field(default=None, ge=0, le=60)
    wick_arm_grace_max_tip_gap_pct: Optional[float] = Field(default=None, ge=0, le=20)


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
    st_atr_period: int = 10
    st_factor: float = 3.0
    st_timeframe_1: str = "15m"
    st_timeframe_2: str = "30m"
    margin_threshold: float
    base_qty_type: str
    base_qty_value: float
    skip_min_qty_exceeds: bool = True
    rsi_entry_threshold: float
    price_drop_pct: float
    price_drop_multiplier: float
    martingale_mult: float
    max_layers: int
    martingale_rsi_enabled: bool
    martingale_st_filter_enabled: bool = False
    take_profit_pct: float
    take_profit_limit_order: bool
    stop_loss_enabled: bool
    stop_loss_pct: float
    single_symbol_stop_loss_enabled: bool
    single_symbol_stop_loss_pct: float
    slippage_pct: float
    leverage: int
    use_coin_pool: bool
    coin_pool_source: str
    coin_pool_refresh_seconds: int
    coin_pool_fetch_mode: str
    coin_pool_anchor_hour: int
    coin_pool_anchor_minute: int
    coin_pool_top_n: int
    coin_pool_min_volume_24h: float
    exclude_tradefi: bool
    exclude_delisting: bool
    exclude_mainstream: bool
    exclude_funding: bool
    funding_rate_threshold_pct: float
    wick_volume_mult: float = 8.0
    wick_volume_sma_period: int = 20
    wick_atr_period: int = 14
    wick_spike_atr_mult: float = 5.0
    wick_cooldown_sec: int = 0
    wick_amp_vol_relax_enabled: bool = True
    wick_vol_relax_progress_start: float = 1.0
    wick_vol_relax_progress_full: float = 1.5
    wick_vol_relax_mult: float = 5.0
    wick_min_move_pct: float = 3.0
    wick_max_retrace_pct: float = 50.0
    wick_arm_wait_sec: float = 12.0
    wick_arm_retrace_grace_sec: float = 3.0
    wick_arm_grace_max_tip_gap_pct: float = 2.0
    blacklisted_symbols: list[str] = Field(default_factory=list)
    status: str
    started_at: Optional[datetime] = None
    last_rsi: Optional[float] = None
    last_signal: Optional[str] = None
    last_signal_at: Optional[datetime] = None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}






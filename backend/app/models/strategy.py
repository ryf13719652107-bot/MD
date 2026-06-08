import logging
import traceback
from datetime import datetime
from sqlalchemy import String, Float, Integer, Boolean, DateTime, ForeignKey, Index, event
from ..config import now_beijing
from sqlalchemy.orm import Mapped, mapped_column
from ..database import Base

logger = logging.getLogger(__name__)


class Strategy(Base):
    __tablename__ = "strategies"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    account_id: Mapped[int] = mapped_column(Integer, ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    direction: Mapped[str] = mapped_column(String(10), nullable=False)  # 'long' or 'short'
    symbol: Mapped[str] = mapped_column(String(50), nullable=True)  # NULL = use coin pool

    # Signal source
    signal_source: Mapped[str] = mapped_column(String(20), default="wavetrend", server_default="wavetrend")  # 'rsi' | 'wavetrend' | 'martingale_base'(不看指标，每根K线开盘开首单)

    # General params
    rsi_period: Mapped[int] = mapped_column(Integer, default=14)
    timeframe: Mapped[str] = mapped_column(String(10), default="1m")
    margin_threshold: Mapped[float] = mapped_column(Float, default=0.0)  # Auto-stop below this margin

    # WaveTrend params
    wt_channel_length: Mapped[int] = mapped_column(Integer, default=10, server_default="10")
    wt_average_length: Mapped[int] = mapped_column(Integer, default=21, server_default="21")
    wt_ob_level: Mapped[float] = mapped_column(Float, default=60.0, server_default="60.0")
    wt_os_level: Mapped[float] = mapped_column(Float, default=-60.0, server_default="-60.0")

    # Entry position params
    base_qty_type: Mapped[str] = mapped_column(String(20), default="margin_pct")  # 'margin_pct' or 'usdt'
    base_qty_value: Mapped[float] = mapped_column(Float, default=6.0)  # 6% margin or USDT amount
    rsi_entry_threshold: Mapped[float] = mapped_column(Float, default=30.0)  # long=30, short=75

    # Martingale params
    price_drop_pct: Mapped[float] = mapped_column(Float, default=30.0)
    price_drop_multiplier: Mapped[float] = mapped_column(Float, default=1.0)  # 加仓跌幅倍数：每层递增 price_drop_pct * multiplier^(layer-1)
    martingale_mult: Mapped[float] = mapped_column(Float, default=1.5)
    max_layers: Mapped[int] = mapped_column(Integer, default=8)
    martingale_rsi_enabled: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")  # Require RSI signal for adds

    # Take profit params
    take_profit_pct: Mapped[float] = mapped_column(Float, default=2.0)
    take_profit_limit_order: Mapped[bool] = mapped_column(Boolean, default=True)

    # Stop loss
    stop_loss_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    stop_loss_pct: Mapped[float] = mapped_column(Float, default=5.0)

    # Slippage protection
    slippage_pct: Mapped[float] = mapped_column(Float, default=0.5)  # Max slippage %, 0 = disabled

    # Leverage
    leverage: Mapped[int] = mapped_column(Integer, default=10)  # Contract leverage

    # Coin pool
    use_coin_pool: Mapped[bool] = mapped_column(Boolean, default=True)
    coin_pool_source: Mapped[str] = mapped_column(String(20), default="gainers")  # gainers, losers, both
    coin_pool_refresh_seconds: Mapped[int] = mapped_column(Integer, default=3600)  # how often to refresh coin pool
    coin_pool_fetch_mode: Mapped[str] = mapped_column(String(20), default="interval")  # 'immediate' | 'interval' | 'scheduled'
    coin_pool_anchor_hour: Mapped[int] = mapped_column(Integer, default=8, server_default="8")  # 北京时间整点，scheduled 模式首次开选
    coin_pool_top_n: Mapped[int] = mapped_column(Integer, default=20, server_default="20")
    # 最低 24h 成交额（USDT quoteVolume）；0 = 不限制。仅本策略过滤选币池新开仓列表
    coin_pool_min_volume_24h: Mapped[float] = mapped_column(Float, default=0.0, server_default="0")
    # True: 排除 TRADIFI_PERPETUAL + 黄金白银原油等非加密货币合约
    exclude_tradefi: Mapped[bool] = mapped_column(Boolean, default=True, server_default="1")
    # True: 排除 14 天内下架或非 TRADING 的合约（无仓时不开新单）
    exclude_delisting: Mapped[bool] = mapped_column(Boolean, default=True, server_default="1")
    # True: 选币池模式下排除 BTC/ETH 等主流币（固定交易对不受限）
    exclude_mainstream: Mapped[bool] = mapped_column(Boolean, default=True, server_default="1")
    # True: 选币池模式下按资金费率过滤新开仓（已有持仓仍管理）
    exclude_funding: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    # 最近结算资金费率阈值(%): 做多 rate>阈值 过滤；做空 rate<阈值 过滤；默认 0
    funding_rate_threshold_pct: Mapped[float] = mapped_column(Float, default=0.0, server_default="0")

    # Runtime state
    status: Mapped[str] = mapped_column(String(20), default="stopped")  # 'running', 'stopped', 'error'
    started_at: Mapped[datetime] = mapped_column(DateTime, nullable=True)
    last_rsi: Mapped[float] = mapped_column(Float, nullable=True)
    last_signal: Mapped[str] = mapped_column(String(20), nullable=True)  # 'long', 'short', 'neutral'
    last_signal_at: Mapped[datetime] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_beijing)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now_beijing, onupdate=now_beijing)


Index("idx_strategies_account", Strategy.account_id)
Index("idx_strategies_status", Strategy.status)


@event.listens_for(Strategy, "before_update")
def _track_status_change(mapper, connection, target):
    state = target._sa_instance_state
    hist = state.get_history("status", state.attrs.status.loaded_value)
    if hist.deleted and hist.deleted[0] != target.status:
        logger.warning(
            "STATUS CHANGE: strategy_id=%d '%s' -> '%s'\n%s",
            target.id, hist.deleted[0], target.status,
            "".join(traceback.format_stack()[-8:-1])
        )

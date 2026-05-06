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
    signal_source: Mapped[str] = mapped_column(String(20), default="rsi", server_default="rsi")  # 'rsi' or 'wavetrend'

    # General params
    rsi_period: Mapped[int] = mapped_column(Integer, default=14)
    timeframe: Mapped[str] = mapped_column(String(10), default="1m")
    margin_threshold: Mapped[float] = mapped_column(Float, default=0.0)  # Auto-stop below this margin

    # WaveTrend params
    wt_channel_length: Mapped[int] = mapped_column(Integer, default=10, server_default="10")
    wt_average_length: Mapped[int] = mapped_column(Integer, default=21, server_default="21")

    # Entry position params
    base_qty_type: Mapped[str] = mapped_column(String(20), default="margin_pct")  # 'margin_pct' or 'usdt'
    base_qty_value: Mapped[float] = mapped_column(Float, default=6.0)  # 6% margin or USDT amount
    rsi_entry_threshold: Mapped[float] = mapped_column(Float, default=30.0)  # long=30, short=75

    # Martingale params
    price_drop_pct: Mapped[float] = mapped_column(Float, default=30.0)
    martingale_mult: Mapped[float] = mapped_column(Float, default=1.5)
    max_layers: Mapped[int] = mapped_column(Integer, default=8)
    martingale_rsi_enabled: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")  # Require RSI signal for adds

    # Take profit params
    take_profit_pct: Mapped[float] = mapped_column(Float, default=2.0)
    take_profit_limit_order: Mapped[bool] = mapped_column(Boolean, default=False)

    # Stop loss
    stop_loss_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    stop_loss_pct: Mapped[float] = mapped_column(Float, default=5.0)

    # Slippage protection
    slippage_pct: Mapped[float] = mapped_column(Float, default=0.5)  # Max slippage %, 0 = disabled

    # Leverage
    leverage: Mapped[int] = mapped_column(Integer, default=20)  # Contract leverage

    # Coin pool
    use_coin_pool: Mapped[bool] = mapped_column(Boolean, default=True)
    coin_pool_source: Mapped[str] = mapped_column(String(20), default="both")  # 'gainers', 'losers', 'both'
    coin_pool_refresh_seconds: Mapped[int] = mapped_column(Integer, default=3600)  # how often to refresh coin pool
    coin_pool_fetch_mode: Mapped[str] = mapped_column(String(20), default="interval")  # 'immediate' or 'interval'
    coin_pool_top_n: Mapped[int] = mapped_column(Integer, default=20, server_default="20")

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

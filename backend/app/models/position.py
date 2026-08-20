from datetime import datetime
from sqlalchemy import String, Float, Integer, DateTime, ForeignKey, Index
from sqlalchemy.orm import Mapped, mapped_column
from ..database import Base
from ..config import now_beijing


class Position(Base):
    __tablename__ = "positions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    strategy_id: Mapped[int] = mapped_column(Integer, ForeignKey("strategies.id", ondelete="SET NULL"), nullable=True)
    account_id: Mapped[int] = mapped_column(Integer, ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False)
    symbol: Mapped[str] = mapped_column(String(50), nullable=False)
    side: Mapped[str] = mapped_column(String(10), nullable=False)  # 'long' or 'short'
    quantity: Mapped[float] = mapped_column(Float, nullable=False)
    entry_price: Mapped[float] = mapped_column(Float, nullable=False)
    mark_price: Mapped[float] = mapped_column(Float, nullable=True)
    unrealized_pnl: Mapped[float] = mapped_column(Float, default=0.0)
    layer: Mapped[int] = mapped_column(Integer, default=0)
    take_profit_price: Mapped[float] = mapped_column(Float, nullable=True)
    exchange_order_id: Mapped[str] = mapped_column(String(100), nullable=True)
    tp_limit_order_id: Mapped[str] = mapped_column(String(100), nullable=True)
    opened_at: Mapped[datetime] = mapped_column(DateTime, default=now_beijing)
    closed_at: Mapped[datetime] = mapped_column(DateTime, nullable=True)
    # 时间移动止盈状态（仅 strategy.trailing_tp_enabled=True 时使用）
    # None=未启用追踪；'armed'=开仓后窗口内等待激活；
    # 'active'=已激活毫秒级追踪；'expired'=窗口超时已回退限价止盈
    trailing_tp_state: Mapped[str] = mapped_column(String(16), nullable=True)
    # 入场后达到的最高盈利 %（用于回撤计算；激活后实时更新）
    trailing_tp_peak_pct: Mapped[float] = mapped_column(Float, nullable=True)


Index("idx_positions_open", Position.closed_at)
Index("idx_positions_strategy", Position.strategy_id)
Index("idx_positions_symbol", Position.symbol)

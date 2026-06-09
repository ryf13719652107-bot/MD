from sqlalchemy import Integer, String, DateTime, ForeignKey, UniqueConstraint, Index
from sqlalchemy.orm import Mapped, mapped_column

from ..config import now_beijing
from ..database import Base


class StrategySymbolBlacklist(Base):
    __tablename__ = "strategy_symbol_blacklist"
    __table_args__ = (
        UniqueConstraint("strategy_id", "symbol_norm", name="uq_strategy_symbol_blacklist"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    strategy_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("strategies.id", ondelete="CASCADE"),
        nullable=False,
    )
    symbol: Mapped[str] = mapped_column(String(50), nullable=False)
    symbol_norm: Mapped[str] = mapped_column(String(50), nullable=False)
    reason: Mapped[str] = mapped_column(String(50), default="single_symbol_stop_loss")
    created_at: Mapped[DateTime] = mapped_column(DateTime, default=now_beijing)


Index("idx_strategy_symbol_blacklist_strategy", StrategySymbolBlacklist.strategy_id)

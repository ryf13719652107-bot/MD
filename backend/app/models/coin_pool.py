from datetime import datetime
from sqlalchemy import String, Float, Integer, DateTime, Index
from sqlalchemy.orm import Mapped, mapped_column
from ..database import Base
from ..config import now_beijing


class CoinPool(Base):
    __tablename__ = "coin_pool"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(50), nullable=False, unique=True)
    rank: Mapped[int] = mapped_column(Integer, nullable=False)
    price_change_pct: Mapped[float] = mapped_column(Float, nullable=False)
    volume_24h: Mapped[float] = mapped_column(Float, nullable=True)
    source: Mapped[str] = mapped_column(String(20), nullable=False)  # 'gainers', 'losers'
    added_at: Mapped[datetime] = mapped_column(DateTime, default=now_beijing)
    last_updated: Mapped[datetime] = mapped_column(DateTime, default=now_beijing, onupdate=now_beijing)


Index("idx_coinpool_source", CoinPool.source)
Index("idx_coinpool_rank", CoinPool.rank)

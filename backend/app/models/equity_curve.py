from datetime import datetime

from sqlalchemy import Float, Integer, DateTime, ForeignKey, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from ..database import Base
from ..config import now_beijing


class AccountBalanceSnapshot(Base):
    """每小时一条：账户 total USDT（与仪表盘 fetch_balance total.USDT 一致）。"""

    __tablename__ = "account_balance_snapshots"
    __table_args__ = (
        UniqueConstraint("account_id", "snapshot_at", name="uq_account_balance_snapshot_hour"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    account_id: Mapped[int] = mapped_column(Integer, ForeignKey("accounts.id"), nullable=False, index=True)
    snapshot_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    total_usdt: Mapped[float] = mapped_column(Float, nullable=False)


class AccountEquityBaseline(Base):
    """用户「重置收益」后的基准余额。"""

    __tablename__ = "account_equity_baselines"

    account_id: Mapped[int] = mapped_column(Integer, ForeignKey("accounts.id"), primary_key=True)
    baseline_total_usdt: Mapped[float] = mapped_column(Float, nullable=False)
    set_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=now_beijing)

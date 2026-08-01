from datetime import datetime

from sqlalchemy import Float, Integer, DateTime, String, ForeignKey, UniqueConstraint
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
    account_id: Mapped[int] = mapped_column(Integer, ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False, index=True)
    snapshot_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    total_usdt: Mapped[float] = mapped_column(Float, nullable=False)


class AccountEquityBaseline(Base):
    """用户「重置收益」后的基准（校正权益 = 余额 − 累计净划转）。"""

    __tablename__ = "account_equity_baselines"

    account_id: Mapped[int] = mapped_column(Integer, ForeignKey("accounts.id", ondelete="CASCADE"), primary_key=True)
    baseline_total_usdt: Mapped[float] = mapped_column(Float, nullable=False)
    set_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=now_beijing)


class AccountCashflow(Base):
    """合约钱包外部资金流（划转入/出等），用于校正收益曲线。"""

    __tablename__ = "account_cashflows"
    __table_args__ = (
        UniqueConstraint("account_id", "external_id", name="uq_account_cashflow_ext"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    account_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    amount: Mapped[float] = mapped_column(Float, nullable=False)  # +划入 / −划出
    occurred_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    income_type: Mapped[str] = mapped_column(String(64), nullable=False, default="TRANSFER")
    asset: Mapped[str] = mapped_column(String(16), nullable=False, default="USDT")
    external_id: Mapped[str] = mapped_column(String(128), nullable=False)
    source: Mapped[str] = mapped_column(String(32), nullable=False, default="binance_income")
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=now_beijing)

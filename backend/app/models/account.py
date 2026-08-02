from datetime import datetime
from sqlalchemy import String, Float, Integer, Boolean, DateTime, Text, ForeignKey, Index
from sqlalchemy.orm import Mapped, mapped_column, relationship
from ..database import Base
from ..config import now_beijing


class Account(Base):
    __tablename__ = "accounts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    # binance | gate — 账户绑定交易所，交易与选币池均走该所
    exchange: Mapped[str] = mapped_column(String(20), nullable=False, default="binance", server_default="binance")
    api_key_encrypted: Mapped[str] = mapped_column(Text, nullable=False)
    api_secret_encrypted: Mapped[str] = mapped_column(Text, nullable=False)
    testnet: Mapped[bool] = mapped_column(Boolean, default=True)
    hedge_mode: Mapped[bool] = mapped_column(Boolean, default=True, server_default="1")
    # 币安 income 划转同步游标（毫秒，值为已成功同步小时窗的终点）；NULL=尚未同步，下次只拉当前前一小时
    cashflow_sync_cursor_ms: Mapped[int | None] = mapped_column(Integer, nullable=True, default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_beijing)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now_beijing, onupdate=now_beijing)

from datetime import datetime

from sqlalchemy import DateTime, Integer, String, Text, UniqueConstraint, Index
from sqlalchemy.orm import Mapped, mapped_column

from ..config import now_beijing
from ..database import Base


class StrategyParamTemplate(Base):
    """用户自建策略参数模板（不含名称/账户；含方向）。"""

    __tablename__ = "strategy_param_templates"
    __table_args__ = (
        UniqueConstraint("name", name="uq_strategy_param_template_name"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(40), nullable=False)
    payload: Mapped[str] = mapped_column(Text, nullable=False)  # JSON
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_beijing)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now_beijing, onupdate=now_beijing)


Index("idx_strategy_param_templates_updated", StrategyParamTemplate.updated_at)

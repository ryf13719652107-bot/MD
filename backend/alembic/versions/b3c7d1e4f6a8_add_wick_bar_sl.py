"""add_wick_bar_sl

Revision ID: b3c7d1e4f6a8
Revises: a9c1d2e3f4b5
Create Date: 2026-09-12 16:30:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "b3c7d1e4f6a8"
down_revision: Union[str, Sequence[str], None] = "a9c1d2e3f4b5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    conn = op.get_bind()
    insp = sa.inspect(conn)
    tables = set(insp.get_table_names())
    if "strategies" in tables:
        cols = [c["name"] for c in insp.get_columns("strategies")]
        if "wick_bar_sl_enabled" not in cols:
            op.add_column(
                "strategies",
                sa.Column(
                    "wick_bar_sl_enabled",
                    sa.Boolean(),
                    server_default="0",
                    nullable=False,
                ),
            )
    if "positions" in tables:
        cols = [c["name"] for c in insp.get_columns("positions")]
        if "sl_stop_order_id" not in cols:
            op.add_column(
                "positions",
                sa.Column("sl_stop_order_id", sa.String(100), nullable=True),
            )
        if "stop_loss_price" not in cols:
            op.add_column(
                "positions",
                sa.Column("stop_loss_price", sa.Float(), nullable=True),
            )


def downgrade() -> None:
    conn = op.get_bind()
    insp = sa.inspect(conn)
    tables = set(insp.get_table_names())
    if "positions" in tables:
        cols = [c["name"] for c in insp.get_columns("positions")]
        for c in ("stop_loss_price", "sl_stop_order_id"):
            if c in cols:
                op.drop_column("positions", c)
    if "strategies" in tables:
        cols = [c["name"] for c in insp.get_columns("strategies")]
        if "wick_bar_sl_enabled" in cols:
            op.drop_column("strategies", "wick_bar_sl_enabled")

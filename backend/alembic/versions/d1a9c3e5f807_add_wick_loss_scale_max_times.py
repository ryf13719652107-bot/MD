"""add wick loss scale max times

Revision ID: d1a9c3e5f807
Revises: c8e4f2a1b709
Create Date: 2026-09-13 19:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "d1a9c3e5f807"
down_revision: Union[str, Sequence[str], None] = "c8e4f2a1b709"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    conn = op.get_bind()
    insp = sa.inspect(conn)
    tables = set(insp.get_table_names())
    if "strategies" not in tables:
        return
    cols = [c["name"] for c in insp.get_columns("strategies")]
    if "wick_loss_scale_max_times" not in cols:
        op.add_column(
            "strategies",
            sa.Column(
                "wick_loss_scale_max_times",
                sa.Integer(),
                server_default="2",
                nullable=False,
            ),
        )


def downgrade() -> None:
    conn = op.get_bind()
    insp = sa.inspect(conn)
    tables = set(insp.get_table_names())
    if "strategies" not in tables:
        return
    cols = [c["name"] for c in insp.get_columns("strategies")]
    if "wick_loss_scale_max_times" in cols:
        op.drop_column("strategies", "wick_loss_scale_max_times")

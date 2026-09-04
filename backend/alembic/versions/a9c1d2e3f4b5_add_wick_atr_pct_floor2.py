"""add_wick_atr_pct_floor2

Revision ID: a9c1d2e3f4b5
Revises: f1a2b3c4d5e6
Create Date: 2026-09-04 23:20:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "a9c1d2e3f4b5"
down_revision: Union[str, Sequence[str], None] = "f1a2b3c4d5e6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    conn = op.get_bind()
    insp = sa.inspect(conn)
    if "strategies" not in insp.get_table_names():
        return
    cols = [c["name"] for c in insp.get_columns("strategies")]
    if "wick_atr_pct_floor2" not in cols:
        op.add_column(
            "strategies",
            sa.Column(
                "wick_atr_pct_floor2",
                sa.Float(),
                server_default="0.25",
                nullable=False,
            ),
        )
    if "wick_atr_quiet_mult2" not in cols:
        op.add_column(
            "strategies",
            sa.Column(
                "wick_atr_quiet_mult2",
                sa.Float(),
                server_default="3.0",
                nullable=False,
            ),
        )


def downgrade() -> None:
    conn = op.get_bind()
    insp = sa.inspect(conn)
    if "strategies" not in insp.get_table_names():
        return
    cols = [c["name"] for c in insp.get_columns("strategies")]
    for c in ("wick_atr_quiet_mult2", "wick_atr_pct_floor2"):
        if c in cols:
            op.drop_column("strategies", c)

"""add_wick_atr_pct_floor

Revision ID: f1a2b3c4d5e6
Revises: a7c3e9f1b2d4
Create Date: 2026-09-04 23:10:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "f1a2b3c4d5e6"
down_revision: Union[str, Sequence[str], None] = "a7c3e9f1b2d4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    conn = op.get_bind()
    insp = sa.inspect(conn)
    if "strategies" not in insp.get_table_names():
        return
    cols = [c["name"] for c in insp.get_columns("strategies")]
    if "wick_atr_pct_floor_enabled" not in cols:
        op.add_column(
            "strategies",
            sa.Column(
                "wick_atr_pct_floor_enabled",
                sa.Boolean(),
                server_default="0",
                nullable=False,
            ),
        )
    if "wick_atr_pct_floor" not in cols:
        op.add_column(
            "strategies",
            sa.Column(
                "wick_atr_pct_floor",
                sa.Float(),
                server_default="0.5",
                nullable=False,
            ),
        )
    if "wick_atr_quiet_mult" not in cols:
        op.add_column(
            "strategies",
            sa.Column(
                "wick_atr_quiet_mult",
                sa.Float(),
                server_default="2.0",
                nullable=False,
            ),
        )


def downgrade() -> None:
    conn = op.get_bind()
    insp = sa.inspect(conn)
    if "strategies" not in insp.get_table_names():
        return
    cols = [c["name"] for c in insp.get_columns("strategies")]
    for c in (
        "wick_atr_quiet_mult",
        "wick_atr_pct_floor",
        "wick_atr_pct_floor_enabled",
    ):
        if c in cols:
            op.drop_column("strategies", c)

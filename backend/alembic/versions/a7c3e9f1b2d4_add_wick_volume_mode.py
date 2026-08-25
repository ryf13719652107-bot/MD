"""add_wick_volume_mode

Revision ID: a7c3e9f1b2d4
Revises: e8f3a1b7c2d4
Create Date: 2026-08-24 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "a7c3e9f1b2d4"
down_revision: Union[str, Sequence[str], None] = "e8f3a1b7c2d4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    conn = op.get_bind()
    insp = sa.inspect(conn)
    if "strategies" in insp.get_table_names():
        cols = [c["name"] for c in insp.get_columns("strategies")]
        if "wick_volume_mode" not in cols:
            op.add_column(
                "strategies",
                sa.Column(
                    "wick_volume_mode",
                    sa.String(32),
                    server_default="original",
                    nullable=False,
                ),
            )
        if "wick_instant_active_until_pct" not in cols:
            op.add_column(
                "strategies",
                sa.Column(
                    "wick_instant_active_until_pct",
                    sa.Float(),
                    server_default="0.5",
                    nullable=False,
                ),
            )


def downgrade() -> None:
    conn = op.get_bind()
    insp = sa.inspect(conn)
    if "strategies" in insp.get_table_names():
        cols = [c["name"] for c in insp.get_columns("strategies")]
        for c in ("wick_instant_active_until_pct", "wick_volume_mode"):
            if c in cols:
                op.drop_column("strategies", c)

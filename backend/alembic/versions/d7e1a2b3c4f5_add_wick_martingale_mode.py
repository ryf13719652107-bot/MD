"""add_wick_martingale_mode

Revision ID: d7e1a2b3c4f5
Revises: c4a8b1e2f903
Create Date: 2026-08-07 09:45:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "d7e1a2b3c4f5"
down_revision: Union[str, Sequence[str], None] = "c4a8b1e2f903"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    conn = op.get_bind()
    insp = sa.inspect(conn)
    if "strategies" in insp.get_table_names():
        cols = [c["name"] for c in insp.get_columns("strategies")]
        if "wick_martingale_mode" not in cols:
            op.add_column(
                "strategies",
                sa.Column(
                    "wick_martingale_mode",
                    sa.String(32),
                    server_default="price_and_wt",
                    nullable=False,
                ),
            )


def downgrade() -> None:
    op.drop_column("strategies", "wick_martingale_mode")

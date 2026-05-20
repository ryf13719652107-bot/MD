"""add_coin_pool_min_volume_24h

Revision ID: b2c8e4f1a903
Revises: 96fcf209e656
Create Date: 2026-05-20

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "b2c8e4f1a903"
down_revision: Union[str, Sequence[str], None] = "96fcf209e656"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    conn = op.get_bind()
    insp = sa.inspect(conn)
    if "strategies" in insp.get_table_names():
        cols = [c["name"] for c in insp.get_columns("strategies")]
        if "coin_pool_min_volume_24h" not in cols:
            op.add_column(
                "strategies",
                sa.Column(
                    "coin_pool_min_volume_24h",
                    sa.Float(),
                    nullable=False,
                    server_default="0",
                ),
            )


def downgrade() -> None:
    op.drop_column("strategies", "coin_pool_min_volume_24h")

"""add wick same-symbol loss scale

Revision ID: c8e4f2a1b709
Revises: b3c7d1e4f6a8
Create Date: 2026-09-13 08:10:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "c8e4f2a1b709"
down_revision: Union[str, Sequence[str], None] = "b3c7d1e4f6a8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    conn = op.get_bind()
    insp = sa.inspect(conn)
    tables = set(insp.get_table_names())
    if "strategies" not in tables:
        return
    cols = [c["name"] for c in insp.get_columns("strategies")]
    if "wick_reopen_after_sl_enabled" not in cols:
        op.add_column(
            "strategies",
            sa.Column(
                "wick_reopen_after_sl_enabled",
                sa.Boolean(),
                server_default="0",
                nullable=False,
            ),
        )
    if "wick_loss_scale_enabled" not in cols:
        op.add_column(
            "strategies",
            sa.Column(
                "wick_loss_scale_enabled",
                sa.Boolean(),
                server_default="0",
                nullable=False,
            ),
        )
    if "wick_loss_scale_base" not in cols:
        op.add_column(
            "strategies",
            sa.Column(
                "wick_loss_scale_base",
                sa.Float(),
                server_default="2.0",
                nullable=False,
            ),
        )
    if "wick_loss_scale_max_mult" not in cols:
        op.add_column(
            "strategies",
            sa.Column(
                "wick_loss_scale_max_mult",
                sa.Float(),
                server_default="8.0",
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
    for name in (
        "wick_loss_scale_max_mult",
        "wick_loss_scale_base",
        "wick_loss_scale_enabled",
        "wick_reopen_after_sl_enabled",
    ):
        if name in cols:
            op.drop_column("strategies", name)

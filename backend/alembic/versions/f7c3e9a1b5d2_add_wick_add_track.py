"""add_wick_add_track

Revision ID: f7c3e9a1b5d2
Revises: d1a9c3e5f807
Create Date: 2026-10-19 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "f7c3e9a1b5d2"
down_revision: Union[str, Sequence[str], None] = "d1a9c3e5f807"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    conn = op.get_bind()
    insp = sa.inspect(conn)

    if "strategies" in insp.get_table_names():
        cols = [c["name"] for c in insp.get_columns("strategies")]
        if "wick_add_track_enabled" not in cols:
            op.add_column(
                "strategies",
                sa.Column(
                    "wick_add_track_enabled",
                    sa.Boolean(),
                    server_default="0",
                    nullable=False,
                ),
            )
        if "wick_add_track_threshold_mult" not in cols:
            op.add_column(
                "strategies",
                sa.Column(
                    "wick_add_track_threshold_mult",
                    sa.Float(),
                    server_default="2.0",
                    nullable=False,
                ),
            )
        if "wick_add_track_qty_mult" not in cols:
            op.add_column(
                "strategies",
                sa.Column(
                    "wick_add_track_qty_mult",
                    sa.Float(),
                    server_default="1.5",
                    nullable=False,
                ),
            )
        if "wick_add_track_min_depth_pct" not in cols:
            op.add_column(
                "strategies",
                sa.Column(
                    "wick_add_track_min_depth_pct",
                    sa.Float(),
                    server_default="0.5",
                    nullable=False,
                ),
            )

    if "positions" in insp.get_table_names():
        cols = [c["name"] for c in insp.get_columns("positions")]
        if "entry_bar_open" not in cols:
            op.add_column(
                "positions",
                sa.Column(
                    "entry_bar_open",
                    sa.Float(),
                    nullable=True,
                ),
            )


def downgrade() -> None:
    conn = op.get_bind()
    insp = sa.inspect(conn)

    if "positions" in insp.get_table_names():
        cols = [c["name"] for c in insp.get_columns("positions")]
        if "entry_bar_open" in cols:
            op.drop_column("positions", "entry_bar_open")

    if "strategies" in insp.get_table_names():
        cols = [c["name"] for c in insp.get_columns("strategies")]
        for c in (
            "wick_add_track_min_depth_pct",
            "wick_add_track_qty_mult",
            "wick_add_track_threshold_mult",
            "wick_add_track_enabled",
        ):
            if c in cols:
                op.drop_column("strategies", c)

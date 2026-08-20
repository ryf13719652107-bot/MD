"""add_trailing_tp

Revision ID: e8f3a1b7c2d4
Revises: d7e1a2b3c4f5
Create Date: 2026-08-20 21:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "e8f3a1b7c2d4"
down_revision: Union[str, Sequence[str], None] = "d7e1a2b3c4f5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    conn = op.get_bind()
    insp = sa.inspect(conn)

    if "strategies" in insp.get_table_names():
        cols = [c["name"] for c in insp.get_columns("strategies")]
        if "trailing_tp_enabled" not in cols:
            op.add_column(
                "strategies",
                sa.Column(
                    "trailing_tp_enabled",
                    sa.Boolean(),
                    server_default="0",
                    nullable=False,
                ),
            )
        if "trailing_tp_window_sec" not in cols:
            op.add_column(
                "strategies",
                sa.Column(
                    "trailing_tp_window_sec",
                    sa.Float(),
                    server_default="300.0",
                    nullable=False,
                ),
            )
        if "trailing_tp_drawdown_base_pct" not in cols:
            op.add_column(
                "strategies",
                sa.Column(
                    "trailing_tp_drawdown_base_pct",
                    sa.Float(),
                    server_default="30.0",
                    nullable=False,
                ),
            )
        if "trailing_tp_drawdown_tier1_pct" not in cols:
            op.add_column(
                "strategies",
                sa.Column(
                    "trailing_tp_drawdown_tier1_pct",
                    sa.Float(),
                    server_default="20.0",
                    nullable=False,
                ),
            )
        if "trailing_tp_drawdown_tier2_pct" not in cols:
            op.add_column(
                "strategies",
                sa.Column(
                    "trailing_tp_drawdown_tier2_pct",
                    sa.Float(),
                    server_default="15.0",
                    nullable=False,
                ),
            )
        if "trailing_tp_tier1_threshold" not in cols:
            op.add_column(
                "strategies",
                sa.Column(
                    "trailing_tp_tier1_threshold",
                    sa.Float(),
                    server_default="2.5",
                    nullable=False,
                ),
            )
        if "trailing_tp_tier2_threshold" not in cols:
            op.add_column(
                "strategies",
                sa.Column(
                    "trailing_tp_tier2_threshold",
                    sa.Float(),
                    server_default="5.0",
                    nullable=False,
                ),
            )

    if "positions" in insp.get_table_names():
        cols = [c["name"] for c in insp.get_columns("positions")]
        if "trailing_tp_state" not in cols:
            op.add_column(
                "positions",
                sa.Column(
                    "trailing_tp_state",
                    sa.String(16),
                    nullable=True,
                ),
            )
        if "trailing_tp_peak_pct" not in cols:
            op.add_column(
                "positions",
                sa.Column(
                    "trailing_tp_peak_pct",
                    sa.Float(),
                    nullable=True,
                ),
            )


def downgrade() -> None:
    conn = op.get_bind()
    insp = sa.inspect(conn)

    if "positions" in insp.get_table_names():
        cols = [c["name"] for c in insp.get_columns("positions")]
        if "trailing_tp_peak_pct" in cols:
            op.drop_column("positions", "trailing_tp_peak_pct")
        if "trailing_tp_state" in cols:
            op.drop_column("positions", "trailing_tp_state")

    if "strategies" in insp.get_table_names():
        cols = [c["name"] for c in insp.get_columns("strategies")]
        for c in (
            "trailing_tp_tier2_threshold",
            "trailing_tp_tier1_threshold",
            "trailing_tp_drawdown_tier2_pct",
            "trailing_tp_drawdown_tier1_pct",
            "trailing_tp_drawdown_base_pct",
            "trailing_tp_window_sec",
            "trailing_tp_enabled",
        ):
            if c in cols:
                op.drop_column("strategies", c)

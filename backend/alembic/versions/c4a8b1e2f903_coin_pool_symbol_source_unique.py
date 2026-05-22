"""coin_pool: unique(symbol) -> unique(symbol, source)

Revision ID: c4a8b1e2f903
Revises: b2c8e4f1a903
Create Date: 2026-05-20

"""
from typing import Sequence, Union

from alembic import op
from sqlalchemy import inspect


revision: str = "c4a8b1e2f903"
down_revision: Union[str, Sequence[str], None] = "b2c8e4f1a903"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    conn = op.get_bind()
    if "coin_pool" not in inspect(conn).get_table_names():
        return
    from app.db_migrations.coin_pool_unique import migrate_coin_pool_symbol_source_unique

    migrate_coin_pool_symbol_source_unique(conn)


def downgrade() -> None:
    pass

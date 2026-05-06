"""initial

Revision ID: 8f805466b71c
Revises:
Create Date: 2026-05-03 19:26:08.688288

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


# revision identifiers, used by Alembic.
revision: str = '8f805466b71c'
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    conn = op.get_bind()
    insp = inspect(conn)
    tables = insp.get_table_names()

    # Fresh DB: create all tables from SQLAlchemy metadata
    if 'strategies' not in tables:
        import sys, os
        backend_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
        sys.path.insert(0, backend_dir)
        from app.database import Base
        Base.metadata.create_all(bind=conn)
        return

    # Existing DB: migrate from pre-Alembic state
    columns = [c['name'] for c in insp.get_columns('strategies')]
    if 'run_interval_seconds' in columns:
        op.drop_column('strategies', 'run_interval_seconds')


def downgrade() -> None:
    """Downgrade schema."""
    pass

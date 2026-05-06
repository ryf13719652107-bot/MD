"""initial

Revision ID: 8f805466b71c
Revises: 
Create Date: 2026-05-03 19:26:08.688288

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '8f805466b71c'
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # If strategies table doesn't exist (fresh DB), skip — Base.metadata.create_all will handle it
    conn = op.get_bind()
    insp = sa.inspect(conn)
    if 'strategies' in insp.get_table_names():
        columns = [c['name'] for c in insp.get_columns('strategies')]
        if 'run_interval_seconds' in columns:
            op.drop_column('strategies', 'run_interval_seconds')


def downgrade() -> None:
    """Downgrade schema."""
    pass

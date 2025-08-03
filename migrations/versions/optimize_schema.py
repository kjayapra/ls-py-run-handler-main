"""optimize schema with indexes and improved types

Revision ID: optimize_schema
Revises: 26a9efc758a0
Create Date: 2025-08-03 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'optimize_schema'
down_revision: Union[str, None] = '26a9efc758a0'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema with optimizations."""
    # Add indexes for common queries
    op.execute("CREATE INDEX IF NOT EXISTS idx_runs_trace_id ON runs(trace_id);")
    op.execute("CREATE INDEX IF NOT EXISTS idx_runs_name ON runs(name);")
    op.execute("CREATE INDEX IF NOT EXISTS idx_runs_created_at ON runs(id);")
    
    # Add a timestamp column for better query capabilities
    op.execute("""
    ALTER TABLE runs ADD COLUMN IF NOT EXISTS created_at TIMESTAMP DEFAULT NOW();
    """)
    
    # Add a size tracking column for cache optimization
    op.execute("""
    ALTER TABLE runs ADD COLUMN IF NOT EXISTS total_size INTEGER DEFAULT 0;
    """)


def downgrade() -> None:
    """Downgrade schema optimizations."""
    op.execute("DROP INDEX IF EXISTS idx_runs_trace_id;")
    op.execute("DROP INDEX IF EXISTS idx_runs_name;")
    op.execute("DROP INDEX IF EXISTS idx_runs_created_at;")
    op.execute("ALTER TABLE runs DROP COLUMN IF EXISTS created_at;")
    op.execute("ALTER TABLE runs DROP COLUMN IF EXISTS total_size;")
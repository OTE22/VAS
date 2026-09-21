"""Explicitly distinguish generated alert names from custom titles."""
from alembic import op
import sqlalchemy as sa

revision = 'ff17b8c9d0e1'
down_revision = 'ff06a7b8c9d0'
branch_labels = depends_on = None


def upgrade():
    op.execute("SET LOCAL lock_timeout = '5s'")
    # Legacy titles have no provenance: keep them custom unless explicitly
    # confirmed as generated. Never guess from a title's wording.
    op.add_column('live_search_alerts', sa.Column('auto_name', sa.Boolean(),
                  nullable=False, server_default=sa.false()))


def downgrade():
    op.drop_column('live_search_alerts', 'auto_name')

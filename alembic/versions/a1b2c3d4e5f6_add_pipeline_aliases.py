"""Add pipeline_aliases table

When a pipeline is renamed (e.g. an auto-registered UUID given a friendly name),
an alias row maps the old id to the new one so future webhooks that still use
the old id automatically land in the renamed pipeline instead of re-creating it.

Revision ID: a1b2c3d4e5f6
Revises: 4e4e7b140f5d
Create Date: 2026-07-21
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'a1b2c3d4e5f6'
down_revision: Union[str, None] = '4e4e7b140f5d'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'pipeline_aliases',
        sa.Column('old_pipeline_id', sa.String(length=255), primary_key=True),
        sa.Column('new_pipeline_id', sa.String(length=255), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
    )
    op.create_index('idx_pipeline_aliases_new', 'pipeline_aliases', ['new_pipeline_id'])


def downgrade() -> None:
    op.drop_index('idx_pipeline_aliases_new', table_name='pipeline_aliases')
    op.drop_table('pipeline_aliases')

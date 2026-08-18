"""Audit metadata: identity_images.source_type.

Additive and nullable. Records how a photo entered the system —
'upload' (full photo), 'cropped_face' (is_face_image=true, pre-cropped), or
'promotion' (copied in when an unknown identity was promoted). NULL is the
honest value for rows created before this column existed; nothing is
backfilled with a guess.

Revision ID: e1f2a3b4c5d6
Revises: d0e1f2a3b4c5
Create Date: 2026-08-01
"""
from alembic import op
import sqlalchemy as sa

revision = 'e1f2a3b4c5d6'
down_revision = 'd0e1f2a3b4c5'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('identity_images',
                  sa.Column('source_type', sa.String(length=32), nullable=True))
    op.create_index('idx_identity_image_source_type', 'identity_images', ['source_type'])


def downgrade() -> None:
    op.drop_index('idx_identity_image_source_type', table_name='identity_images')
    op.drop_column('identity_images', 'source_type')

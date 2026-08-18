"""Multi-image enrollment: identity_images + nullable identity_embeddings.image_id.

Additive and backward compatible.

  * Creates identity_images (one Identity -> many images).
  * Adds a NULLABLE image_id FK to identity_embeddings. Every pre-existing
    embedding keeps image_id NULL, which is a legitimate state: embeddings
    produced from live detections (rather than an upload) have no source
    enrollment image, and rows predating this table cannot be attributed to
    one without guessing.
  * UNIQUE(identity_id, file_checksum) makes re-uploading the same photo for
    the same person idempotent.
  * A PARTIAL unique index enforces at most one primary image per identity in
    the database, not merely by convention.

No existing file is moved, renamed or deleted. Folders already laid out by
display name keep working; only NEW enrollments use FACES_DIR/<identity_uuid>/.
Backfill from identities.best_snapshot_path is OPTIONAL and lives in a
separate command (scripts/backfill_identity_images.py) so this migration
stays pure DDL.

Revision ID: d0e1f2a3b4c5
Revises: b8c9d0e1f2a3
Create Date: 2026-08-01
"""
from alembic import op
import sqlalchemy as sa

revision = 'd0e1f2a3b4c5'
down_revision = 'b8c9d0e1f2a3'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'identity_images',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('identity_id', sa.UUID(), nullable=False),
        sa.Column('storage_path', sa.String(length=512), nullable=False),
        sa.Column('original_filename', sa.String(length=255), nullable=True),
        sa.Column('file_checksum', sa.String(length=64), nullable=False),
        sa.Column('content_type', sa.String(length=100), nullable=True),
        sa.Column('file_size', sa.Integer(), nullable=True),
        sa.Column('width', sa.Integer(), nullable=True),
        sa.Column('height', sa.Integer(), nullable=True),
        sa.Column('quality_score', sa.Float(), nullable=True),
        sa.Column('is_primary', sa.Boolean(), server_default=sa.text('false'), nullable=False),
        sa.Column('processing_status', sa.String(length=32),
                  server_default=sa.text("'pending'"), nullable=False),
        sa.Column('failure_reason', sa.String(length=255), nullable=True),
        sa.Column('faiss_sync_state', sa.String(length=32),
                  server_default=sa.text("'not_applicable'"), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('created_by', sa.Integer(), nullable=True),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['identity_id'], ['identities.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['created_by'], ['users.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
    )

    op.create_index('idx_identity_image_identity', 'identity_images', ['identity_id'])
    op.create_index('idx_identity_image_created', 'identity_images', ['created_at'])
    op.create_index('idx_identity_image_status', 'identity_images', ['processing_status'])
    op.create_index('idx_identity_image_primary', 'identity_images', ['is_primary'])
    op.create_index('idx_identity_image_checksum', 'identity_images', ['file_checksum'])
    op.create_index('idx_identity_image_faiss_sync', 'identity_images', ['faiss_sync_state'])

    # Same photo, same person -> rejected at the database level.
    op.create_index('uq_identity_image_checksum', 'identity_images',
                    ['identity_id', 'file_checksum'], unique=True)

    # At most one primary image per identity.
    op.create_index('uq_identity_image_one_primary', 'identity_images',
                    ['identity_id'], unique=True,
                    postgresql_where=sa.text('is_primary'))

    # Nullable link from an embedding back to the photo it came from.
    op.add_column('identity_embeddings', sa.Column('image_id', sa.Integer(), nullable=True))
    op.create_foreign_key('fk_identity_embeddings_image_id', 'identity_embeddings',
                          'identity_images', ['image_id'], ['id'], ondelete='SET NULL')
    op.create_index('ix_identity_embeddings_image_id', 'identity_embeddings', ['image_id'])


def downgrade() -> None:
    op.drop_index('ix_identity_embeddings_image_id', table_name='identity_embeddings')
    op.drop_constraint('fk_identity_embeddings_image_id', 'identity_embeddings',
                       type_='foreignkey')
    op.drop_column('identity_embeddings', 'image_id')

    op.drop_index('uq_identity_image_one_primary', table_name='identity_images')
    op.drop_index('uq_identity_image_checksum', table_name='identity_images')
    op.drop_index('idx_identity_image_faiss_sync', table_name='identity_images')
    op.drop_index('idx_identity_image_checksum', table_name='identity_images')
    op.drop_index('idx_identity_image_primary', table_name='identity_images')
    op.drop_index('idx_identity_image_status', table_name='identity_images')
    op.drop_index('idx_identity_image_created', table_name='identity_images')
    op.drop_index('idx_identity_image_identity', table_name='identity_images')
    op.drop_table('identity_images')

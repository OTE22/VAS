"""PostgreSQL becomes the authoritative vector store.

Adds the single canonical per-vector synchronization state and the model
provenance reconciliation compares, and removes the duplicated image-level
state that was written but never read.

  + identity_embeddings.vector_index_sync_state  pending|synced|failed
  + identity_embeddings.embedding_model_version   nullable, NULL = unknown
  - identity_images.faiss_sync_state              write-only duplicate

State machine (the only legal transitions):
    pending -> synced    successful synchronization
    pending -> failed    synchronization failed
    failed  -> pending   retry requested
    synced  -> pending   reconciliation found it missing/stale/mismatched
Deletion is not a state — removing an embedding deletes the row and the index
entry.

DOWNGRADE SAFETY: the downgrade drops ONLY the two columns added here and
restores the removed one as nullable. It MUST NEVER touch
identity_embeddings.embedding — that column is the authoritative store, and a
downgrade that deleted vectors would destroy the source of truth. Existing rows
are backfilled to 'pending' rather than 'synced': claiming a vector is indexed
when nothing verified it would be a fabricated fact, and 'pending' is the state
reconciliation knows how to resolve.

Revision ID: f2a3b4c5d6e7
Revises: e1f2a3b4c5d6
Create Date: 2026-08-02
"""
from alembic import op
import sqlalchemy as sa

revision = 'f2a3b4c5d6e7'
down_revision = 'e1f2a3b4c5d6'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        'identity_embeddings',
        sa.Column('vector_index_sync_state', sa.String(length=16),
                  server_default=sa.text("'pending'"), nullable=False))
    op.add_column(
        'identity_embeddings',
        sa.Column('embedding_model_version', sa.String(length=64), nullable=True))
    op.create_index('idx_embedding_sync_state', 'identity_embeddings',
                    ['vector_index_sync_state'])

    # Duplicated, write-only state. The vector's state belongs on the vector.
    op.drop_index('idx_identity_image_faiss_sync', table_name='identity_images')
    op.drop_column('identity_images', 'faiss_sync_state')


def downgrade() -> None:
    # Restore the image-level column as NULLABLE with its original default.
    # It is re-created empty; its old values carried no information (nothing
    # ever read them).
    op.add_column(
        'identity_images',
        sa.Column('faiss_sync_state', sa.String(length=32),
                  server_default=sa.text("'not_applicable'"), nullable=False))
    op.create_index('idx_identity_image_faiss_sync', 'identity_images',
                    ['faiss_sync_state'])

    op.drop_index('idx_embedding_sync_state', table_name='identity_embeddings')
    op.drop_column('identity_embeddings', 'embedding_model_version')
    op.drop_column('identity_embeddings', 'vector_index_sync_state')
    # identity_embeddings.embedding is deliberately untouched in both
    # directions — see the module docstring.

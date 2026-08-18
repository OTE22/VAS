"""Add pgvector embedding column to identity_embeddings

This migration adds:
1. The 'embedding' column to store vectors directly in PostgreSQL
2. HNSW index for fast similarity search

Revision ID: 001_pgvector
Revises: 
Create Date: 2026-01-08
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = '001_pgvector'
down_revision = '000_baseline'   # baseline of the create_all-era tables (added 2026-08)
branch_labels = None
depends_on = None


def upgrade() -> None:
    """
    Add pgvector embedding column and create HNSW index.
    
    Prerequisites:
    - pgvector extension must be installed: CREATE EXTENSION IF NOT EXISTS vector;
    - This is handled in init-db.sql for fresh installations
    """
    # First, ensure pgvector extension exists
    op.execute('CREATE EXTENSION IF NOT EXISTS vector;')
    
    # Add embedding column (512-dim vector for ArcFace embeddings)
    # Use raw SQL to create vector type column (pgvector native type)
    op.execute('''
        ALTER TABLE identity_embeddings 
        ADD COLUMN IF NOT EXISTS embedding vector(512);
    ''')
    
    # Create HNSW index for fast approximate nearest neighbor search
    # Note: We use raw SQL because SQLAlchemy doesn't natively support HNSW indexes
    op.execute('''
        CREATE INDEX IF NOT EXISTS idx_embedding_vector_hnsw
        ON identity_embeddings 
        USING hnsw (embedding vector_cosine_ops)
        WITH (m = 16, ef_construction = 64)
    ''')
    
    print("✅ Added 'embedding' column and HNSW index to identity_embeddings table")
    print("💡 Run scripts/migrate_faiss_to_pgvector.py to migrate existing FAISS embeddings")


def downgrade() -> None:
    """Remove embedding column and index."""
    # Drop HNSW index
    op.execute('DROP INDEX IF EXISTS idx_embedding_vector_hnsw')
    
    # Remove embedding column
    op.drop_column('identity_embeddings', 'embedding')
    
    print("✅ Removed 'embedding' column from identity_embeddings table")


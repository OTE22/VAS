"""Add HNSW index for user query embeddings

Revision ID: 4e4e7b140f5d
Revises: 569b6ec65a8f
Create Date: 2026-01-13 17:21:18.903237

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '4e4e7b140f5d'
down_revision: Union[str, None] = '569b6ec65a8f'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Check if pgvector extension exists, create if not
    op.execute("CREATE EXTENSION IF NOT EXISTS vector;")
    
    # Convert embedding column to vector type if it's JSONB
    # Note: This assumes the column might be JSONB. If it's already vector, this will be a no-op
    op.execute("""
        DO $$
        BEGIN
            -- Check if column is JSONB and convert to vector
            IF EXISTS (
                SELECT 1 FROM information_schema.columns 
                WHERE table_name = 'user_query_embeddings' 
                AND column_name = 'embedding' 
                AND data_type = 'jsonb'
            ) THEN
                -- Convert JSONB to vector (assuming embeddings are stored as JSON arrays)
                ALTER TABLE user_query_embeddings 
                ALTER COLUMN embedding TYPE vector(384) 
                USING embedding::text::vector;
            END IF;
        END $$;
    """)
    
    # Create HNSW index for fast approximate nearest neighbor search
    # HNSW (Hierarchical Navigable Small World) is optimized for cosine distance
    # vector_cosine_ops is the operator class for cosine distance
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_user_query_embeddings_hnsw 
        ON user_query_embeddings 
        USING hnsw (embedding vector_cosine_ops)
        WITH (m = 16, ef_construction = 64);
    """)
    
    # Update column comment
    op.execute("""
        COMMENT ON COLUMN user_query_embeddings.embedding IS 
        'Vector embedding stored as pgvector (384-dim for sentence-transformers/all-MiniLM-L6-v2). HNSW index for fast similarity search.';
    """)


def downgrade() -> None:
    # Drop HNSW index
    op.execute("DROP INDEX IF EXISTS idx_user_query_embeddings_hnsw;")
    
    # Convert vector column back to JSONB if needed
    op.execute("""
        DO $$
        BEGIN
            -- Check if column is vector and convert to JSONB
            IF EXISTS (
                SELECT 1 FROM information_schema.columns 
                WHERE table_name = 'user_query_embeddings' 
                AND column_name = 'embedding' 
                AND udt_name = 'vector'
            ) THEN
                -- Convert vector to JSONB
                ALTER TABLE user_query_embeddings 
                ALTER COLUMN embedding TYPE jsonb 
                USING to_jsonb(embedding::text);
            END IF;
        END $$;
    """)
    
    # Restore original comment
    op.execute("""
        COMMENT ON COLUMN user_query_embeddings.embedding IS 
        'Vector embedding stored as JSONB array (fallback when pgvector not available)';
    """)

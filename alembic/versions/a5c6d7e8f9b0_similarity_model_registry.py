"""Similarity model registry

Versioned model lifecycle for the merge-suggestion similarity model:
candidate -> (validated/quality-gated) -> active -> archived, with
rollback. A partial unique index guarantees at most ONE active model
per model_type; activation is a transaction that archives the previous
active row and promotes the candidate.

Revision ID: a5c6d7e8f9b0
Revises: f4b5c6d7e8a9
Create Date: 2026-07-26
"""
from alembic import op

revision = 'a5c6d7e8f9b0'
down_revision = 'f4b5c6d7e8a9'
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
        CREATE TABLE IF NOT EXISTS similarity_model_registry (
            id SERIAL PRIMARY KEY,
            model_type VARCHAR(50) NOT NULL DEFAULT 'merge_similarity',
            version INTEGER NOT NULL,
            status VARCHAR(20) NOT NULL DEFAULT 'candidate',
            artifact_name VARCHAR(200) NOT NULL,
            artifact_path TEXT NOT NULL,
            artifact_hash VARCHAR(64),
            training_job_id VARCHAR(64),
            dataset_version VARCHAR(64),
            dataset_hash VARCHAR(64),
            feature_schema_version VARCHAR(64),
            seed INTEGER,
            metrics JSONB,
            quality_gates JSONB,
            comparison JSONB,
            created_at TIMESTAMP NOT NULL DEFAULT now(),
            created_by INTEGER,
            activated_at TIMESTAMP,
            activated_by INTEGER,
            archived_at TIMESTAMP,
            failure_code VARCHAR(64),
            notes TEXT
        )
    """)
    op.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS uq_model_registry_one_active
        ON similarity_model_registry (model_type)
        WHERE status = 'active'
    """)
    op.execute("CREATE INDEX IF NOT EXISTS idx_model_registry_type_status ON similarity_model_registry (model_type, status)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_model_registry_created ON similarity_model_registry (created_at)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_model_registry_job ON similarity_model_registry (training_job_id)")


def downgrade():
    op.execute("DROP TABLE IF EXISTS similarity_model_registry")

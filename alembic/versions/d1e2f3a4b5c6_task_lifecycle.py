"""Background-task lifecycle columns

Extends background_task_history so every job has a full, observable
lifecycle: stable job_id, progress, retries, structured result, error
code/message, correlation ids and worker identity. Adds 'cancelled'
support (status stays a VARCHAR — no enum migration needed).

Revision ID: d1e2f3a4b5c6
Revises: c9d0e1f2a3b4
Create Date: 2026-07-25
"""
from alembic import op

revision = 'd1e2f3a4b5c6'
down_revision = 'c9d0e1f2a3b4'
branch_labels = None
depends_on = None

COLUMNS = (
    ("job_id", "VARCHAR(64)"),
    ("progress_percent", "INTEGER"),
    ("result", "JSONB"),
    ("retry_count", "INTEGER DEFAULT 0"),
    ("max_retries", "INTEGER DEFAULT 0"),
    ("error_code", "VARCHAR(50)"),
    ("error_message", "TEXT"),
    ("created_by_user_id", "INTEGER"),
    ("request_id", "VARCHAR(64)"),
    ("correlation_id", "VARCHAR(64)"),
    ("worker_name", "VARCHAR(100)"),
    ("hostname", "VARCHAR(100)"),
    ("updated_at", "TIMESTAMP"),
)


def upgrade():
    for name, ddl in COLUMNS:
        op.execute(f"ALTER TABLE background_task_history ADD COLUMN IF NOT EXISTS {name} {ddl}")
    op.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_task_history_job_id "
               "ON background_task_history (job_id) WHERE job_id IS NOT NULL")
    op.execute("CREATE INDEX IF NOT EXISTS idx_task_history_job_id ON background_task_history (job_id)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_task_history_correlation ON background_task_history (correlation_id)")


def downgrade():
    op.execute("DROP INDEX IF EXISTS uq_task_history_job_id")
    op.execute("DROP INDEX IF EXISTS idx_task_history_job_id")
    op.execute("DROP INDEX IF EXISTS idx_task_history_correlation")
    for name, _ in COLUMNS:
        op.execute(f"ALTER TABLE background_task_history DROP COLUMN IF EXISTS {name}")

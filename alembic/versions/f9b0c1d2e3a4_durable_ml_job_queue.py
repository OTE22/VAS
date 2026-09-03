"""Durable PostgreSQL-backed queue controls for ML jobs.

Revision ID: f9b0c1d2e3a4
Revises: d6f8a2c4e7b1
Create Date: 2026-09-03

The existing background_task_history row remains the operator-facing job
record.  Queue-only columns add command payload, worker lease/heartbeat, and
persistent cancellation without creating a second lifecycle to reconcile.
"""

from alembic import op


revision = "f9b0c1d2e3a4"
down_revision = "d6f8a2c4e7b1"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("ALTER TABLE background_task_history ADD COLUMN IF NOT EXISTS queue_name VARCHAR(32)")
    op.execute("ALTER TABLE background_task_history ADD COLUMN IF NOT EXISTS payload JSONB")
    op.execute("ALTER TABLE background_task_history ADD COLUMN IF NOT EXISTS lease_owner VARCHAR(100)")
    op.execute("ALTER TABLE background_task_history ADD COLUMN IF NOT EXISTS lease_expires_at TIMESTAMP")
    op.execute("ALTER TABLE background_task_history ADD COLUMN IF NOT EXISTS heartbeat_at TIMESTAMP")
    op.execute("ALTER TABLE background_task_history ADD COLUMN IF NOT EXISTS cancel_requested_at TIMESTAMP")
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_task_history_queue_status "
        "ON background_task_history (queue_name, status, scheduled_time)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_task_history_lease_expiry "
        "ON background_task_history (lease_expires_at) "
        "WHERE status = 'running' AND lease_expires_at IS NOT NULL"
    )
    # One active operation of each expensive ML kind. Training, collection,
    # dataset building and drift may queue independently, while repeated
    # button clicks cannot create duplicate work.
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_ml_queue_active_task_type "
        "ON background_task_history (task_type) "
        "WHERE queue_name = 'ml' AND status IN ('scheduled', 'running')"
    )


def downgrade():
    op.execute("DROP INDEX IF EXISTS uq_ml_queue_active_task_type")
    op.execute("DROP INDEX IF EXISTS idx_task_history_lease_expiry")
    op.execute("DROP INDEX IF EXISTS idx_task_history_queue_status")
    for column in (
        "cancel_requested_at", "heartbeat_at", "lease_expires_at",
        "lease_owner", "payload", "queue_name",
    ):
        op.execute(f"ALTER TABLE background_task_history DROP COLUMN IF EXISTS {column}")

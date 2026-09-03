"""Observable ML worker heartbeat.

Revision ID: faa1b2c3d4e5
Revises: f9b0c1d2e3a4
Create Date: 2026-09-03
"""

from alembic import op


revision = "faa1b2c3d4e5"
down_revision = "f9b0c1d2e3a4"
branch_labels = None
depends_on = None


def upgrade():
    op.execute(
        "CREATE TABLE IF NOT EXISTS ml_worker_heartbeats ("
        "worker_id VARCHAR(100) PRIMARY KEY, hostname VARCHAR(100) NOT NULL, "
        "process_id INTEGER NOT NULL, status VARCHAR(20) NOT NULL DEFAULT 'idle', "
        "current_job_id VARCHAR(64), started_at TIMESTAMP NOT NULL DEFAULT now(), "
        "heartbeat_at TIMESTAMP NOT NULL DEFAULT now())"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_ml_worker_heartbeat_at "
        "ON ml_worker_heartbeats (heartbeat_at)"
    )


def downgrade():
    op.execute("DROP TABLE IF EXISTS ml_worker_heartbeats")

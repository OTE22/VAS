"""Live-alert hardening

1. Trigger idempotency: a (alert_id, detection_id) pair can only produce ONE
   trigger row (partial unique index — detection_id may be NULL for realtime
   triggers created before the detection row is persisted).
2. live_alert_audit_log: audit trail for alert lifecycle actions
   (create/pause/resume/delete/update/acknowledge/bulk_acknowledge/
   channel_test/delivery_failure). Never stores tokens, embeddings or
   other sensitive payloads — details is counts/ids only.
3. Index for the acknowledged-filtered, paginated trigger queries.

Revision ID: e2f3a4b5c6d7
Revises: d1e2f3a4b5c6
Create Date: 2026-07-25
"""
from alembic import op

revision = 'e2f3a4b5c6d7'
down_revision = 'd1e2f3a4b5c6'
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS uq_alert_trigger_alert_detection
        ON live_alert_triggers (alert_id, detection_id)
        WHERE detection_id IS NOT NULL
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_alert_trigger_alert_ack
        ON live_alert_triggers (alert_id, acknowledged, created_at)
    """)
    op.execute("""
        CREATE TABLE IF NOT EXISTS live_alert_audit_log (
            id SERIAL PRIMARY KEY,
            user_id INTEGER,
            username VARCHAR(100),
            alert_id UUID,
            action VARCHAR(50) NOT NULL,
            details JSONB,
            result VARCHAR(20),
            request_id VARCHAR(64),
            ip_address VARCHAR(45),
            created_at TIMESTAMP NOT NULL DEFAULT now()
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS idx_la_audit_alert ON live_alert_audit_log (alert_id, created_at)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_la_audit_user ON live_alert_audit_log (user_id, created_at)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_la_audit_created ON live_alert_audit_log (created_at)")


def downgrade():
    op.execute("DROP INDEX IF EXISTS uq_alert_trigger_alert_detection")
    op.execute("DROP INDEX IF EXISTS idx_alert_trigger_alert_ack")
    op.execute("DROP TABLE IF EXISTS live_alert_audit_log")

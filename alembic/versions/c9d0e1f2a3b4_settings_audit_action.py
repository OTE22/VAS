"""Add `action` column to settings_audit_log

Distinguishes what actually happened for each audit entry:
value_saved | value_applied | application_failed | retention_dry_run |
retention_executed | setting_reverted

Revision ID: c9d0e1f2a3b4
Revises: b7c8d9e0f1a2
Create Date: 2026-07-25
"""
from alembic import op

revision = 'c9d0e1f2a3b4'
down_revision = 'b7c8d9e0f1a2'
branch_labels = None
depends_on = None


def upgrade():
    op.execute("ALTER TABLE settings_audit_log ADD COLUMN IF NOT EXISTS action VARCHAR(50)")


def downgrade():
    op.execute("ALTER TABLE settings_audit_log DROP COLUMN IF EXISTS action")

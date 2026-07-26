"""Watchlist hardening

1. Optimistic concurrency: integer version column, bumped on every update —
   concurrent admin edits get a controlled 409 instead of lost updates.
2. Real soft deletion: deleted_at/deleted_by_user_id/deletion_reason.
   Soft-deleted watchlists stop matching detections but keep their entries,
   alert history and audit trail (no cascade wipe).
3. Name policy: the strict exact-match unique constraint is replaced by a
   case-insensitive unique index over LIVE (non-deleted) watchlists only —
   a deleted watchlist's name can be reused, "VIP" vs "vip" cannot coexist.

Revision ID: f4b5c6d7e8a9
Revises: e2f3a4b5c6d7
Create Date: 2026-07-26
"""
from alembic import op

revision = 'f4b5c6d7e8a9'
down_revision = 'e2f3a4b5c6d7'
branch_labels = None
depends_on = None


def upgrade():
    op.execute("ALTER TABLE watchlists ADD COLUMN IF NOT EXISTS version INTEGER NOT NULL DEFAULT 1")
    op.execute("ALTER TABLE watchlists ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMP NULL")
    op.execute("ALTER TABLE watchlists ADD COLUMN IF NOT EXISTS deleted_by_user_id INTEGER NULL")
    op.execute("ALTER TABLE watchlists ADD COLUMN IF NOT EXISTS deletion_reason TEXT NULL")
    # Replace exact unique constraint with case-insensitive live-only uniqueness
    op.execute("ALTER TABLE watchlists DROP CONSTRAINT IF EXISTS watchlists_name_key")
    op.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS uq_watchlists_name_live
        ON watchlists (lower(btrim(name)))
        WHERE deleted_at IS NULL
    """)
    op.execute("CREATE INDEX IF NOT EXISTS idx_watchlists_deleted ON watchlists (deleted_at)")


def downgrade():
    op.execute("DROP INDEX IF EXISTS uq_watchlists_name_live")
    op.execute("DROP INDEX IF EXISTS idx_watchlists_deleted")
    op.execute("ALTER TABLE watchlists DROP COLUMN IF EXISTS version")
    op.execute("ALTER TABLE watchlists DROP COLUMN IF EXISTS deleted_at")
    op.execute("ALTER TABLE watchlists DROP COLUMN IF EXISTS deleted_by_user_id")
    op.execute("ALTER TABLE watchlists DROP COLUMN IF EXISTS deletion_reason")
    op.execute("ALTER TABLE watchlists ADD CONSTRAINT watchlists_name_key UNIQUE (name)")

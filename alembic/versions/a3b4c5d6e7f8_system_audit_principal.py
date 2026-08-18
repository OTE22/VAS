"""System audit principal for machine-initiated identity actions

Revision ID: a3b4c5d6e7f8
Revises: f2a3b4c5d6e7
Create Date: 2026-08-02

`identity_audit_log.user_id` is NOT NULL and a foreign key to `users.id`, so
automated actions — index rebuild, reconciliation, corruption recovery, the
configured-fallback event — had nowhere to write. They were writing NULL and the
insert was rejected, which meant the exact operations that most need a durable
record produced none.

Two ways to fix that; this migration takes the second:

  1. Make `user_id` nullable. Loses the FK guarantee for every row, and the
     model's own contract names `username` AND `user_id` as the accountability
     pair.
  2. Give machine actions a real principal that is not a person.

The `system` account exists solely to be referenced. It cannot authenticate:
`is_active=false` is checked on both login (auth_service.py:142) and token
validation (auth_service.py:292), and the password hash is the literal string
'!', which is not a valid bcrypt hash and can never verify. Role `system` is not
`admin`, so it grants nothing through require_role either.

Attributing an automated rebuild to the human admin account was the alternative,
and it would have written a false audit record — worse than no record at all.
"""
from alembic import op
import sqlalchemy as sa


revision = 'a3b4c5d6e7f8'
down_revision = 'f2a3b4c5d6e7'
branch_labels = None
depends_on = None

SYSTEM_USERNAME = 'system'
SYSTEM_EMAIL = 'system@localhost.invalid'
# Not a bcrypt hash; passlib/bcrypt verification against it cannot succeed.
UNUSABLE_PASSWORD_HASH = '!'


def upgrade() -> None:
    op.execute(sa.text("""
        INSERT INTO users (username, email, password_hash, full_name, role,
                           is_active, can_use_chatbot, created_at, updated_at,
                           must_change_password, permissions_version)
        VALUES (:u, :e, :p, 'Automated system actions', 'system',
                false, false, NOW(), NOW(), false, 1)
        ON CONFLICT (username) DO NOTHING
    """).bindparams(u=SYSTEM_USERNAME, e=SYSTEM_EMAIL, p=UNUSABLE_PASSWORD_HASH))

    # If the account already existed for some other reason, force it inactive:
    # a login-capable account named `system` would be a far worse outcome than
    # a missing audit row.
    op.execute(sa.text("""
        UPDATE users SET is_active = false, can_use_chatbot = false
        WHERE username = :u
    """).bindparams(u=SYSTEM_USERNAME))


def downgrade() -> None:
    """Remove the principal only if nothing references it.

    Audit rows are evidence. If machine actions have already been recorded
    against this account, deleting it would either break the foreign key or
    destroy the attribution on rows that remain — so the account stays and the
    downgrade is a no-op for that case. This is deliberate: a downgrade may undo
    a schema change, never an audit trail.
    """
    op.execute(sa.text("""
        DELETE FROM users
        WHERE username = :u
          AND NOT EXISTS (
              SELECT 1 FROM identity_audit_log a
              JOIN users s ON s.id = a.user_id
              WHERE s.username = :u
          )
    """).bindparams(u=SYSTEM_USERNAME))

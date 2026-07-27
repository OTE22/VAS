"""authorization propagation: permissions_version, audit log, canonical roles

Three related changes, one revision, because they only make sense together:

1. `users.permissions_version` — bumped whenever a user's authorization
   changes. Long-lived connections (the SQL-agent WebSocket, an in-flight SSE
   stream) compare one integer instead of re-resolving permissions per message,
   which is what lets a revocation reach a session that is already open.

2. `user_authorization_audit_log` — nothing recorded who changed a role, from
   what, to what, or when. Shaped after the existing `settings_audit_log`
   (db_models.py) rather than inventing a second pattern.

3. Role canonicalisation — the database held a capitalised "Observer" beside
   lowercase peers, and role checks were case-sensitive, so the mismatch could
   deny access to the wrong person.

Revision ID: c1d2e3f4a5b6
Revises: b1c2d3e4f5a6
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "c1d2e3f4a5b6"
down_revision = "b1c2d3e4f5a6"
branch_labels = None
depends_on = None


# Historical spelling -> canonical. Mirrors _ROLE_ALIASES in
# backend/auth/capabilities.py; kept literal here because a migration must not
# depend on application code that may have moved on by the time it runs.
_ROLE_REWRITES = [
    ("Observer", "observer"),
    ("OBSERVER", "observer"),
    ("Analyzer", "analyzer"),
    ("analyst", "analyzer"),
    ("administrator", "admin"),
    ("normal_user", "user"),
]


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    user_columns = {c["name"] for c in inspector.get_columns("users")}

    # --- 1. permissions_version ------------------------------------------
    if "permissions_version" not in user_columns:
        op.add_column(
            "users",
            sa.Column(
                "permissions_version",
                sa.Integer(),
                nullable=False,
                # server_default matters: existing rows need a value, and code
                # paths that INSERT without naming this column must keep working.
                server_default=sa.text("1"),
            ),
        )

    # --- 2. canonical role values ----------------------------------------
    # Done before the audit table so the first audit rows already record
    # canonical values.
    for old, new in _ROLE_REWRITES:
        op.execute(
            sa.text("UPDATE users SET role = :new WHERE role = :old")
            .bindparams(new=new, old=old)
        )

    # --- 3. audit log -----------------------------------------------------
    if "user_authorization_audit_log" not in inspector.get_table_names():
        op.create_table(
            "user_authorization_audit_log",
            sa.Column("id", sa.Integer(), primary_key=True, index=True),

            # Target. username is denormalised so the record survives the user
            # being deleted — the same reasoning as
            # settings_audit_log.changed_by_username.
            sa.Column("target_user_id", sa.Integer(),
                      sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
            sa.Column("target_username", sa.String(100), nullable=True),

            # Actor.
            sa.Column("changed_by_user_id", sa.Integer(),
                      sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
            sa.Column("changed_by_username", sa.String(100), nullable=True),

            sa.Column("action", sa.String(50), nullable=False,
                      server_default=sa.text("'authorization_updated'")),

            # Before/after. Nullable because an action may touch only some of
            # them; a NULL pair means "not part of this change".
            sa.Column("old_role", sa.String(50), nullable=True),
            sa.Column("new_role", sa.String(50), nullable=True),
            sa.Column("old_can_use_chatbot", sa.Boolean(), nullable=True),
            sa.Column("new_can_use_chatbot", sa.Boolean(), nullable=True),
            sa.Column("old_is_active", sa.Boolean(), nullable=True),
            sa.Column("new_is_active", sa.Boolean(), nullable=True),
            sa.Column("old_pipeline_ids", postgresql.JSONB(), nullable=True),
            sa.Column("new_pipeline_ids", postgresql.JSONB(), nullable=True),

            sa.Column("permissions_version", sa.Integer(), nullable=True),

            # Request context. Never a token, password or cookie.
            sa.Column("request_id", sa.String(64), nullable=True),
            sa.Column("ip_address", sa.String(45), nullable=True),
            sa.Column("user_agent", sa.Text(), nullable=True),
            sa.Column("change_reason", sa.Text(), nullable=True),

            sa.Column("created_at", sa.DateTime(), nullable=False,
                      server_default=sa.text("now()")),
        )
        op.create_index("idx_user_authz_audit_target",
                        "user_authorization_audit_log",
                        ["target_user_id", "created_at"])
        op.create_index("idx_user_authz_audit_actor",
                        "user_authorization_audit_log",
                        ["changed_by_user_id", "created_at"])
        op.create_index("idx_user_authz_audit_created",
                        "user_authorization_audit_log", ["created_at"])

    # --- 4. the SQL agent must not be able to read the audit trail --------
    # fr_readonly executes LLM-generated SQL, and db/roles.sql grants it SELECT
    # on every future table created by fr_migrator (which is how migrations run
    # in production). Without this REVOKE, a prompt injection that defeats the
    # AST guard's table allowlist could read who changed whose permissions.
    op.execute(sa.text("""
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fr_readonly') THEN
                REVOKE SELECT ON user_authorization_audit_log FROM fr_readonly;
            END IF;
        END
        $$;
    """))


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if "user_authorization_audit_log" in inspector.get_table_names():
        op.drop_index("idx_user_authz_audit_created",
                      table_name="user_authorization_audit_log")
        op.drop_index("idx_user_authz_audit_actor",
                      table_name="user_authorization_audit_log")
        op.drop_index("idx_user_authz_audit_target",
                      table_name="user_authorization_audit_log")
        op.drop_table("user_authorization_audit_log")

    user_columns = {c["name"] for c in inspector.get_columns("users")}
    if "permissions_version" in user_columns:
        op.drop_column("users", "permissions_version")

    # Role canonicalisation is intentionally NOT reversed. Restoring
    # "Observer" would reintroduce the case-sensitivity bug, and the lowercase
    # value is valid for the previous code as well.

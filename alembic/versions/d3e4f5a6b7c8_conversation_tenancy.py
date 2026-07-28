"""Tenancy + conversation domain: Organization -> Workspace -> Conversation -> Branch -> Message.

Why this migration exists
-------------------------
Chat history has been a flat table (user_query_history keyed by a string
session_id). The product direction requires first-class conversations with
branching and typed message content, scoped as
Organization -> Workspace -> User -> Conversation -> ConversationBranch ->
Message, with conversations belonging DIRECTLY to a workspace (no Project
layer — an explicit scope decision).

What it does
------------
1. Creates organizations, workspaces, workspace_members, conversations,
   conversation_branches, messages, message_feedback.
2. Seeds "Default Organization" / "Default Workspace" and enrolls every
   existing user as a member (platform admins as workspace admins).
3. Backfills every (user, session) group in user_query_history into a
   conversation with a primary branch, converting each history row into a
   user message + assistant message with TYPED content blocks. The flat
   table is left untouched — reads keep working during rollout, and the
   backfill is idempotent via conversations.legacy_session_id.
4. REVOKEs SELECT on every new table from fr_readonly. That role executes
   LLM-generated SQL and inherits SELECT on future tables via ALTER DEFAULT
   PRIVILEGES (db/roles.sql); without the revoke, a prompt-injected query
   could read anyone's private conversation content.

Reversible: downgrade drops the new tables only; user_query_history was
never modified, so no data is lost that existed before the upgrade.

Revision ID: d3e4f5a6b7c8
Revises: c1d2e3f4a5b6
"""

import json
import uuid

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision = "d3e4f5a6b7c8"
down_revision = "c1d2e3f4a5b6"
branch_labels = None
depends_on = None

_NEW_TABLES = (
    "message_feedback",
    "messages",
    "conversation_branches",
    "conversations",
    "workspace_members",
    "workspaces",
    "organizations",
)


def upgrade() -> None:
    # ------------------------------------------------------------------ DDL
    op.create_table(
        "organizations",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.String(255), nullable=False, unique=True),
        sa.Column("is_default", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.text("now()")),
    )

    op.create_table(
        "workspaces",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("organization_id", UUID(as_uuid=True),
                  sa.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("is_default", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("settings", JSONB(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.text("now()")),
    )
    op.create_index("idx_workspace_org", "workspaces", ["organization_id", "name"], unique=True)

    op.create_table(
        "workspace_members",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("workspace_id", UUID(as_uuid=True),
                  sa.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", sa.Integer(),
                  sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("role", sa.String(32), nullable=False, server_default="member"),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("now()")),
    )
    op.create_index("idx_ws_member_unique", "workspace_members",
                    ["workspace_id", "user_id"], unique=True)
    op.create_index("idx_ws_member_user", "workspace_members", ["user_id"])

    op.create_table(
        "conversations",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("workspace_id", UUID(as_uuid=True),
                  sa.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", sa.Integer(),
                  sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("title", sa.String(500), nullable=False, server_default="New conversation"),
        sa.Column("pinned", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("archived", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("deleted_at", sa.DateTime(), nullable=True),
        sa.Column("legacy_session_id", sa.String(255), nullable=True),
        sa.Column("last_message_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.text("now()")),
    )
    op.create_index("idx_conv_owner_listing", "conversations",
                    ["user_id", "workspace_id", "deleted_at", "pinned", "last_message_at"])
    op.create_index("idx_conv_legacy_session", "conversations",
                    ["user_id", "legacy_session_id"])
    op.create_index("idx_conv_deleted", "conversations", ["deleted_at"])

    op.create_table(
        "conversation_branches",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("conversation_id", UUID(as_uuid=True),
                  sa.ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("parent_branch_id", UUID(as_uuid=True),
                  sa.ForeignKey("conversation_branches.id", ondelete="SET NULL"), nullable=True),
        sa.Column("forked_from_message_id", UUID(as_uuid=True), nullable=True),
        sa.Column("name", sa.String(255), nullable=True),
        sa.Column("is_primary", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("now()")),
    )
    op.create_index("idx_branch_conversation", "conversation_branches",
                    ["conversation_id", "created_at"])

    op.create_table(
        "messages",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("branch_id", UUID(as_uuid=True),
                  sa.ForeignKey("conversation_branches.id", ondelete="CASCADE"), nullable=False),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("content_blocks", JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("status", sa.String(16), nullable=False, server_default="complete"),
        sa.Column("model_provider", sa.String(64), nullable=True),
        sa.Column("model_name", sa.String(255), nullable=True),
        sa.Column("processing_time_ms", sa.Float(), nullable=True),
        sa.Column("edited_from_message_id", UUID(as_uuid=True), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("now()")),
    )
    op.create_index("idx_message_branch_seq", "messages", ["branch_id", "sequence"], unique=True)
    op.create_index("idx_message_created", "messages", ["created_at"])

    op.create_table(
        "message_feedback",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("message_id", UUID(as_uuid=True),
                  sa.ForeignKey("messages.id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", sa.Integer(),
                  sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("rating", sa.Integer(), nullable=False),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("now()")),
    )
    op.create_index("idx_feedback_unique", "message_feedback",
                    ["message_id", "user_id"], unique=True)
    op.create_index("idx_feedback_user", "message_feedback", ["user_id"])

    # ------------------------------------------------- lock out fr_readonly
    # fr_readonly executes LLM-generated SQL and inherits SELECT on all future
    # tables (ALTER DEFAULT PRIVILEGES in db/roles.sql). Conversations are
    # private user content; a prompt-injected query must not be able to read
    # them. Guarded so dev databases without the role still migrate.
    op.execute("""
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fr_readonly') THEN
                REVOKE SELECT ON organizations, workspaces, workspace_members,
                                 conversations, conversation_branches,
                                 messages, message_feedback
                FROM fr_readonly;
            END IF;
        END $$;
    """)

    # -------------------------------------------------------- seed + backfill
    bind = op.get_bind()

    org_id = uuid.uuid4()
    ws_id = uuid.uuid4()
    bind.execute(sa.text(
        "INSERT INTO organizations (id, name, is_default) "
        "VALUES (:id, 'Default Organization', true)"
    ), {"id": org_id})
    bind.execute(sa.text(
        "INSERT INTO workspaces (id, organization_id, name, is_default) "
        "VALUES (:id, :org, 'Default Workspace', true)"
    ), {"id": ws_id, "org": org_id})

    # Every existing user becomes a member; platform admins run the workspace.
    users = bind.execute(sa.text("SELECT id, role FROM users")).fetchall()
    for user_id, platform_role in users:
        bind.execute(sa.text(
            "INSERT INTO workspace_members (id, workspace_id, user_id, role) "
            "VALUES (:id, :ws, :uid, :role)"
        ), {"id": uuid.uuid4(), "ws": ws_id, "uid": user_id,
            "role": "admin" if platform_role == "admin" else "member"})

    # Backfill the flat history. One conversation per (user, session); rows
    # with a NULL session_id collapse into one 'Imported history' conversation
    # per user rather than being dropped.
    groups = bind.execute(sa.text("""
        SELECT user_id, COALESCE(session_id, '') AS session_key,
               MIN(query_timestamp) AS started,
               MAX(COALESCE(response_timestamp, query_timestamp)) AS last_at
        FROM user_query_history
        GROUP BY user_id, COALESCE(session_id, '')
        ORDER BY user_id, started
    """)).fetchall()

    for user_id, session_key, started, last_at in groups:
        first_query = bind.execute(sa.text("""
            SELECT query_text FROM user_query_history
            WHERE user_id = :uid AND COALESCE(session_id, '') = :sk
            ORDER BY query_timestamp, id LIMIT 1
        """), {"uid": user_id, "sk": session_key}).scalar()
        title = (first_query or "Imported history").strip()[:120] or "Imported history"

        conv_id = uuid.uuid4()
        branch_id = uuid.uuid4()
        bind.execute(sa.text("""
            INSERT INTO conversations (id, workspace_id, user_id, title,
                                       legacy_session_id, last_message_at, created_at)
            VALUES (:id, :ws, :uid, :title, :legacy, :last_at, :started)
        """), {"id": conv_id, "ws": ws_id, "uid": user_id, "title": title,
               "legacy": session_key or None, "last_at": last_at, "started": started})
        bind.execute(sa.text("""
            INSERT INTO conversation_branches (id, conversation_id, is_primary, created_at)
            VALUES (:id, :conv, true, :started)
        """), {"id": branch_id, "conv": conv_id, "started": started})

        rows = bind.execute(sa.text("""
            SELECT query_text, response_text, query_timestamp, response_timestamp,
                   success, error_message, processing_time_ms, query_metadata
            FROM user_query_history
            WHERE user_id = :uid AND COALESCE(session_id, '') = :sk
            ORDER BY query_timestamp, id
        """), {"uid": user_id, "sk": session_key}).fetchall()

        seq = 0
        for (q_text, r_text, q_ts, r_ts, success, error_message,
             processing_ms, metadata) in rows:
            seq += 1
            bind.execute(sa.text("""
                INSERT INTO messages (id, branch_id, role, sequence,
                                      content_blocks, status, created_at)
                VALUES (:id, :branch, 'user', :seq, CAST(:blocks AS jsonb),
                        'complete', :ts)
            """), {"id": uuid.uuid4(), "branch": branch_id, "seq": seq,
                   "blocks": json.dumps([{"type": "text", "text": q_text or ""}]),
                   "ts": q_ts})

            blocks = []
            if r_text:
                blocks.append({"type": "text", "text": r_text})
            meta = metadata if isinstance(metadata, dict) else None
            if meta and meta.get("sql"):
                blocks.append({"type": "sql", "sql": str(meta["sql"])})
            if error_message:
                blocks.append({"type": "error", "message": str(error_message)})
            if not blocks:
                blocks.append({"type": "text", "text": ""})

            seq += 1
            bind.execute(sa.text("""
                INSERT INTO messages (id, branch_id, role, sequence,
                                      content_blocks, status, processing_time_ms, created_at)
                VALUES (:id, :branch, 'assistant', :seq, CAST(:blocks AS jsonb),
                        :status, :pms, :ts)
            """), {"id": uuid.uuid4(), "branch": branch_id, "seq": seq,
                   "blocks": json.dumps(blocks),
                   "status": "complete" if success else "failed",
                   "pms": processing_ms, "ts": r_ts or q_ts})


def downgrade() -> None:
    for table in _NEW_TABLES:
        op.drop_table(table)

"""Agent artifact registry.

The SQL agent could generate a PDF but never remember it had: exports were
bytes in an HTTP response and nothing was persisted. So "make the last report
Arabic" had nothing to resolve against, and "same report but only for camera
3" could not recover the report's originating query.

This table is that memory. It records WHERE the file is (relative to
settings.ARTIFACTS_DIR — never an absolute or client-supplied path) and, more
importantly, the immutable lineage needed to reproduce or amend the document:
the SQL it reports on, the message and result rows it came from, and the
parent artifact when it is a translation or a re-filter. Free-text
`source_query` is kept for humans and is explicitly NOT the reproduction path.

Ownership follows the conversation rule: chat history outlives the account, so
user_id is SET NULL and created_by_username preserves attribution. Files are
removed with the row by the retention job.

Revision ID: c5e7f9a1b3d4
Revises: b4d5e6f7a8c9
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "c5e7f9a1b3d4"
down_revision: Union[str, None] = "b4d5e6f7a8c9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "agent_artifacts",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("user_id", sa.Integer(),
                  sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_by_username", sa.String(length=255), nullable=False),
        sa.Column("conversation_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("conversations.id", ondelete="SET NULL"), nullable=True),
        sa.Column("type", sa.String(length=16), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("language", sa.String(length=8), nullable=False, server_default="en"),
        sa.Column("storage_path", sa.String(length=512), nullable=False),
        sa.Column("source_query", sa.Text(), nullable=True),
        sa.Column("source_sql", sa.Text(), nullable=True),
        sa.Column("source_content", sa.Text(), nullable=True),
        sa.Column("source_message_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("messages.id", ondelete="SET NULL"), nullable=True),
        sa.Column("source_result_id", sa.Integer(),
                  sa.ForeignKey("user_query_history.id", ondelete="SET NULL"), nullable=True),
        sa.Column("modification_meta", postgresql.JSONB(), nullable=True),
        sa.Column("parent_artifact_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("agent_artifacts.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("deleted_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_agent_artifacts_user_id", "agent_artifacts", ["user_id"])
    op.create_index("ix_agent_artifacts_conversation_id", "agent_artifacts", ["conversation_id"])
    op.create_index("ix_agent_artifacts_created_at", "agent_artifacts", ["created_at"])
    # The reference resolver's hot path: newest live artifacts for one owner.
    op.create_index("idx_artifact_owner_recent", "agent_artifacts", ["user_id", "created_at"])
    op.create_index("idx_artifact_conversation", "agent_artifacts",
                    ["conversation_id", "created_at"])


def downgrade() -> None:
    # Rows are dropped with the table; the FILES under ARTIFACTS_DIR are left
    # in place deliberately. Deleting user-visible documents as a side effect
    # of a schema rollback would be destroying data to undo a migration.
    op.drop_index("idx_artifact_conversation", table_name="agent_artifacts")
    op.drop_index("idx_artifact_owner_recent", table_name="agent_artifacts")
    op.drop_index("ix_agent_artifacts_created_at", table_name="agent_artifacts")
    op.drop_index("ix_agent_artifacts_conversation_id", table_name="agent_artifacts")
    op.drop_index("ix_agent_artifacts_user_id", table_name="agent_artifacts")
    op.drop_table("agent_artifacts")

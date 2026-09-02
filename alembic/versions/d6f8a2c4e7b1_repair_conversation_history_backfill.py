"""Repair query-history groups missing from the conversation domain.

The original conversation migration backfilled the rows that existed when it
ran. During the dual-write rollout, later flat-history rows could still be
created without a matching conversation. Account deletion also makes both
live owner ids nullable, so matching only ``c.user_id = h.user_id`` cannot see
anonymized history at all.

This data-only migration is idempotent. It imports every currently unmatched
owner/session group, preserving its messages and using historical ownership
when available.

Revision ID: d6f8a2c4e7b1
Revises: c5e7f9a1b3d4
"""

import json
import uuid
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "d6f8a2c4e7b1"
down_revision: Union[str, None] = "c5e7f9a1b3d4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    workspace_id = bind.execute(sa.text("""
        SELECT id FROM workspaces
        ORDER BY is_default DESC, created_at, id
        LIMIT 1
    """)).scalar()
    if workspace_id is None:
        raise RuntimeError("cannot repair conversation history without a workspace")

    groups = bind.execute(sa.text("""
        SELECT COALESCE(h.user_id, h.historical_user_id) AS owner_key,
               MAX(h.user_id) AS live_user_id,
               h.session_id,
               MIN(h.query_timestamp) AS started,
               MAX(COALESCE(h.response_timestamp, h.query_timestamp)) AS last_at,
               (ARRAY_AGG(h.query_text ORDER BY h.query_timestamp, h.id))[1]
                   AS first_query
        FROM user_query_history h
        WHERE NOT EXISTS (
            SELECT 1 FROM conversations c
            WHERE COALESCE(c.user_id, c.historical_user_id)
                      IS NOT DISTINCT FROM
                  COALESCE(h.user_id, h.historical_user_id)
              AND c.legacy_session_id IS NOT DISTINCT FROM h.session_id
        )
        GROUP BY COALESCE(h.user_id, h.historical_user_id), h.session_id
        ORDER BY started
    """)).fetchall()

    for (owner_key, live_user_id, session_id, started, last_at,
         first_query) in groups:
        username = None
        if owner_key is not None:
            username = bind.execute(sa.text("""
                SELECT username FROM users WHERE id = :owner
                UNION ALL
                SELECT username FROM deleted_users WHERE user_id = :owner
                LIMIT 1
            """), {"owner": owner_key}).scalar()

        conversation_id = uuid.uuid4()
        branch_id = uuid.uuid4()
        title = (first_query or "Imported history").strip()[:500]
        bind.execute(sa.text("""
            INSERT INTO conversations
                (id, workspace_id, user_id, historical_user_id,
                 author_username, title, legacy_session_id,
                 last_message_at, created_at, updated_at)
            VALUES
                (:id, :workspace, :user_id, :historical_user_id,
                 :author_username, :title, :session_id,
                 :last_at, :started, :last_at)
        """), {
            "id": conversation_id,
            "workspace": workspace_id,
            "user_id": live_user_id,
            "historical_user_id": owner_key,
            "author_username": username,
            "title": title or "Imported history",
            "session_id": session_id,
            "last_at": last_at,
            "started": started,
        })
        bind.execute(sa.text("""
            INSERT INTO conversation_branches
                (id, conversation_id, is_primary, created_at)
            VALUES (:id, :conversation_id, true, :started)
        """), {
            "id": branch_id,
            "conversation_id": conversation_id,
            "started": started,
        })

        history_rows = bind.execute(sa.text("""
            SELECT query_text, response_text, query_timestamp,
                   response_timestamp, success, error_message,
                   processing_time_ms, query_metadata
            FROM user_query_history
            WHERE COALESCE(user_id, historical_user_id)
                      IS NOT DISTINCT FROM :owner_key
              AND session_id IS NOT DISTINCT FROM :session_id
            ORDER BY query_timestamp, id
        """), {
            "owner_key": owner_key,
            "session_id": session_id,
        }).fetchall()

        sequence = 0
        for (query_text, response_text, query_at, response_at, success,
             error_message, processing_ms, metadata) in history_rows:
            sequence += 1
            bind.execute(sa.text("""
                INSERT INTO messages
                    (id, branch_id, role, sequence, content_blocks,
                     status, created_at)
                VALUES
                    (:id, :branch_id, 'user', :sequence,
                     CAST(:blocks AS jsonb), 'complete', :created_at)
            """), {
                "id": uuid.uuid4(),
                "branch_id": branch_id,
                "sequence": sequence,
                "blocks": json.dumps([
                    {"type": "text", "text": query_text or ""}
                ]),
                "created_at": query_at,
            })

            blocks = []
            if response_text:
                blocks.append({"type": "text", "text": response_text})
            if isinstance(metadata, dict) and metadata.get("sql"):
                blocks.append({"type": "sql", "sql": str(metadata["sql"])})
            if error_message:
                blocks.append({"type": "error", "message": str(error_message)})
            if not blocks:
                blocks.append({"type": "text", "text": ""})

            sequence += 1
            bind.execute(sa.text("""
                INSERT INTO messages
                    (id, branch_id, role, sequence, content_blocks,
                     status, processing_time_ms, created_at)
                VALUES
                    (:id, :branch_id, 'assistant', :sequence,
                     CAST(:blocks AS jsonb), :status, :processing_ms,
                     :created_at)
            """), {
                "id": uuid.uuid4(),
                "branch_id": branch_id,
                "sequence": sequence,
                "blocks": json.dumps(blocks),
                "status": "complete" if success else "failed",
                "processing_ms": processing_ms,
                "created_at": response_at or query_at,
            })


def downgrade() -> None:
    # This migration imports user history. Deleting those conversations on a
    # downgrade would destroy independently valuable data and cannot be done
    # safely once users have continued those threads.
    pass

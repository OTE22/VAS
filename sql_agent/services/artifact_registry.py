"""Artifact registry — the agent's record of documents it generated.

Two jobs, both of which exist so the agent can resolve "the last report"
without guessing:

  1. Persist a rendered document TRANSACTIONALLY. A row that points at a
     missing file, or a file no row knows about, is worse than no artifact:
     the first breaks every later reference, the second leaks surveillance
     output that retention will never clean up. So: render to a temp name,
     validate, atomically move into place, then insert the row — and unlink
     the file if the insert fails.

  2. Answer ownership questions from the DATABASE. Callers never decide who
     owns an artifact, and the planner LLM certainly does not: it may only
     name an id, which is then re-checked here.

Path safety follows the pattern established by backend/routes/webhook.py and
backend/ml/registry_service.py: a fixed realpath base, containment asserted
after resolution (never against an already-traversed directory), stored paths
that are relative and server-generated.
"""

import logging
import os
import uuid as uuid_mod
from datetime import datetime
from typing import List, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from config import settings
from db_models import AgentArtifact

logger = logging.getLogger(__name__)

# Only formats we render ourselves. The extension is never taken from a
# caller: it is derived from this map, so a stored path cannot end in
# something executable or unexpected.
_EXTENSIONS = {"pdf": ".pdf", "word": ".docx", "report": ".txt"}

# A rendered document that is empty is a failed render wearing a success mask.
_MIN_BYTES = 64
_MAX_BYTES = 64 * 1024 * 1024


class ArtifactError(Exception):
    """Registration failed; nothing was persisted."""


def artifacts_root() -> str:
    return os.path.realpath(settings.ARTIFACTS_DIR)


def _assert_inside_artifacts(path: str) -> str:
    """Resolve `path` and prove it is under ARTIFACTS_DIR.

    Anchored to a FIXED base and checked AFTER resolution — checking against a
    directory that was itself built from untrusted input is the bug this
    codebase has already been bitten by (see webhook.py's _debug_images_dir).
    """
    root = artifacts_root()
    resolved = os.path.realpath(path)
    if resolved != root and not resolved.startswith(root + os.sep):
        raise ArtifactError("artifact path escapes the artifacts directory")
    return resolved


def storage_path_for(artifact_id: uuid_mod.UUID, artifact_type: str) -> str:
    """Server-generated relative path: '<uuid>.<ext>'. Never client input."""
    ext = _EXTENSIONS.get(artifact_type)
    if not ext:
        raise ArtifactError(f"unsupported artifact type: {artifact_type!r}")
    return f"{artifact_id}{ext}"


def commit_bytes(artifact_id: uuid_mod.UUID, artifact_type: str, payload: bytes) -> str:
    """Write bytes into ARTIFACTS_DIR atomically. Returns the relative path.

    tmp in .incoming/ (same filesystem) -> fsync -> os.replace, so no reader
    ever sees a partial document under its final name.
    """
    if not payload or len(payload) < _MIN_BYTES:
        raise ArtifactError("refusing to store an empty or truncated document")
    if len(payload) > _MAX_BYTES:
        raise ArtifactError("rendered document exceeds the size limit")

    relative = storage_path_for(artifact_id, artifact_type)
    final_path = _assert_inside_artifacts(os.path.join(artifacts_root(), relative))
    temp_dir = settings.ARTIFACTS_TEMP_DIR
    os.makedirs(temp_dir, exist_ok=True)
    temp_path = os.path.join(temp_dir, f"{artifact_id}.part")

    try:
        with open(temp_path, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, final_path)
    except Exception:
        try:
            os.unlink(temp_path)
        except OSError:
            pass
        raise
    return relative


def delete_file(relative_path: str) -> None:
    """Remove a stored artifact file. Never raises."""
    try:
        path = _assert_inside_artifacts(os.path.join(artifacts_root(), relative_path))
        os.unlink(path)
    except (OSError, ArtifactError):
        pass


async def register_artifact(
    db: AsyncSession, *, payload: bytes, artifact_type: str, title: str,
    language: str, user_id: Optional[int], created_by_username: str,
    conversation_id=None, source_query: Optional[str] = None,
    source_sql: Optional[str] = None, source_content: Optional[str] = None,
    source_message_id=None, source_result_id: Optional[int] = None,
    modification_meta: Optional[dict] = None, parent_artifact_id=None,
) -> AgentArtifact:
    """Store the document and its lineage, or leave nothing behind.

    File first, row second: if the insert fails the file is unlinked, so the
    two never disagree.
    """
    artifact_id = uuid_mod.uuid4()
    relative = commit_bytes(artifact_id, artifact_type, payload)
    artifact = None      # bound before the try: the constructor can fail too

    try:
        artifact = AgentArtifact(
            id=artifact_id,
            user_id=user_id,
            created_by_username=(created_by_username or "unknown")[:255],
            conversation_id=conversation_id,
            type=artifact_type,
            title=(title or "Report")[:255],
            language=(language or "en")[:8],
            storage_path=relative,
            source_query=source_query,
            source_sql=source_sql,
            source_content=source_content,
            source_message_id=source_message_id,
            source_result_id=source_result_id,
            modification_meta=modification_meta,
            parent_artifact_id=parent_artifact_id,
            created_at=datetime.utcnow(),
        )
        db.add(artifact)
        await db.flush()
        logger.info("[ARTIFACT] registered id=%s type=%s lang=%s user_id=%s parent=%s",
                    artifact.id, artifact_type, language, user_id, parent_artifact_id)
        return artifact
    except Exception:
        # EXPUNGE BEFORE UNLINKING. db.add() made the row pending in the
        # caller's session, and db_manager.get_session() commits that session
        # on a clean exit — so a caller that merely catches this failure and
        # carries on ("sorry, I couldn't build the report") would still have
        # the row inserted at the end of the request, now pointing at a file
        # we are about to delete. A row without a file breaks every later
        # reference to it and is invisible to nothing: it is the exact state
        # this module exists to prevent.
        try:
            if artifact is not None and artifact in db:
                db.expunge(artifact)
        except Exception:
            pass          # never let cleanup mask the original failure
        delete_file(relative)
        raise


async def get_owned_artifact(db: AsyncSession, artifact_id, user_id: Optional[int]
                             ) -> Optional[AgentArtifact]:
    """The artifact, only if this user owns it and it is not deleted.

    Ownership is answered here, by the database. Callers — including anything
    driven by a planner's output — must route every artifact reference through
    this function rather than trusting an id they were handed.
    """
    if artifact_id is None or user_id is None:
        return None
    try:
        parsed = artifact_id if isinstance(artifact_id, uuid_mod.UUID) \
            else uuid_mod.UUID(str(artifact_id))
    except (ValueError, AttributeError, TypeError):
        return None       # a malformed id is simply not found

    row = (await db.execute(
        select(AgentArtifact).where(
            AgentArtifact.id == parsed,
            AgentArtifact.user_id == user_id,
            AgentArtifact.deleted_at.is_(None),
        )
    )).scalar_one_or_none()
    return row


async def get_artifact_source_sql(db: AsyncSession, user_id: Optional[int],
                                  limit: int = 3) -> dict:
    """{artifact_id: source_sql} for this user's recent documents.

    Deliberately a SEPARATE call from list_recent_artifacts. That one feeds a
    PROMPT and must never carry SQL; this one feeds "same report but only
    camera 3", where the originating query IS the answer — it goes to the
    modification node and nowhere near a model's context as raw provenance.

    Owner-scoped in the WHERE clause, so a map built here cannot contain
    another user's query no matter what id is later looked up in it.
    """
    if user_id is None:
        return {}
    stmt = (select(AgentArtifact.id, AgentArtifact.source_sql)
            .where(AgentArtifact.user_id == user_id,
                   AgentArtifact.deleted_at.is_(None),
                   AgentArtifact.source_sql.isnot(None))
            .order_by(AgentArtifact.created_at.desc())
            .limit(max(1, min(limit, 10))))
    return {str(row[0]): row[1] for row in (await db.execute(stmt)).all()}


async def list_recent_artifacts(db: AsyncSession, user_id: Optional[int],
                                conversation_id=None, limit: int = 3) -> List[dict]:
    """Compact, owner-scoped candidates for the reference resolver.

    Deliberately NOT the full row: `source_content` and `source_sql` carry
    surveillance data and must not travel into a prompt or an API response.
    """
    if user_id is None:
        return []
    # The five columns, NOT select(AgentArtifact): the whole-row version
    # dragged source_content — up to 500KB of report narrative per row —
    # across the wire on EVERY turn, including "hello", only to discard it.
    # The docstring above promises that column never travels; now it doesn't
    # even leave the database.
    stmt = select(AgentArtifact.id, AgentArtifact.type, AgentArtifact.title,
                  AgentArtifact.language, AgentArtifact.created_at).where(
        AgentArtifact.user_id == user_id,
        AgentArtifact.deleted_at.is_(None),
    )
    if conversation_id is not None:
        stmt = stmt.where(AgentArtifact.conversation_id == conversation_id)
    stmt = stmt.order_by(AgentArtifact.created_at.desc()).limit(max(1, min(limit, 10)))

    return [
        {
            "artifact_id": str(row.id),
            "type": row.type,
            "title": row.title,
            "language": row.language,
            "created_at": row.created_at.isoformat() + "Z" if row.created_at else None,
        }
        for row in (await db.execute(stmt)).all()
    ]

"""Conversation domain service: tenancy-scoped chat threads, branches, messages.

Boundary rules this module enforces (and routes must not re-implement):

* Every read and write is scoped to (user, workspace membership). A
  conversation id from the client is never trusted on its own — ownership is
  re-checked inside the same query that fetches the row, so there is no
  check-then-act gap.
* Messages carry TYPED content blocks (list of {"type": ...} dicts), never a
  single pre-rendered string.
* Deletion is soft (deleted_at); hard deletion belongs to retention jobs.
* The service takes an AsyncSession and does NOT commit unless the operation
  says so — write endpoints own their transaction explicitly, mirroring the
  update_user lesson: a 200 must mean the data is committed.
"""

import logging
import uuid as uuid_module
from datetime import datetime
from typing import List, Optional

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from db_models import (
    Conversation,
    ConversationBranch,
    Message,
    MessageFeedback,
    Workspace,
    WorkspaceMember,
)

logger = logging.getLogger(__name__)

# Block types the API accepts from clients. The backfill and server-side
# writers may use the full set; user-authored messages are text-only for now.
_ALLOWED_CLIENT_BLOCK_TYPES = {"text"}
_ALLOWED_BLOCK_TYPES = {"text", "sql", "result_table", "warning", "error", "citation"}


class ConversationAccessError(Exception):
    """Raised when the target does not exist OR the caller may not see it.

    One error for both cases on purpose: distinguishing "not found" from
    "forbidden" would let a caller probe which conversation ids exist.
    """


async def get_default_workspace_id(db: AsyncSession):
    """The seeded default workspace. Every user is enrolled by the migration."""
    ws_id = (await db.execute(
        select(Workspace.id).where(Workspace.is_default.is_(True)).limit(1)
    )).scalar()
    if ws_id is None:
        raise RuntimeError("No default workspace; migration d3e4f5a6b7c8 has not run")
    return ws_id


async def require_membership(db: AsyncSession, workspace_id, user_id: int) -> str:
    """Return the caller's workspace role or raise. The single tenancy gate."""
    role = (await db.execute(
        select(WorkspaceMember.role).where(
            WorkspaceMember.workspace_id == workspace_id,
            WorkspaceMember.user_id == user_id,
        )
    )).scalar()
    if role is None:
        raise ConversationAccessError("workspace access denied")
    return role


async def ensure_membership(db: AsyncSession, workspace_id, user_id: int,
                            role: Optional[str] = None) -> None:
    """Enroll a user created after the migration ran. Idempotent.

    Same rule as migration d3e4f5a6b7c8 applied to the users that existed
    then: platform admins RUN the workspace (role `admin`), everyone else is a
    `member`. Without this a bootstrap administrator created on a fresh
    database would be a plain member and could not see orphaned (deleted-user)
    conversations — the admin-readable guarantee would silently not hold on
    every fresh install. `role` may be passed explicitly; otherwise it is
    derived from users.role.
    """
    existing = (await db.execute(
        select(WorkspaceMember.id).where(
            WorkspaceMember.workspace_id == workspace_id,
            WorkspaceMember.user_id == user_id,
        )
    )).scalar()
    if existing is None:
        if role is None:
            from db_models import User
            platform_role = (await db.execute(
                select(User.role).where(User.id == user_id))).scalar()
            role = "admin" if platform_role == "admin" else "member"
        db.add(WorkspaceMember(workspace_id=workspace_id, user_id=user_id, role=role))
        # Flush now: the session maker sets autoflush=False, so without this
        # the membership check that follows in the same request would SELECT
        # right past the pending row and deny the user we just enrolled.
        await db.flush()


async def _owned_conversation(db: AsyncSession, conversation_id, user_id: int,
                              include_deleted: bool = False) -> Conversation:
    """Fetch a conversation the caller owns, or raise. Ownership in the WHERE.

    Deliberately strict about orphans: `Conversation.user_id == user_id` can
    never match a NULL user_id, so a deleted user's conversation is not owned
    by anyone. Every MUTATING path (rename, flags, delete, append, branch)
    resolves through here, which is what makes orphaned history immutable.
    Read access for workspace admins goes through
    _readable_historical_conversation below — a separate gate on purpose, so
    historical visibility can never widen into mutation rights.
    """
    query = select(Conversation).where(
        Conversation.id == conversation_id,
        Conversation.user_id == user_id,
    )
    if not include_deleted:
        query = query.where(Conversation.deleted_at.is_(None))
    conversation = (await db.execute(query)).scalars().first()
    if conversation is None:
        raise ConversationAccessError("conversation not found")
    await require_membership(db, conversation.workspace_id, user_id)
    return conversation


async def _readable_historical_conversation(db: AsyncSession, conversation_id,
                                            user_id: int) -> Conversation:
    """READ-ONLY access to an orphaned conversation (owner deleted).

    Matches only `user_id IS NULL` rows — a live user's conversation can never
    be reached this way — and requires the caller to hold the `admin` role in
    that conversation's workspace, so ordinary members see nothing and a
    cross-workspace admin fails the membership check. Called exclusively from
    GET paths; no mutating handler resolves through here.
    """
    conversation = (await db.execute(
        select(Conversation).where(
            Conversation.id == conversation_id,
            Conversation.user_id.is_(None),
            Conversation.deleted_at.is_(None),
        )
    )).scalars().first()
    if conversation is None:
        raise ConversationAccessError("conversation not found")
    role = await require_membership(db, conversation.workspace_id, user_id)
    if role != "admin":
        raise ConversationAccessError("conversation not found")
    return conversation


async def _readable_conversation(db: AsyncSession, conversation_id,
                                 user_id: int) -> Conversation:
    """Owner access first; workspace-admin historical read as the fallback."""
    try:
        return await _owned_conversation(db, conversation_id, user_id)
    except ConversationAccessError:
        return await _readable_historical_conversation(db, conversation_id, user_id)


def _validate_blocks(blocks, allowed=_ALLOWED_BLOCK_TYPES) -> List[dict]:
    if not isinstance(blocks, list) or not blocks:
        raise ValueError("content_blocks must be a non-empty list")
    for block in blocks:
        if not isinstance(block, dict) or block.get("type") not in allowed:
            raise ValueError(f"unsupported content block: {block!r:.80}")
    return blocks


def _conversation_dict(c: Conversation) -> dict:
    return {
        "id": str(c.id),
        "workspace_id": str(c.workspace_id),
        "title": c.title,
        "pinned": c.pinned,
        "archived": c.archived,
        "last_message_at": c.last_message_at.isoformat() + "Z" if c.last_message_at else None,
        "created_at": c.created_at.isoformat() + "Z" if c.created_at else None,
        # NULL user_id = the owner's account was deleted. author_username is
        # the denormalized attribution stamped at deletion; the UI renders
        # "Deleted User (<name>)" and treats the conversation as read-only.
        "orphaned": c.user_id is None,
        "author_username": c.author_username if c.user_id is None else None,
    }


def _message_dict(m: Message) -> dict:
    return {
        "id": str(m.id),
        "branch_id": str(m.branch_id),
        "role": m.role,
        "sequence": m.sequence,
        "content_blocks": m.content_blocks or [],
        "status": m.status,
        "model_provider": m.model_provider,
        "model_name": m.model_name,
        "processing_time_ms": m.processing_time_ms,
        "created_at": m.created_at.isoformat() + "Z" if m.created_at else None,
    }


# ---------------------------------------------------------------------------
# Conversation lifecycle
# ---------------------------------------------------------------------------

async def list_conversations(db: AsyncSession, user_id: int, workspace_id,
                             include_archived: bool = False,
                             limit: int = 50, offset: int = 0,
                             search: Optional[str] = None) -> List[dict]:
    role = await require_membership(db, workspace_id, user_id)
    # Owner-only listing — except that workspace ADMINS also see orphaned
    # conversations (owner deleted, user_id IS NULL), read-only. Members never
    # do: `user_id == user_id` cannot match NULL.
    if role == "admin":
        from sqlalchemy import or_ as _or
        owner_filter = _or(Conversation.user_id == user_id,
                           Conversation.user_id.is_(None))
    else:
        owner_filter = Conversation.user_id == user_id
    query = (
        select(Conversation)
        .where(
            Conversation.workspace_id == workspace_id,
            owner_filter,
            Conversation.deleted_at.is_(None),
        )
        .order_by(Conversation.pinned.desc(),
                  Conversation.last_message_at.desc().nulls_last())
        .limit(min(limit, 200)).offset(max(offset, 0))
    )
    if not include_archived:
        query = query.where(Conversation.archived.is_(False))
    if search and search.strip():
        # Title match OR any message in the conversation whose typed blocks
        # contain the term. content_blocks::text over-matches slightly (it
        # also sees the block "type" keys), which is acceptable for search;
        # the parameter is bound, never interpolated. Scoping to the OWNER's
        # conversations happens in the outer WHERE, so search cannot widen
        # visibility.
        from sqlalchemy import cast, exists, or_
        from sqlalchemy.dialects.postgresql import TEXT

        term = f"%{search.strip()[:200]}%"
        message_match = exists(
            select(Message.id)
            .join(ConversationBranch, Message.branch_id == ConversationBranch.id)
            .where(ConversationBranch.conversation_id == Conversation.id,
                   cast(Message.content_blocks, TEXT).ilike(term))
        )
        query = query.where(or_(Conversation.title.ilike(term), message_match))
    rows = (await db.execute(query)).scalars().all()
    return [_conversation_dict(c) for c in rows]


async def create_conversation(db: AsyncSession, user_id: int, workspace_id,
                              title: Optional[str] = None) -> dict:
    await require_membership(db, workspace_id, user_id)
    conversation = Conversation(
        workspace_id=workspace_id, user_id=user_id,
        title=(title or "New conversation").strip()[:500] or "New conversation",
    )
    db.add(conversation)
    await db.flush()
    branch = ConversationBranch(conversation_id=conversation.id, is_primary=True)
    db.add(branch)
    await db.flush()
    result = _conversation_dict(conversation)
    result["primary_branch_id"] = str(branch.id)
    return result


async def rename_conversation(db: AsyncSession, user_id: int, conversation_id,
                              title: str) -> dict:
    conversation = await _owned_conversation(db, conversation_id, user_id)
    conversation.title = (title or "").strip()[:500] or conversation.title
    conversation.updated_at = datetime.utcnow()
    return _conversation_dict(conversation)


async def set_conversation_flags(db: AsyncSession, user_id: int, conversation_id,
                                 pinned: Optional[bool] = None,
                                 archived: Optional[bool] = None) -> dict:
    conversation = await _owned_conversation(db, conversation_id, user_id)
    if pinned is not None:
        conversation.pinned = bool(pinned)
    if archived is not None:
        conversation.archived = bool(archived)
    conversation.updated_at = datetime.utcnow()
    return _conversation_dict(conversation)


async def soft_delete_conversation(db: AsyncSession, user_id: int,
                                   conversation_id) -> None:
    conversation = await _owned_conversation(db, conversation_id, user_id)
    conversation.deleted_at = datetime.utcnow()


# ---------------------------------------------------------------------------
# Branches + messages
# ---------------------------------------------------------------------------

async def get_primary_branch_id(db: AsyncSession, conversation_id):
    return (await db.execute(
        select(ConversationBranch.id)
        .where(ConversationBranch.conversation_id == conversation_id,
               ConversationBranch.is_primary.is_(True))
        .limit(1)
    )).scalar()


async def list_branches(db: AsyncSession, user_id: int, conversation_id) -> List[dict]:
    # Read path: owner, or workspace admin for an orphaned conversation.
    await _readable_conversation(db, conversation_id, user_id)
    rows = (await db.execute(
        select(ConversationBranch)
        .where(ConversationBranch.conversation_id == conversation_id)
        .order_by(ConversationBranch.created_at)
    )).scalars().all()
    return [{
        "id": str(b.id),
        "parent_branch_id": str(b.parent_branch_id) if b.parent_branch_id else None,
        "forked_from_message_id": str(b.forked_from_message_id) if b.forked_from_message_id else None,
        "name": b.name,
        "is_primary": b.is_primary,
        "created_at": b.created_at.isoformat() + "Z" if b.created_at else None,
    } for b in rows]


async def get_messages(db: AsyncSession, user_id: int, conversation_id,
                       branch_id=None, limit: int = 100, before_sequence: Optional[int] = None) -> dict:
    """Messages for one branch, oldest-first, paginated by sequence."""
    # Read path: owner, or workspace admin for an orphaned conversation.
    await _readable_conversation(db, conversation_id, user_id)
    if branch_id is None:
        branch_id = await get_primary_branch_id(db, conversation_id)
        if branch_id is None:
            return {"branch_id": None, "messages": []}
    else:
        owner = (await db.execute(
            select(ConversationBranch.conversation_id)
            .where(ConversationBranch.id == branch_id)
        )).scalar()
        # Compare as strings: ids arrive as str from the API and as UUID from
        # the ORM, and a type mismatch here would silently deny every branch.
        if owner is None or str(owner) != str(conversation_id):
            raise ConversationAccessError("branch not found")

    query = (
        select(Message)
        .where(Message.branch_id == branch_id)
        .order_by(Message.sequence.desc())
        .limit(min(limit, 200))
    )
    if before_sequence is not None:
        query = query.where(Message.sequence < before_sequence)
    rows = (await db.execute(query)).scalars().all()
    rows.reverse()  # newest page fetched, returned oldest-first
    return {"branch_id": str(branch_id), "messages": [_message_dict(m) for m in rows]}


async def _next_sequence(db: AsyncSession, branch_id) -> int:
    current = (await db.execute(
        select(func.max(Message.sequence)).where(Message.branch_id == branch_id)
    )).scalar()
    return (current or 0) + 1


async def append_exchange(db: AsyncSession, user_id: int, conversation_id,
                          user_text: str, assistant_blocks: List[dict],
                          success: bool = True,
                          processing_time_ms: Optional[float] = None,
                          model_provider: Optional[str] = None,
                          model_name: Optional[str] = None) -> dict:
    """Append a user turn + assistant turn atomically to the primary branch."""
    conversation = await _owned_conversation(db, conversation_id, user_id)
    branch_id = await get_primary_branch_id(db, conversation_id)
    if branch_id is None:
        branch = ConversationBranch(conversation_id=conversation_id, is_primary=True)
        db.add(branch)
        await db.flush()
        branch_id = branch.id

    _validate_blocks(assistant_blocks)
    seq = await _next_sequence(db, branch_id)
    user_message = Message(branch_id=branch_id, role="user", sequence=seq,
                           content_blocks=[{"type": "text", "text": user_text or ""}])
    assistant_message = Message(branch_id=branch_id, role="assistant", sequence=seq + 1,
                                content_blocks=assistant_blocks,
                                status="complete" if success else "failed",
                                processing_time_ms=processing_time_ms,
                                model_provider=model_provider, model_name=model_name)
    db.add_all([user_message, assistant_message])
    conversation.last_message_at = datetime.utcnow()
    if conversation.title == "New conversation" and (user_text or "").strip():
        conversation.title = user_text.strip()[:120]
    await db.flush()
    return {"user_message_id": str(user_message.id),
            "assistant_message_id": str(assistant_message.id),
            "branch_id": str(branch_id)}


async def branch_from_message(db: AsyncSession, user_id: int, conversation_id,
                              message_id, new_user_text: str) -> dict:
    """Edit an earlier user message: fork a branch at that point.

    Copies every message BEFORE the edited one into a new branch, then appends
    the edited user message. The original branch is untouched — both timelines
    remain navigable, which is the whole point of branching over overwriting.
    """
    await _owned_conversation(db, conversation_id, user_id)

    source = (await db.execute(
        select(Message).join(ConversationBranch, Message.branch_id == ConversationBranch.id)
        .where(Message.id == message_id,
               ConversationBranch.conversation_id == conversation_id)
    )).scalars().first()
    if source is None:
        raise ConversationAccessError("message not found")
    if source.role != "user":
        raise ValueError("only user messages can be edited into a branch")

    new_branch = ConversationBranch(
        conversation_id=conversation_id,
        parent_branch_id=source.branch_id,
        forked_from_message_id=source.id,
    )
    db.add(new_branch)
    await db.flush()

    prefix = (await db.execute(
        select(Message).where(Message.branch_id == source.branch_id,
                              Message.sequence < source.sequence)
        .order_by(Message.sequence)
    )).scalars().all()
    for original in prefix:
        db.add(Message(branch_id=new_branch.id, role=original.role,
                       sequence=original.sequence,
                       content_blocks=original.content_blocks,
                       status=original.status,
                       model_provider=original.model_provider,
                       model_name=original.model_name,
                       processing_time_ms=original.processing_time_ms,
                       created_at=original.created_at))

    edited = Message(branch_id=new_branch.id, role="user", sequence=source.sequence,
                     content_blocks=[{"type": "text", "text": new_user_text or ""}],
                     edited_from_message_id=source.id)
    db.add(edited)
    await db.flush()
    return {"branch_id": str(new_branch.id), "edited_message_id": str(edited.id)}


async def record_feedback(db: AsyncSession, user_id: int, conversation_id,
                          message_id, rating: int, comment: Optional[str]) -> None:
    """Upsert one feedback row per (message, user)."""
    if rating not in (1, -1):
        raise ValueError("rating must be +1 or -1")
    await _owned_conversation(db, conversation_id, user_id)
    owned = (await db.execute(
        select(Message.id).join(ConversationBranch, Message.branch_id == ConversationBranch.id)
        .where(Message.id == message_id,
               ConversationBranch.conversation_id == conversation_id)
    )).scalar()
    if owned is None:
        raise ConversationAccessError("message not found")

    existing = (await db.execute(
        select(MessageFeedback).where(MessageFeedback.message_id == message_id,
                                      MessageFeedback.user_id == user_id)
    )).scalars().first()
    if existing:
        existing.rating = rating
        existing.comment = (comment or None)
    else:
        db.add(MessageFeedback(message_id=message_id, user_id=user_id,
                               rating=rating, comment=comment or None))


async def verify_conversation_owner(db: AsyncSession, conversation_id,
                                    user_id: int) -> bool:
    """True when the user owns this live conversation.

    Used by the SQL-agent stream BEFORE it starts: a client-supplied
    conversation_id is never trusted, and rejecting up front beats streaming a
    whole answer into a conversation the caller cannot read back.
    """
    try:
        await _owned_conversation(db, conversation_id, user_id)
        return True
    except ConversationAccessError:
        return False


# ---------------------------------------------------------------------------
# Bridge from the streaming path (dual-write)
# ---------------------------------------------------------------------------

async def record_exchange_for_session(db: AsyncSession, user_id: int,
                                      session_id: Optional[str],
                                      user_text: str,
                                      assistant_blocks: List[dict],
                                      success: bool,
                                      processing_time_ms: Optional[float],
                                      conversation_id=None) -> Optional[dict]:
    """Append an exchange to an EXPLICIT conversation, or locate one by the
    legacy session id.

    conversation_id wins when given (the client selected a conversation in the
    sidebar); ownership is re-checked here even though the route validated it
    pre-stream, because the conversation can be deleted while the answer was
    generating. On any access failure it falls back to the session path so the
    exchange is recorded SOMEWHERE the user can see rather than lost.
    """
    if conversation_id is not None:
        try:
            return await append_exchange(
                db, user_id, conversation_id, user_text, assistant_blocks,
                success=success, processing_time_ms=processing_time_ms,
            )
        except ConversationAccessError:
            logger.warning(
                "[CONV] target conversation vanished mid-stream for user_id=%s; "
                "falling back to session placement", user_id,
            )

    workspace_id = await get_default_workspace_id(db)
    await ensure_membership(db, workspace_id, user_id)

    conversation = None
    if session_id:
        conversation = (await db.execute(
            select(Conversation).where(
                Conversation.user_id == user_id,
                Conversation.legacy_session_id == session_id,
                Conversation.deleted_at.is_(None),
            ).limit(1)
        )).scalars().first()
    if conversation is None:
        conversation = Conversation(
            workspace_id=workspace_id, user_id=user_id,
            title=(user_text or "New conversation").strip()[:120] or "New conversation",
            legacy_session_id=session_id,
        )
        db.add(conversation)
        await db.flush()
        db.add(ConversationBranch(conversation_id=conversation.id, is_primary=True))
        await db.flush()

    return await append_exchange(
        db, user_id, conversation.id, user_text, assistant_blocks,
        success=success, processing_time_ms=processing_time_ms,
    )

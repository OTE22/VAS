"""/api/v1/conversations — the conversation domain API.

Versioned from day one (the first /api/v1 surface in this codebase) so the
contract can evolve without breaking the legacy /api/sql-agent/history reads,
which stay in place during rollout.

Authorization model:
* Reads require CHATBOT_HISTORY_READ, deletes CHATBOT_HISTORY_DELETE,
  everything else CHATBOT_USE — the same capability vocabulary every other
  gate uses (backend/auth/capabilities.py).
* Tenancy (workspace membership + ownership) is enforced INSIDE the service
  queries, never from client-supplied ids alone.
* "Not found" and "not yours" are the same 404, so ids cannot be probed.
* Mutating routes carry the standard cookie-CSRF dependency (Bearer exempt).
* Every write commits BEFORE the 200 is returned — the update_user lesson.
"""

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from backend.auth.auth_service import require_capability
from backend.auth.capabilities import Capability
from backend.services import conversation_service as svc
from backend.services.conversation_service import ConversationAccessError
from backend.core.rate_limiter import rate_limited
from db_connection import get_db
from backend.utils.pagination import resolve_page, resolve_page_size
from db_models import User

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1", tags=["Conversations"])


def require_conversation_csrf(request: Request):
    """Cookie-auth mutations need the custom header; Bearer is exempt.

    Same policy as require_user_admin_csrf / require_sql_agent_csrf.
    """
    if request.headers.get("authorization"):
        return
    if request.headers.get("x-requested-with", "").lower() != "xmlhttprequest":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="CSRF check failed: X-Requested-With header required",
        )


def _not_found() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail={"error_code": "CONVERSATION_NOT_FOUND",
                "message": "Conversation not found."},
    )


def _parse_uuid(raw: str):
    """Coerce a client-supplied id, treating garbage as not-found.

    A malformed id gets the same 404 as a nonexistent one: it is not a server
    error, and answering differently would leak which strings are valid ids.
    """
    import uuid as uuid_module
    try:
        return uuid_module.UUID(str(raw))
    except (ValueError, AttributeError, TypeError):
        raise _not_found()


class CreateConversationRequest(BaseModel):
    title: Optional[str] = Field(default=None, max_length=500)


class RenameRequest(BaseModel):
    title: str = Field(min_length=1, max_length=500)


class FlagsRequest(BaseModel):
    pinned: Optional[bool] = None
    archived: Optional[bool] = None


class BranchRequest(BaseModel):
    message_id: str
    new_text: str = Field(min_length=1, max_length=20000)


class FeedbackRequest(BaseModel):
    message_id: str
    rating: int
    comment: Optional[str] = Field(default=None, max_length=2000)


@router.get("/conversations")
async def list_conversations(
    include_archived: bool = False,
    limit: int = None,
    offset: int = 0,
    q: Optional[str] = None,
    current_user: User = Depends(require_capability(Capability.CHATBOT_HISTORY_READ)),
    db: AsyncSession = Depends(get_db),
):
    # Bounds from configuration; this used to accept any limit a caller sent.
    """The caller's own conversations in the default workspace, pinned first then most recently active, with optional archived inclusion and substring search over titles and message content."""
    limit = resolve_page_size(limit, field="limit")
    workspace_id = await svc.get_default_workspace_id(db)
    await svc.ensure_membership(db, workspace_id, current_user.id)
    await db.commit()
    conversations = await svc.list_conversations(
        db, current_user.id, workspace_id,
        include_archived=include_archived, limit=limit, offset=offset,
        search=q,
    )
    return {"workspace_id": str(workspace_id), "conversations": conversations}


@router.post("/conversations", dependencies=[Depends(require_conversation_csrf)])
async def create_conversation(
    body: CreateConversationRequest,
    current_user: User = Depends(require_capability(Capability.CHATBOT_USE)),
    db: AsyncSession = Depends(get_db),
    _rl: None = Depends(rate_limited("chatbot", heavy=False)),
):
    """Create a conversation and its primary branch; returns the conversation with primary_branch_id."""
    workspace_id = await svc.get_default_workspace_id(db)
    await svc.ensure_membership(db, workspace_id, current_user.id)
    conversation = await svc.create_conversation(db, current_user.id, workspace_id, body.title)
    await db.commit()
    return conversation


@router.get("/conversations/{conversation_id}/messages")
async def get_messages(
    conversation_id: str,
    branch_id: Optional[str] = None,
    limit: int = None,
    before_sequence: Optional[int] = None,
    current_user: User = Depends(require_capability(Capability.CHATBOT_HISTORY_READ)),
    db: AsyncSession = Depends(get_db),
):
    """Messages of one branch (the primary branch when branch_id is omitted), oldest first, cursor-paginated via before_sequence. A conversation that is not the caller's own is indistinguishable from a missing one: 404 CONVERSATION_NOT_FOUND."""
    limit = resolve_page_size(limit, field="limit")
    try:
        return await svc.get_messages(
            db, current_user.id, _parse_uuid(conversation_id),
            branch_id=_parse_uuid(branch_id) if branch_id else None,
            limit=limit, before_sequence=before_sequence)
    except ConversationAccessError:
        raise _not_found()


@router.get("/conversations/{conversation_id}/branches")
async def list_branches(
    conversation_id: str,
    current_user: User = Depends(require_capability(Capability.CHATBOT_HISTORY_READ)),
    db: AsyncSession = Depends(get_db),
):
    """All branches of the conversation in creation order, each with its fork point and primary flag."""
    try:
        return {"branches": await svc.list_branches(db, current_user.id, _parse_uuid(conversation_id))}
    except ConversationAccessError:
        raise _not_found()


@router.patch("/conversations/{conversation_id}",
              dependencies=[Depends(require_conversation_csrf)])
async def rename_conversation(
    conversation_id: str,
    body: RenameRequest,
    current_user: User = Depends(require_capability(Capability.CHATBOT_USE)),
    db: AsyncSession = Depends(get_db),
):
    """Rename the caller's conversation. An all-whitespace title leaves the existing one unchanged."""
    try:
        result = await svc.rename_conversation(db, current_user.id, _parse_uuid(conversation_id), body.title)
        await db.commit()
        return result
    except ConversationAccessError:
        raise _not_found()


@router.patch("/conversations/{conversation_id}/flags",
              dependencies=[Depends(require_conversation_csrf)])
async def set_flags(
    conversation_id: str,
    body: FlagsRequest,
    current_user: User = Depends(require_capability(Capability.CHATBOT_USE)),
    db: AsyncSession = Depends(get_db),
):
    """Set the pinned and/or archived flags; omitted fields are left unchanged."""
    try:
        result = await svc.set_conversation_flags(
            db, current_user.id, _parse_uuid(conversation_id),
            pinned=body.pinned, archived=body.archived,
        )
        await db.commit()
        return result
    except ConversationAccessError:
        raise _not_found()


@router.delete("/conversations/{conversation_id}",
               dependencies=[Depends(require_conversation_csrf)])
async def delete_conversation(
    conversation_id: str,
    current_user: User = Depends(require_capability(Capability.CHATBOT_HISTORY_DELETE)),
    db: AsyncSession = Depends(get_db),
):
    """Soft delete: stamps deleted_at and hides the conversation from listings. No rows are removed."""
    try:
        await svc.soft_delete_conversation(db, current_user.id, _parse_uuid(conversation_id))
        await db.commit()
        return {"success": True, "deleted_id": conversation_id}
    except ConversationAccessError:
        raise _not_found()


@router.post("/conversations/{conversation_id}/branches",
             dependencies=[Depends(require_conversation_csrf)])
async def branch_from_message(
    conversation_id: str,
    body: BranchRequest,
    current_user: User = Depends(require_capability(Capability.CHATBOT_USE)),
    db: AsyncSession = Depends(get_db),
    _rl: None = Depends(rate_limited("chatbot", heavy=False)),
):
    """Fork the conversation at an earlier user message: copies the history before that message into a new branch and appends the edited text. The original branch is unmodified. Editing a non-user message is 400 INVALID_BRANCH_REQUEST."""
    try:
        result = await svc.branch_from_message(
            db, current_user.id, _parse_uuid(conversation_id),
            _parse_uuid(body.message_id), body.new_text,
        )
        await db.commit()
        return result
    except ConversationAccessError:
        raise _not_found()
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error_code": "INVALID_BRANCH_REQUEST", "message": str(exc)},
        )


@router.post("/conversations/{conversation_id}/feedback",
             dependencies=[Depends(require_conversation_csrf)])
async def record_feedback(
    conversation_id: str,
    body: FeedbackRequest,
    current_user: User = Depends(require_capability(Capability.CHATBOT_USE)),
    db: AsyncSession = Depends(get_db),
):
    """Rate a message +1/-1 with an optional comment. One feedback row per (message, user) — repeated calls update it. Any other rating is 400 INVALID_FEEDBACK."""
    try:
        await svc.record_feedback(db, current_user.id, _parse_uuid(conversation_id),
                                  _parse_uuid(body.message_id), body.rating, body.comment)
        await db.commit()
        return {"success": True}
    except ConversationAccessError:
        raise _not_found()
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error_code": "INVALID_FEEDBACK", "message": str(exc)},
        )

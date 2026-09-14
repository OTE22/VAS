"""VAS -> LAF-AI (armyeye-chatbot) single sign-on hand-off.

    TRACKING  ->  GET /api/sso/laf-ai/launch  (signed-in user with Chatbot access)
              ->  303 https://armyeye-chatbot/auth/vas?ticket=<one-time ticket>
              ->  the chatbot's gate calls POST /api/sso/laf-ai/consume, server to
                  server, and receives the identity plus a fresh access token
                  for that user; that token backs its session from then on.

The ticket lives 60 s, is redeemed exactly once, carries no password and is
worthless to anyone but the gate (the consume endpoint is internal-only:
see `laf_ai_sso.came_through_public_proxy`). The minted token is linked to
the browser's VAS session so that VAS logout ends the chatbot session too.
"""
import logging
from datetime import datetime, timezone
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from config import settings
from db_connection import get_db
from db_models import User
from backend.auth import auth_security
from backend.auth import laf_ai_sso as sso
from backend.auth.auth_service import AuthService, require_chatbot_access
from backend.auth.capabilities import Capability, resolve_effective_authorization
from backend.routes.auth import require_auth_csrf

logger = logging.getLogger(__name__)
router = APIRouter(tags=["SSO"])

_NO_STORE = {"Cache-Control": "no-store", "Pragma": "no-cache"}


class TicketResponse(BaseModel):
    ticket: str
    launch_url: str
    expires_in: int


class ConsumeRequest(BaseModel):
    ticket: str = Field(min_length=1, max_length=512)


class ConsumeResponse(BaseModel):
    user_id: int
    username: str
    display_name: Optional[str] = None
    role: str
    permissions: List[str]
    issued_at: str
    expires_at: str
    # Server-to-server only: the gate keeps this in its own session store and
    # validates it against /api/auth/me on every request. It never reaches a
    # browser, a query string or a log.
    access_token: str
    token_type: str = "bearer"
    jti: str
    parent_jti: Optional[str] = None
    authentication_source: str = "vas_sso"


def _presented_token(request: Request) -> Optional[str]:
    token = auth_security.read_auth_cookie(request)
    if not token:
        header = request.headers.get("authorization", "")
        if header.lower().startswith("bearer "):
            token = header[7:].strip()
    return token


def _parent_jti(request: Request) -> Optional[str]:
    token = _presented_token(request)
    if not token:
        return None
    payload = AuthService.decode_token(token, verify_revocation=False) or {}
    return payload.get("jti")


def _navigation_from_vas(request: Request) -> bool:
    """A cross-site page must not be able to launch a hand-off for a victim's
    session (login CSRF into the chatbot). Browsers say where a navigation came
    from; requests without the header (older clients, curl) are allowed."""
    site = request.headers.get("sec-fetch-site", "").lower()
    return site in ("", "none", "same-origin", "same-site")


async def _issue(request: Request, user: User) -> TicketResponse:
    ticket = await sso.issue_ticket(
        user_id=user.id, username=user.username, display_name=user.full_name,
        role=user.role, parent_jti=_parent_jti(request),
    )
    return TicketResponse(
        ticket=ticket,
        launch_url=sso.chatbot_url("/auth/vas?ticket=" + ticket),
        expires_in=int(settings.LAF_AI_SSO_TICKET_TTL_SECONDS),
    )


@router.get("/api/sso/laf-ai/launch")
async def launch(request: Request, current_user: User = Depends(require_chatbot_access())):
    """What TRACKING points at: mint a ticket and send the browser to the chatbot."""
    request_id = getattr(request.state, "request_id", "-")
    ip = auth_security.client_ip(request)
    if not settings.LAF_AI_SSO_ENABLED:
        return RedirectResponse("/tracking-people", status_code=status.HTTP_303_SEE_OTHER, headers=_NO_STORE)
    if not _navigation_from_vas(request):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Cross-site launch rejected")
    try:
        issued = await _issue(request, current_user)
    except sso.SSOUnavailable:
        auth_security.audit("laf_ai_sso_ticket", result="unavailable", request_id=request_id,
                            user_id=current_user.id, ip=ip)
        # Redis is down: the chatbot's own sign-in page still works.
        return RedirectResponse(sso.chatbot_url("/login?sso=unavailable"),
                                status_code=status.HTTP_303_SEE_OTHER, headers=_NO_STORE)
    auth_security.audit("laf_ai_sso_ticket", result="success", request_id=request_id,
                        user_id=current_user.id, ip=ip)
    return RedirectResponse(issued.launch_url, status_code=status.HTTP_303_SEE_OTHER,
                            headers={**_NO_STORE, "Referrer-Policy": "no-referrer"})


@router.post("/api/sso/laf-ai/ticket", response_model=TicketResponse)
async def create_ticket(request: Request, current_user: User = Depends(require_chatbot_access()),
                        _csrf: None = Depends(require_auth_csrf)):
    """Same ticket as `launch`, returned as JSON for scripted clients and tests."""
    if not settings.LAF_AI_SSO_ENABLED:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="LAF-AI sign-on is disabled")
    request_id = getattr(request.state, "request_id", "-")
    try:
        issued = await _issue(request, current_user)
    except sso.SSOUnavailable:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                            detail="Sign-on temporarily unavailable")
    auth_security.audit("laf_ai_sso_ticket", result="success", request_id=request_id,
                        user_id=current_user.id, ip=auth_security.client_ip(request))
    return issued


@router.post("/api/sso/laf-ai/consume", response_model=ConsumeResponse)
async def consume(body: ConsumeRequest, request: Request, db: AsyncSession = Depends(get_db)):
    """Redeem a ticket exactly once. Internal to the chatbot gate: calls that
    came through the public proxy are refused, as is a missing shared secret
    when one is configured. Identity is re-read from the database at
    consumption time, so a permission withdrawn in the last 60 s still wins."""
    request_id = getattr(request.state, "request_id", "-")
    ip = auth_security.client_ip(request)
    if not settings.LAF_AI_SSO_ENABLED:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="LAF-AI sign-on is disabled")
    if sso.came_through_public_proxy(request) or not sso.presented_secret_ok(request):
        auth_security.audit("laf_ai_sso_consume", result="failure", failure_code="NOT_INTERNAL",
                            request_id=request_id, ip=ip)
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                            detail="Ticket consumption is internal to the chatbot gate")
    try:
        payload = await sso.consume_ticket(body.ticket)
    except sso.SSOUnavailable:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                            detail="Sign-on temporarily unavailable")
    if payload is None:
        auth_security.audit("laf_ai_sso_consume", result="failure", failure_code="INVALID_TICKET",
                            request_id=request_id, ip=ip)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                            detail="Invalid, expired or already used ticket")

    user = (await db.execute(select(User).where(User.id == int(payload["user_id"])))).scalar_one_or_none()
    if user is None or not user.is_active:
        auth_security.audit("laf_ai_sso_consume", result="failure", failure_code="ACCOUNT_UNAVAILABLE",
                            request_id=request_id, user_id=payload.get("user_id"), ip=ip)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Account is not available")
    authz = resolve_effective_authorization(user)
    if not authz.has(Capability.CHATBOT_USE):
        auth_security.audit("laf_ai_sso_consume", result="failure", failure_code="CHATBOT_ACCESS_DENIED",
                            request_id=request_id, user_id=user.id, ip=ip)
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Chatbot access denied")
    if bool(getattr(user, "must_change_password", False)):
        auth_security.audit("laf_ai_sso_consume", result="failure", failure_code="ROTATION_REQUIRED",
                            request_id=request_id, user_id=user.id, ip=ip)
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Password change required")

    # Same claims as a password sign-in; the token honours every existing rule
    # (revocation denylist, password-change stamp, inactive account).
    access_token = AuthService.create_access_token(
        data={"sub": str(user.id), "username": user.username, "role": user.role})
    claims = AuthService.decode_token(access_token, verify_revocation=False) or {}
    issued_at = int(claims.get("iat") or 0)
    expires_at = int(claims.get("exp") or 0)
    await sso.link_child_session(payload.get("parent_jti"), claims.get("jti"),
                                 max(60, expires_at - issued_at))
    auth_security.audit("laf_ai_sso_consume", result="success", request_id=request_id,
                        user_id=user.id, ip=ip)
    return ConsumeResponse(
        user_id=user.id, username=user.username, display_name=user.full_name, role=user.role,
        permissions=list(authz.permission_codes),
        issued_at=datetime.fromtimestamp(issued_at, timezone.utc).isoformat(),
        expires_at=datetime.fromtimestamp(expires_at, timezone.utc).isoformat(),
        access_token=access_token, jti=str(claims.get("jti") or ""),
        parent_jti=payload.get("parent_jti"),
    )

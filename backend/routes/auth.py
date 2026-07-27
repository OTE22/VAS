"""
Authentication Routes
====================
Login, logout, and token management.
"""

import os
import sys
import time
import logging
from datetime import timedelta
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, status, Request, Response
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials
from sqlalchemy.ext.asyncio import AsyncSession
from pydantic import BaseModel, Field, field_validator

# Add parent directory to path
parent_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if parent_dir not in sys.path:
    sys.path.insert(0, parent_dir)

from db_connection import get_db
from backend.auth.auth_service import AuthService, get_current_user, security
from backend.auth import auth_security
from backend.auth.password import hash_password
from db_models import User

logger = logging.getLogger(__name__)

router = APIRouter()


class LoginRequest(BaseModel):
    """Credential submission.

    Length limits protect the password-verification service from oversized
    payloads. The backend is authoritative — the frontend mirrors these.
    """
    username: str = Field(..., min_length=1, max_length=auth_security.MAX_USERNAME_LENGTH)
    password: str = Field(..., min_length=1, max_length=auth_security.MAX_PASSWORD_LENGTH)

    @field_validator("username")
    @classmethod
    def normalize_username(cls, v: str) -> str:
        # Trim surrounding whitespace only — internal characters are preserved.
        # The password is NEVER modified.
        return (v or "").strip()


class LoginResponse(BaseModel):
    """Login result.

    `access_token` is omitted entirely for browser (cookie) clients — the
    credential lives only in the HttpOnly cookie. Programmatic clients that
    do not send `X-Requested-With: XMLHttpRequest` still receive a bearer
    token so existing scripts and integrations keep working.
    """
    success: bool = True
    user: dict
    redirect_url: str
    access_token: Optional[str] = None
    token_type: Optional[str] = None


class UserInfo(BaseModel):
    id: int
    username: str
    email: str
    full_name: str | None
    role: str
    can_use_chatbot: bool
    is_active: bool
    # Same capability vocabulary and version as /api/auth/me/privileges, so the
    # two endpoints can never disagree about the same user at the same moment.
    # Defaulted because UserInfo is also nested inside UserPrivileges, where the
    # authoritative copy is the top-level one.
    permissions: list[str] = []
    permissions_version: int = 1


class NavbarLink(BaseModel):
    """Navbar link information"""
    page: str  # data-page attribute
    href: str  # URL
    label: str  # Display text
    icon: str  # FontAwesome icon class
    visible: bool  # Whether this link should be visible
    title: Optional[str] = None  # Optional tooltip/title
    parent_page: Optional[str] = None  # If set, this link belongs to a dropdown (e.g., "system", "management")


class UserPrivileges(BaseModel):
    """Comprehensive user privileges and access information.

    STABLE CONTRACT — frontend consumers read the TOP-LEVEL fields:
        user_id, username, role, privileges
    The nested `user` object is kept for backward compatibility; new code
    must use `privileges.role`, never `privileges.user.role`.
    Role is UI-shaping only — every backend route enforces authorization
    independently.
    """
    user_id: int
    username: str
    role: str
    privileges: list[str]
    user: UserInfo
    pipelines: list[str]
    can_access_unknown_faces: bool
    can_manage_identities: bool
    privileges_summary: str
    accessible_pipelines_count: int
    navbar_links: list[NavbarLink]  # Backend-determined navbar links to display
    # Stable capability codes (e.g. "chatbot.use", "admin.users.manage") resolved
    # by backend.auth.capabilities — the same resolver every server-side gate
    # uses, so the client cannot disagree with the enforcement about what this
    # user may do. `privileges` above remains human-readable prose.
    permissions: list[str] = []
    # Increments on every authorization change. A client holding a different
    # value knows its view is stale without having to diff the whole payload.
    permissions_version: int = 1


def _client_ip(request: Request) -> str:
    """Best-effort client IP: nginx sets X-Real-IP; fall back to the socket peer."""
    return request.headers.get("x-real-ip") or (request.client.host if request.client else "unknown")


def _is_browser_client(request: Request) -> bool:
    """Browser (cookie) clients announce themselves with X-Requested-With.

    They receive NO token in the response body — the credential exists only
    in the HttpOnly cookie. Programmatic clients (curl, scripts, server-side
    integrations) still get a bearer token so existing automation keeps
    working; a token in their hands is not reachable by page JavaScript.
    """
    return request.headers.get("x-requested-with", "").lower() == "xmlhttprequest"


def _auth_json_error(status_code: int, code: str, message: str,
                     reference_id: str = None, retryable: bool = True,
                     headers: dict = None, **extra) -> JSONResponse:
    """Structured auth error with no-store headers."""
    body = auth_security.auth_error(code, message, reference_id, retryable, **extra)
    resp = JSONResponse(status_code=status_code, content=body, headers=headers or {})
    resp.headers["Cache-Control"] = "no-store"
    resp.headers["Pragma"] = "no-cache"
    return resp


@router.post("/api/auth/login", response_model=LoginResponse)
async def login(credentials: LoginRequest, request: Request, response: Response, db: AsyncSession = Depends(get_db)):
    """Authenticate and establish a session.

    Flow: origin check -> rate-limit check -> credential verification (with
    timing equalization) -> fresh token (new jti = session rotation) ->
    HttpOnly cookie -> minimal safe response.

    Never logs credentials, tokens, cookies or user objects; usernames and
    IPs are pseudonymized in the audit trail.
    """
    ip = auth_security.client_ip(request)
    request_id = getattr(request.state, "request_id", "-")
    start_time = time.time()
    username = credentials.username  # already trimmed by the validator

    auth_security.record_metric("attempt", result="received")

    # --- 1. Login CSRF: reject cross-site credential submissions ---
    origin_ok, origin_failure = auth_security.validate_login_origin(request)
    if not origin_ok:
        reference_id = auth_security.new_reference_id()
        auth_security.audit("login", result="rejected", request_id=request_id,
                            failure_code="CSRF_FAILED", username=username, ip=ip,
                            reference_id=reference_id, detail=origin_failure,
                            duration_ms=int((time.time() - start_time) * 1000))
        auth_security.record_metric("failure", failure_code="CSRF_FAILED")
        return _auth_json_error(403, "CSRF_FAILED",
                                "This sign-in request was blocked for security reasons.",
                                reference_id, retryable=False)

    # --- 2. Brute-force / credential-stuffing protection ---
    decision = await auth_security.check_rate_limits(username, ip)
    if not decision.allowed:
        reference_id = auth_security.new_reference_id()
        auth_security.audit("login", result="rate_limited", request_id=request_id,
                            failure_code="RATE_LIMITED", username=username, ip=ip,
                            reference_id=reference_id, scope=decision.scope,
                            duration_ms=int((time.time() - start_time) * 1000))
        auth_security.record_metric("rate_limited", scope=decision.scope)
        auth_security.record_metric("failure", failure_code="RATE_LIMITED")
        return _auth_json_error(
            429, "RATE_LIMITED",
            "Too many sign-in attempts. Please wait and try again.",
            reference_id, retryable=True,
            headers={"Retry-After": str(max(1, decision.retry_after_seconds))},
            retry_after_seconds=max(1, decision.retry_after_seconds))

    try:
        user, failure_reason = await AuthService.authenticate_user_detailed(
            username, credentials.password, db)

        if not user:
            # Timing equalization, applied ONLY to paths that skipped bcrypt
            # (unknown user / disabled account). Running it after a real
            # verification would double the wrong-password latency and become
            # a timing oracle in the opposite direction.
            if failure_reason in ("unknown_user", "account_disabled"):
                auth_security.dummy_password_verify(credentials.password)
            await auth_security.record_failure(username, ip)
            reference_id = auth_security.new_reference_id()
            auth_security.audit("login", result="failure", request_id=request_id,
                                failure_code="INVALID_CREDENTIALS", username=username, ip=ip,
                                reference_id=reference_id,
                                duration_ms=int((time.time() - start_time) * 1000))
            auth_security.record_metric("failure", failure_code="INVALID_CREDENTIALS")
            auth_security.record_metric("duration", seconds=time.time() - start_time)
            # Identical message and shape for "no such user" and "wrong password"
            return _auth_json_error(401, "INVALID_CREDENTIALS",
                                    auth_security.GENERIC_CREDENTIALS_MESSAGE,
                                    reference_id, retryable=True,
                                    headers={"WWW-Authenticate": "Bearer"})

        # --- 3. Session rotation: a brand-new token id per login ---
        try:
            access_token = AuthService.create_access_token(
                data={"sub": str(user.id), "username": user.username, "role": user.role}
            )
        except Exception as e:
            reference_id = auth_security.new_reference_id()
            logger.error("[AUTH] Session creation failed reference_id=%s error=%s",
                         reference_id, type(e).__name__, exc_info=True)
            auth_security.audit("login", result="error", request_id=request_id,
                                failure_code="SESSION_CREATION_FAILED", user_id=user.id,
                                ip=ip, reference_id=reference_id)
            auth_security.record_metric("session_failure")
            return _auth_json_error(500, "SESSION_CREATION_FAILED",
                                    "Could not establish a session. Please try again.",
                                    reference_id, retryable=True)

        # --- 4. Secure HttpOnly cookie (the only place the credential lives) ---
        cookie = auth_security.cookie_settings()
        response.set_cookie(value=access_token, **cookie)

        # Successful login releases the throttle for this account/source, so an
        # attacker cannot lock the real user out by burning their counter.
        await auth_security.clear_failures(username, ip)

        redirect_url = auth_security.redirect_for_role(user.role)
        browser_client = _is_browser_client(request)

        login_response = LoginResponse(
            success=True,
            user={
                "id": user.id,
                "username": user.username,
                "role": user.role,
            },
            redirect_url=redirect_url,
            # Browser clients get NO token in the body — cookie only.
            access_token=None if browser_client else access_token,
            token_type=None if browser_client else "bearer",
        )

        auth_security.audit("login", result="success", request_id=request_id,
                            user_id=user.id, ip=ip, username=username,
                            client="browser" if browser_client else "api",
                            duration_ms=int((time.time() - start_time) * 1000))
        auth_security.record_metric("attempt", result="success")
        auth_security.record_metric("duration", seconds=time.time() - start_time)

        response.headers["Cache-Control"] = "no-store"
        response.headers["Pragma"] = "no-cache"
        return login_response

    except HTTPException:
        raise
    except Exception as e:
        reference_id = auth_security.new_reference_id()
        logger.error("[AUTH] Login internal error reference_id=%s request_id=%s error=%s",
                     reference_id, request_id, type(e).__name__, exc_info=True)
        auth_security.audit("login", result="error", request_id=request_id,
                            failure_code="AUTH_SERVICE_UNAVAILABLE", ip=ip,
                            reference_id=reference_id)
        auth_security.record_metric("failure", failure_code="AUTH_SERVICE_UNAVAILABLE")
        return _auth_json_error(500, "AUTH_SERVICE_UNAVAILABLE",
                                "Sign-in is temporarily unavailable. Please try again.",
                                reference_id, retryable=True)


@router.get("/api/auth/me", response_model=UserInfo)
async def get_current_user_info(response: Response, current_user: User = Depends(get_current_user)):
    """Get current user information (never cached)."""
    from backend.auth.capabilities import resolve_effective_authorization

    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    authz = resolve_effective_authorization(current_user)
    return UserInfo(
        id=current_user.id,
        username=current_user.username,
        email=current_user.email,
        full_name=current_user.full_name,
        role=current_user.role,
        can_use_chatbot=current_user.can_use_chatbot,
        is_active=current_user.is_active,
        permissions=authz.permission_codes,
        permissions_version=authz.permissions_version,
    )


@router.get("/api/auth/me/privileges", response_model=UserPrivileges)
async def get_user_privileges(
    response: Response,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """
    Get comprehensive user privileges and access information.
    
    This endpoint provides all information needed by the frontend to determine
    what features the user can access, including pipeline access and identity management permissions.
    
    **Response includes:**
    - User information (id, username, email, role, etc.)
    - List of accessible pipeline IDs
    - Boolean flags for access permissions:
      - `can_access_unknown_faces`: Whether user can view/manage unknown identities
      - `can_manage_identities`: Whether user can promote/merge identities
    - Human-readable privileges summary
    - Count of accessible pipelines
    
    **Access Control:**
    - Admins: Full access to all pipelines and all identities
    - Regular users: Access only to assigned pipelines and their associated identities
    
    **Use Cases:**
    - Frontend navbar customization (show/hide links)
    - Page access control
    - UI privilege display
    - Feature gating
    """
    try:
        from backend.auth.auth_service import AuthService
        from backend.auth.capabilities import resolve_effective_authorization
        from sqlalchemy import select
        from db_models import Pipeline

        # One resolver, shared with every server-side gate, so the capability
        # list the client receives cannot drift from what is actually enforced.
        effective_authz = resolve_effective_authorization(current_user)

        logger.debug(f"[PRIVILEGES] Getting privileges for user: {current_user.username} (role: {current_user.role})")
        
        # Get user pipelines
        if current_user.role == "admin":
            # Admin has access to all pipelines
            logger.debug("[PRIVILEGES] User is admin, fetching all pipelines")
            result = await db.execute(select(Pipeline.pipeline_id))
            pipelines = [row[0] for row in result.all()]
            can_access_unknown_faces = True
            can_manage_identities = True
            privileges_summary = "Full access to all pipelines and identities"
            logger.info(f"[PRIVILEGES] Admin user has access to {len(pipelines)} pipelines")
        else:
            # Regular users get their assigned pipelines
            logger.debug(f"[PRIVILEGES] User is regular user, fetching assigned pipelines for user_id: {current_user.id}")
            pipelines = await AuthService.get_user_pipelines(current_user.id, db)
            can_access_unknown_faces = len(pipelines) > 0
            can_manage_identities = len(pipelines) > 0
            
            logger.debug(f"[PRIVILEGES] User has {len(pipelines)} assigned pipelines")
            
            if len(pipelines) > 0:
                pipeline_list = pipelines[:3]
                if len(pipelines) > 3:
                    privileges_summary = f"Access to pipelines: {', '.join(pipeline_list)} +{len(pipelines) - 3} more. You can view, promote, and merge identities from these pipelines."
                else:
                    privileges_summary = f"Access to pipelines: {', '.join(pipelines)}. You can view, promote, and merge identities from these pipelines."
                logger.info(f"[PRIVILEGES] Regular user has access to {len(pipelines)} pipelines: {', '.join(pipelines[:5])}")
            else:
                privileges_summary = "No pipeline access. Contact your administrator."
                logger.warning(f"[PRIVILEGES] Regular user has no pipeline access")
        
        # BACKEND DETERMINES NAVBAR LINKS (Single Source of Truth)
        navbar_links = []
        
        if current_user.role == "admin":
            # Admin: Show all links (order matches admin-navbar.html)
            navbar_links = [
                NavbarLink(page="home", href="/home", label="HOME", icon="fas fa-home", visible=True),
                NavbarLink(page="dashboard", href="/dashboard", label="DASHBOARD", icon="fas fa-tachometer-alt", visible=True),
                # Management Dropdown items
                NavbarLink(page="add-person", href="/dashboard", label="ADD PERSON", icon="fas fa-user-plus", visible=True, title="Add a person to track", parent_page="management"),
                NavbarLink(page="users", href="/admin/users", label="USERS", icon="fas fa-users", visible=True, parent_page="management"),
                NavbarLink(page="pipelines", href="/admin/pipelines", label="PIPELINES", icon="fas fa-video", visible=True, parent_page="management"),
                NavbarLink(page="audit", href="/admin/audit", label="AUDIT LOG", icon="fas fa-clipboard-list", visible=True, parent_page="management"),
                NavbarLink(page="tracking", href="/tracking-people", label="TRACKING", icon="fas fa-user-friends", visible=True, parent_page="management"),
                NavbarLink(page="unknown", href="/admin/unknown", label="UNKNOWN FACES", icon="fas fa-user-secret", visible=True),
                # Search & Intelligence Dropdown items
                NavbarLink(page="search", href="/admin/search", label="ADVANCED SEARCH", icon="fas fa-search-plus", visible=True, title="Advanced Face Search with Multi-face Detection", parent_page="search"),
                NavbarLink(page="search-history", href="/admin/search-history", label="SEARCH HISTORY", icon="fas fa-history", visible=True, title="View and manage search history", parent_page="search"),
                NavbarLink(page="intelligence", href="/admin/intelligence", label="INTELLIGENCE ANALYSIS", icon="fas fa-brain", visible=True, title="Intelligence Analysis - Related Identities, Temporal Patterns, Cross-Camera Tracking", parent_page="search"),
                NavbarLink(page="watchlists", href="/admin/watchlists", label="WATCHLISTS", icon="fas fa-list-alt", visible=True, title="Manage Watchlists (VIP, Threat, POI)", parent_page="search"),
                NavbarLink(page="security-intelligence", href="/admin/security-intelligence", label="SECURITY INTELLIGENCE", icon="fas fa-shield-alt", visible=True, title="Security Intelligence - Network Analysis, Pattern Detection, Threat Assessment", parent_page="search"),
                # System Dropdown items
                NavbarLink(page="ml-model", href="/admin/ml-model", label="ML MODEL", icon="fas fa-brain", visible=True, title="ML Similarity Model - Train and manage merge suggestion model", parent_page="system"),
                NavbarLink(page="background-tasks", href="/admin/background-tasks", label="BACKGROUND TASKS", icon="fas fa-tasks", visible=True, title="Monitor background task executions", parent_page="system"),
                NavbarLink(page="live-alerts", href="/admin/live-alerts", label="LIVE ALERTS", icon="fas fa-bell", visible=True, title="Live Search Alerts - Get notified when tracked faces appear", parent_page="system"),
                NavbarLink(page="tutorial", href="/admin/tutorial", label="TUTORIAL", icon="fas fa-graduation-cap", visible=True, title="Admin Tutorial & Learning Guide", parent_page="system"),
                NavbarLink(page="settings", href="/admin/settings", label="SETTINGS", icon="fas fa-cog", visible=True, title="System Settings Management", parent_page="system"),
                NavbarLink(page="logs", href="/admin/logs", label="ERROR LOGS", icon="fas fa-exclamation-triangle", visible=True, title="System Error Logs Viewer", parent_page="system"),
                NavbarLink(page="docs", href="/docs", label="API DOCS", icon="fas fa-book", visible=True, title="Open API Documentation in new tab", parent_page="system"),
            ]
            logger.debug(f"[PRIVILEGES] Admin user - showing all {len(navbar_links)} navbar links")
        else:
            # Regular users: Only show DASHBOARD, UNKNOWN FACES (if they have access)
            # Note: Merge suggestions are accessed via the UNKNOWN FACES page, not a separate navbar link
            navbar_links = [
                NavbarLink(page="dashboard", href="/dashboard", label="DASHBOARD", icon="fas fa-tachometer-alt", visible=True),
            ]
            
            # Add UNKNOWN FACES and LIVE ALERTS links only if user has pipeline access
            # This page includes access to merge suggestions and live alerts for users with pipeline access
            if can_access_unknown_faces:
                navbar_links.append(
                    NavbarLink(page="unknown", href="/admin/unknown", label="UNKNOWN FACES", icon="fas fa-user-secret", visible=True)
                )
                navbar_links.append(
                    NavbarLink(page="live-alerts", href="/admin/live-alerts", label="LIVE ALERTS", icon="fas fa-bell", visible=True, title="Live Search Alerts - Get notified when tracked faces appear")
                )
                logger.debug(f"[PRIVILEGES] Regular user with pipeline access - showing UNKNOWN FACES and LIVE ALERTS links")
            else:
                logger.debug(f"[PRIVILEGES] Regular user without pipeline access - hiding UNKNOWN FACES and LIVE ALERTS links")
            
            # Hide all admin-only links (backend decision)
            # These are not added to navbar_links, so frontend won't show them
            logger.debug(f"[PRIVILEGES] Regular user - showing only {len(navbar_links)} navbar links (DASHBOARD + UNKNOWN FACES if applicable)")
        
        privilege_flags = []
        if can_access_unknown_faces:
            privilege_flags.append("unknown_faces")
        if can_manage_identities:
            privilege_flags.append("manage_identities")
        if current_user.can_use_chatbot:
            privilege_flags.append("chatbot")

        privileges = UserPrivileges(
            user_id=current_user.id,
            username=current_user.username,
            role=current_user.role,
            privileges=privilege_flags,
            user=UserInfo(
                id=current_user.id,
                username=current_user.username,
                email=current_user.email,
                full_name=current_user.full_name,
                role=current_user.role,
                can_use_chatbot=current_user.can_use_chatbot,
                is_active=current_user.is_active,
            ),
            pipelines=pipelines,
            can_access_unknown_faces=can_access_unknown_faces,
            can_manage_identities=can_manage_identities,
            privileges_summary=privileges_summary,
            accessible_pipelines_count=len(pipelines),
            navbar_links=navbar_links,
            permissions=effective_authz.permission_codes,
            permissions_version=effective_authz.permissions_version,
        )

        # no-store, matching /api/auth/me. Without it a cached privileges
        # response can outlive a revocation and keep rendering navigation the
        # backend will refuse — the client-side half of the same staleness bug.
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
        response.headers["Pragma"] = "no-cache"

        logger.debug(f"[PRIVILEGES] Successfully returned privileges for user: {current_user.username}")
        return privileges
        
    except Exception as e:
        logger.error(f"[PRIVILEGES] Error getting privileges for user {current_user.username}: {str(e)}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to retrieve user privileges. Please try again later."
        )


@router.post("/api/auth/logout")
async def logout(request: Request, response: Response, current_user: User = Depends(get_current_user)):
    """End the session for real.

    The presented token's `jti` is added to the revocation denylist until its
    natural expiry, so the old credential cannot be replayed even if it was
    captured. The cookie is cleared with **matching attributes** (a mismatched
    Path/Secure/SameSite leaves the cookie in the browser).
    """
    request_id = getattr(request.state, "request_id", "-")
    ip = auth_security.client_ip(request)

    # Revoke the presented token (both transports)
    token = auth_security.read_auth_cookie(request)
    if not token:
        header = request.headers.get("authorization", "")
        if header.lower().startswith("bearer "):
            token = header[7:].strip()

    revoked = False
    if token:
        payload = AuthService.decode_token(token, verify_revocation=False) or {}
        jti = payload.get("jti")
        expires_at = payload.get("exp")
        if jti:
            ttl = max(1, int(expires_at - time.time())) if expires_at else 3600
            revoked = await auth_security.revoke_token(jti, ttl)

    # Clear the cookie with the same attributes it was set with
    cookie = auth_security.cookie_settings()
    response.delete_cookie(
        key=cookie["key"], path=cookie["path"], domain=cookie["domain"],
        httponly=True, samesite=cookie["samesite"], secure=cookie["secure"],
    )
    # Also clear the alternate name so a rollout in either direction is clean
    for legacy in ("access_token", "__Host-access_token"):
        if legacy != cookie["key"]:
            response.delete_cookie(key=legacy, path="/", httponly=True,
                                   samesite=cookie["samesite"], secure=cookie["secure"])

    auth_security.audit("logout", result="success", request_id=request_id,
                        user_id=current_user.id, ip=ip, token_revoked=revoked)

    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    return {"success": True, "message": "Logged out successfully"}


"""
Dashboard Routes
================
Routes for dashboard and root endpoints.
"""

import os
import sys
import logging
from fastapi import APIRouter, Depends, Header, Request, Query
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, HTMLResponse
from typing import Optional

# Add parent directory to path
parent_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if parent_dir not in sys.path:
    sys.path.insert(0, parent_dir)

from config import settings
from backend.config import FACE_TRACKING_ENABLED
from backend.auth.auth_service import (AuthService,
                                       get_current_user_allow_pending_rotation,
                                       require_chatbot_access,
                                       require_strict_access)
from db_models import User
from db_connection import get_db
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Admin Pages"])


async def read_file_off_loop(path: str, encoding: str = 'utf-8') -> str:
    """Read a text file in the default executor so route handlers never block the loop."""
    import asyncio

    def _read():
        with open(path, 'r', encoding=encoding) as f:
            return f.read()

    return await asyncio.get_running_loop().run_in_executor(None, _read)


@router.get("/dashboard")
async def dashboard(
    current_user: User = Depends(require_strict_access(allowed_roles=None, require_pipeline_access=False))
):
    """
    Production Dashboard HTML page - STRICT AUTHENTICATION REQUIRED
    
    **STRICT ACCESS CONTROL:**
    - User MUST be authenticated and active
    - Access is granted based on admin-defined permissions
    - Backend validates ALL access - frontend cannot bypass
    """
    try:
        # STRICT CHECK: User must be authenticated and active (handled by require_strict_access)
        logger.info(f"[DASHBOARD] ✅ STRICT AUTH: User {current_user.username} (ID: {current_user.id}, Role: {current_user.role}) accessing dashboard")
        return FileResponse("frontend/dashboard.html")
    except FileNotFoundError:
        return JSONResponse(
            content={
                "message": "Dashboard HTML file not found. Running in API-only mode.",
                "api_endpoints": {
                    "webhook": "/webhook/{pipeline_id}",
                    "detections": "/api/detections",
                    "stats": "/api/stats",
                    "health": "/health",
                    "metrics": "/metrics",
                    "upload_person": "/api/upload-person"
                }
            }
        )


@router.get("/home")
async def home(
    request: Request,
    authorization: Optional[str] = Header(None, alias="Authorization"),
    db: AsyncSession = Depends(get_db)
):
    """Home page - Admin only"""
    # Try to get token from Authorization header first (for API/fetch requests)
    token = None
    if authorization and authorization.startswith("Bearer "):
        token = authorization.replace("Bearer ", "").strip()
    
    # If no token in header, check cookies (for browser navigation)
    if not token:
        token = request.cookies.get("__Host-access_token") or request.cookies.get("access_token")
    
    # If still no token, redirect to signin
    if not token:
        logger.info("[HOME] No token found, redirecting to signin")
        return RedirectResponse(url="/signin", status_code=302)
    
    # Validate token and check if user is admin
    try:
        payload = AuthService.decode_token(token)
        if not payload:
            logger.warning("[HOME] Invalid token payload, redirecting to signin")
            return RedirectResponse(url="/signin", status_code=302)
        
        # JWT 'sub' claim is a string, convert to int for user lookup
        user_id_str = payload.get("sub")
        if not user_id_str:
            logger.warning("[HOME] No user ID in token, redirecting to signin")
            return RedirectResponse(url="/signin", status_code=302)
        
        try:
            user_id = int(user_id_str)
            result = await db.execute(
                select(User).where(User.id == user_id)
            )
            current_user = result.scalar_one_or_none()
            
            if not current_user:
                logger.warning(f"[HOME] User {user_id} not found, redirecting to signin")
                return RedirectResponse(url="/signin", status_code=302)
            
            if current_user.role == "admin":
                # Admin user - allow access
                logger.info(f"[HOME] Admin user {current_user.username} accessing home page")
                try:
                    return FileResponse("frontend/home.html")
                except:
                    return JSONResponse(content={"message": "Home page not found"})
            else:
                # Non-admin user - STRICTLY FORBIDDEN - redirect to dashboard
                logger.warning(f"[HOME] ⚠️ SECURITY: Non-admin user {current_user.username} (ID: {current_user.id}) attempted to access admin-only home page. Redirecting to dashboard.")
                # Return 403 Forbidden for API requests, redirect for browser requests
                accept_header = request.headers.get("Accept", "")
                if "application/json" in accept_header or "text/html" not in accept_header:
                    return JSONResponse(
                        status_code=403,
                        content={"error": "Forbidden", "message": "Home page is only accessible to administrators"}
                    )
                return RedirectResponse(url="/dashboard", status_code=302)
                
        except (ValueError, TypeError) as e:
            logger.warning(f"[HOME] Invalid user ID in token: {user_id_str}, error: {e}")
            return RedirectResponse(url="/signin", status_code=302)
            
    except Exception as e:
        logger.warning(f"[HOME] Error checking authentication: {e}")
        return RedirectResponse(url="/signin", status_code=302)


@router.get("/admin/unknown")
async def unknown_faces(
    request: Request,
    authorization: Optional[str] = Header(None, alias="Authorization"),
    db: AsyncSession = Depends(get_db)
):
    """
    Unknown faces management page.
    
    **ACCESS CONTROL (Backend-Enforced):**
    ======================================
    ✅ ADMIN USERS: Always allowed (full access)
    ✅ REGULAR USERS WITH PIPELINE ACCESS: Allowed (can see/manage identities from their assigned pipelines)
    ❌ REGULAR USERS WITHOUT PIPELINE ACCESS: Redirected to /dashboard
    
    **How to Grant Access:**
    - Admin must go to: Admin → Users → Edit User
    - Check at least ONE pipeline in "VIDEO PIPELINE ACCESS" section
    - Click "Save"
    - User will immediately have access to Unknown Faces page
    
    **Security:**
    - All access decisions are made by the backend (no frontend logic)
    - Token validation and user verification performed server-side
    - Pipeline access checked via user_pipeline_access table
    """
    # LOG IMMEDIATELY TO VERIFY ROUTE IS BEING HIT
    # Use print() as well to ensure we see it even if logging is misconfigured
    logger.info(f"[UNKNOWN] ========== ROUTE HIT ========== GET /admin/unknown")
    logger.info(f"[UNKNOWN] ========== ROUTE HIT ========== GET /admin/unknown from {request.client.host if request.client else 'unknown'}")
    logger.info(f"[UNKNOWN] Request headers: Authorization={'present' if authorization else 'missing'}, Cookies: {list(request.cookies.keys())}")
    
    from backend.auth.auth_service import AuthService
    from fastapi.responses import FileResponse, RedirectResponse, JSONResponse
    
    # Try to get token from Authorization header first
    token = None
    if authorization and authorization.startswith("Bearer "):
        token = authorization.replace("Bearer ", "").strip()
        logger.debug(f"[UNKNOWN] Token found in Authorization header (length: {len(token)})")
    
    # If no token in header, check cookies
    if not token:
        token = request.cookies.get("__Host-access_token") or request.cookies.get("access_token")
        if token:
            logger.debug(f"[UNKNOWN] Token found in cookies (length: {len(token)})")
    
    # If still no token, redirect to signin
    if not token:
        logger.warning("[UNKNOWN] ❌ No token provided in header or cookies, redirecting to signin")
        return RedirectResponse(url="/signin", status_code=302)
    
    # Validate token and check user access (BACKEND HANDLES ALL LOGIC)
    try:
        payload = AuthService.decode_token(token)
        if not payload:
            logger.warning("[UNKNOWN] Invalid token, redirecting to signin")
            return RedirectResponse(url="/signin", status_code=302)
        
        user_id_str = payload.get("sub")
        if not user_id_str:
            logger.warning("[UNKNOWN] No user ID in token, redirecting to signin")
            return RedirectResponse(url="/signin", status_code=302)
        
        user_id = int(user_id_str)
        result = await db.execute(select(User).where(User.id == user_id))
        current_user = result.scalar_one_or_none()
        
        if not current_user:
            logger.warning(f"[UNKNOWN] User {user_id} not found, redirecting to signin")
            return RedirectResponse(url="/signin", status_code=302)
        
        if not current_user.is_active:
            logger.warning(f"[UNKNOWN] User {current_user.username} is inactive, redirecting to signin")
            return RedirectResponse(url="/signin", status_code=302)
        
        # BACKEND ACCESS CONTROL LOGIC
        # ============================================
        # ACCESS RULES:
        # 1. Admin users: ALWAYS ALLOWED
        # 2. Regular users: ALLOWED if they have at least ONE pipeline assigned
        # 3. Regular users without pipelines: REDIRECTED to /dashboard
        # ============================================
        
        logger.info(f"[UNKNOWN] Access check for user: {current_user.username} (ID: {current_user.id}, Role: {current_user.role})")
        logger.info(f"[UNKNOWN] Access check for user: {current_user.username} (ID: {current_user.id}, Role: {current_user.role})")
        
        # ?view= / &from= are handled CLIENT-side now (admin-unknown.js
        # openDeepLinkedIdentity). The server used to fetch the identity here,
        # serialize it, and string-inject an inline <script> that called
        # viewIdentityDetails() — which (a) violated the script-src 'self'
        # CSP, (b) duplicated the /api/admin/identity/{id} query only for its
        # payload to be discarded (the modal re-fetches), and (c) failed
        # SILENTLY for a missing or not-permitted identity. The API endpoint
        # enforces the same per-identity access check, so removing the
        # server-side path loses no protection — it gains an error message.
        
        # Rule 1: Admin always has access
        if current_user.role == "admin":
            logger.info(f"[UNKNOWN] ✅ ADMIN ACCESS GRANTED: Admin user {current_user.username} accessing unknown faces page")
            try:
                # Read HTML file
                html_path = "frontend/admin/unknown.html"
                html_content = await read_file_off_loop(html_path)

                
                return HTMLResponse(content=html_content)
            except Exception as e:
                logger.error(f"[UNKNOWN] Error serving HTML file: {e}", exc_info=True)
                return JSONResponse(
                    status_code=500,
                    content={"message": "Unknown faces page not found", "error": str(e)}
                )
        
        # Rule 2 & 3: Regular users - check pipeline access
        logger.info(f"[UNKNOWN] Regular user detected. Checking pipeline access for: {current_user.username} (ID: {current_user.id})")
        logger.info(f"[UNKNOWN] Regular user detected. Checking pipeline access for: {current_user.username} (ID: {current_user.id})")
        
        try:
            user_pipelines = await AuthService.get_user_pipelines(current_user.id, db)
            logger.info(f"[UNKNOWN] Pipeline query result: {user_pipelines} (type: {type(user_pipelines)}, length: {len(user_pipelines) if user_pipelines else 0})")
            logger.info(f"[UNKNOWN] Pipeline query result: {user_pipelines} (type: {type(user_pipelines)}, length: {len(user_pipelines) if user_pipelines else 0})")
            
            # Debug: Check database directly to verify
            from db_models import UserPipelineAccess
            debug_result = await db.execute(
                select(UserPipelineAccess.pipeline_id)
                .where(UserPipelineAccess.user_id == current_user.id)
            )
            debug_pipelines = [row[0] for row in debug_result.all()]
            logger.info(f"[UNKNOWN] Direct database query for user {current_user.id}: {debug_pipelines} (count: {len(debug_pipelines)})")
            
            # Check if user has any pipeline access
            has_pipeline_access = user_pipelines is not None and len(user_pipelines) > 0
            
            if has_pipeline_access:
                # Rule 2: User has pipeline access - ALLOW ACCESS
                logger.info(f"[UNKNOWN] ✅ USER WITH PIPELINE ACCESS GRANTED: Regular user {current_user.username} has access to {len(user_pipelines)} pipeline(s): {user_pipelines}")
                logger.info(f"[UNKNOWN] ✅ USER WITH PIPELINE ACCESS GRANTED: Regular user {current_user.username} has access to {len(user_pipelines)} pipeline(s): {user_pipelines}")
                try:
                    # Read HTML file
                    html_path = "frontend/admin/unknown.html"
                    html_content = await read_file_off_loop(html_path)
                    
                    
                    return HTMLResponse(content=html_content)
                except Exception as e:
                    logger.error(f"[UNKNOWN] Error serving HTML file: {e}", exc_info=True)
                    return JSONResponse(
                        status_code=500,
                        content={"message": "Unknown faces page not found", "error": str(e)}
                    )
            else:
                # Rule 3: User has NO pipeline access - REDIRECT
                logger.warning(f"[UNKNOWN] ❌ ACCESS DENIED: Regular user {current_user.username} (ID: {current_user.id}) has NO pipeline access")
                logger.warning(f"[UNKNOWN] Pipeline list: {user_pipelines} (empty or None)")
                logger.warning(f"[UNKNOWN] Direct DB query result: {debug_pipelines}")
                logger.warning(f"[UNKNOWN] SOLUTION: Admin must assign at least one pipeline to this user via Admin → Users → Edit User → Video Pipeline Access")
                return RedirectResponse(url="/dashboard", status_code=302)
                
        except Exception as pipeline_check_error:
            logger.error(f"[UNKNOWN] ❌ ERROR checking pipeline access for user {current_user.id}: {type(pipeline_check_error).__name__}: {str(pipeline_check_error)}", exc_info=True)
            # On error, deny access for security (fail closed)
            logger.warning(f"[UNKNOWN] Access denied due to pipeline check error. User: {current_user.username} (ID: {current_user.id})")
            return RedirectResponse(url="/dashboard", status_code=302)
            
    except ValueError as e:
        logger.warning(f"[UNKNOWN] Invalid user ID format: {e}")
        return RedirectResponse(url="/signin", status_code=302)
    except Exception as e:
        logger.error(f"[UNKNOWN] Error checking authentication: {e}", exc_info=True)
        return RedirectResponse(url="/signin", status_code=302)


@router.get("/admin/identity/{identity_id}")
async def identity_profile(
    identity_id: str,
    request: Request,
    authorization: Optional[str] = Header(None, alias="Authorization"),
    db: AsyncSession = Depends(get_db)
):
    """Identity profile page — the app's first path-parameter HTML route.

    Exists because "Open full profile" had nowhere honest to land: identity
    detail lived only in a modal on the Unknown Faces Center, so a KNOWN
    person's profile carried an "unknown" URL and framing.

    Access mirrors /admin/unknown (same audience as identity detail today):
    admin always; regular user with >=1 pipeline; otherwise /dashboard;
    unauthenticated /signin. Per-identity authorization is enforced by
    GET /api/admin/identity/{id} (403), which the page surfaces.

    The server deliberately does NOT fetch or inject the identity —
    frontend/admin/identity.html is served as-is and the page JS reads the id
    from location.pathname. Injecting data here is what the old ?view= flow
    did, and it was both a CSP violation and duplicated work
    (test_ml_model_system.test_page_injection_removed pins the prohibition).
    `identity_id` is therefore intentionally unused; malformed values render
    the page's own error state.
    """
    token = None
    if authorization and authorization.startswith("Bearer "):
        token = authorization.replace("Bearer ", "").strip()
    if not token:
        token = request.cookies.get("__Host-access_token") or request.cookies.get("access_token")
    if not token:
        return RedirectResponse(url="/signin", status_code=302)

    try:
        payload = AuthService.decode_token(token)
        if not payload or not payload.get("sub"):
            return RedirectResponse(url="/signin", status_code=302)

        result = await db.execute(select(User).where(User.id == int(payload["sub"])))
        current_user = result.scalar_one_or_none()
        if not current_user or not current_user.is_active:
            return RedirectResponse(url="/signin", status_code=302)

        if current_user.role != "admin":
            try:
                user_pipelines = await AuthService.get_user_pipelines(current_user.id, db)
            except Exception as pipeline_error:
                # Fail closed, same as /admin/unknown.
                logger.error(f"[IDENTITY_PAGE] Pipeline check failed: {pipeline_error}", exc_info=True)
                return RedirectResponse(url="/dashboard", status_code=302)
            if not user_pipelines:
                return RedirectResponse(url="/dashboard", status_code=302)

        logger.info(
            f"[IDENTITY_PAGE] user={current_user.username} role={current_user.role} "
            f"identity={identity_id}")
        return FileResponse("frontend/admin/identity.html")

    except ValueError:
        return RedirectResponse(url="/signin", status_code=302)
    except Exception as e:
        logger.error(f"[IDENTITY_PAGE] Auth error: {e}", exc_info=True)
        return RedirectResponse(url="/signin", status_code=302)


@router.get("/signin")
async def signin():
    """Sign in page"""
    try:
        return FileResponse("frontend/signin.html")
    except:
        return JSONResponse(content={"message": "Sign in page not found"})


@router.get("/change-password")
async def change_password_page(
    current_user: User = Depends(get_current_user_allow_pending_rotation),
):
    """Change-password page.

    Uses the allow-pending dependency deliberately: this is the one page an
    account with a seeded or admin-assigned password must be able to open.
    It still requires a session — an unauthenticated caller raises 401, which
    the global handler turns into a redirect to /signin.
    """
    try:
        return FileResponse("frontend/change-password.html")
    except Exception:
        return JSONResponse(content={"message": "Change password page not found"})


@router.get("/admin/users")
async def admin_users(
    current_user: User = Depends(require_strict_access(allowed_roles=["admin"], require_pipeline_access=False))
):
    """
    Admin user management page - ADMIN ONLY
    
    **STRICT ACCESS CONTROL:**
    - User MUST be authenticated and active
    - User MUST have admin role
    - Backend validates ALL access - frontend cannot bypass
    """
    try:
        logger.info(f"[ADMIN_USERS] ✅ STRICT AUTH: Admin user {current_user.username} (ID: {current_user.id}) accessing users page")
        return FileResponse("frontend/admin/users.html")
    except FileNotFoundError:
        return JSONResponse(content={"message": "Admin users page not found"})


@router.get("/admin/background-tasks")
async def admin_background_tasks(
    request: Request,
    authorization: Optional[str] = Header(None, alias="Authorization"),
    db: AsyncSession = Depends(get_db)
):
    """Background tasks monitoring page - accessible to all users"""
    try:
        # Check authentication
        token = authorization.replace("Bearer ", "") if authorization else None
        if not token:
            # Try to get from cookie
            token = request.cookies.get("__Host-access_token") or request.cookies.get("access_token")
        
        if not token:
            return RedirectResponse(url="/signin", status_code=302)
        
        # Verify token and get user
        try:
            payload = AuthService.decode_token(token)
            if not payload:
                return RedirectResponse(url="/signin", status_code=302)
            
            user_id_str = payload.get("sub")
            if user_id_str:
                user_id = int(user_id_str)
                result = await db.execute(select(User).where(User.id == user_id))
                current_user = result.scalar_one_or_none()
                
                if not current_user:
                    return RedirectResponse(url="/signin", status_code=302)
                
                # All authenticated users can access (they'll see filtered tasks)
                return FileResponse("frontend/admin/background-tasks.html")
            else:
                return RedirectResponse(url="/signin", status_code=302)
        except Exception as e:
            logger.warning(f"[BACKGROUND_TASKS] Token verification failed: {e}")
            return RedirectResponse(url="/signin", status_code=302)
    except Exception as e:
        logger.error(f"[BACKGROUND_TASKS] Error serving page: {e}")
        return JSONResponse(content={"message": "Background tasks page not found"})


@router.get("/admin/audit")
async def admin_audit(
    current_user: User = Depends(require_strict_access(allowed_roles=["admin"], require_pipeline_access=False))
):
    """
    Admin chatbot audit log page - ADMIN ONLY
    
    **STRICT ACCESS CONTROL:**
    - User MUST be authenticated and active
    - User MUST have admin role
    - Backend validates ALL access - frontend cannot bypass
    """
    try:
        logger.info(f"[ADMIN_AUDIT] ✅ STRICT AUTH: Admin user {current_user.username} (ID: {current_user.id}) accessing audit page")
        return FileResponse("frontend/admin/audit.html")
    except FileNotFoundError:
        return JSONResponse(content={"message": "Audit page not found"})


@router.get("/admin/pipelines")
async def admin_pipelines(
    current_user: User = Depends(require_strict_access(allowed_roles=["admin"], require_pipeline_access=False))
):
    """
    Admin pipeline management page - ADMIN ONLY
    
    **STRICT ACCESS CONTROL:**
    - User MUST be authenticated and active
    - User MUST have admin role
    - Backend validates ALL access - frontend cannot bypass
    """
    try:
        logger.info(f"[ADMIN_PIPELINES] ✅ STRICT AUTH: Admin user {current_user.username} (ID: {current_user.id}) accessing pipelines page")
        return FileResponse("frontend/admin/pipelines.html")
    except FileNotFoundError:
        return JSONResponse(content={"message": "Admin pipelines page not found"})


@router.get("/admin/tutorial")
async def admin_tutorial(
    current_user: User = Depends(require_strict_access(allowed_roles=["admin"], require_pipeline_access=False))
):
    """
    Admin tutorial and learning guide page - ADMIN ONLY
    
    **STRICT ACCESS CONTROL:**
    - User MUST be authenticated and active
    - User MUST have admin role
    - Backend validates ALL access - frontend cannot bypass
    """
    try:
        logger.info(f"[ADMIN_TUTORIAL] ✅ STRICT AUTH: Admin user {current_user.username} (ID: {current_user.id}) accessing tutorial page")
        return FileResponse("frontend/admin/tutorial.html")
    except FileNotFoundError:
        return JSONResponse(content={"message": "Tutorial page not found"})


@router.get("/admin/settings")
async def admin_settings(
    current_user: User = Depends(require_strict_access(allowed_roles=["admin"], require_pipeline_access=False))
):
    """
    Admin settings management page - ADMIN ONLY
    
    **STRICT ACCESS CONTROL:**
    - User MUST be authenticated and active
    - User MUST have admin role
    - Backend validates ALL access - frontend cannot bypass
    """
    try:
        logger.info(f"[ADMIN_SETTINGS] ✅ STRICT AUTH: Admin user {current_user.username} (ID: {current_user.id}) accessing settings page")
        return FileResponse("frontend/admin/settings.html")
    except FileNotFoundError:
        return JSONResponse(content={"message": "Settings page not found"})


@router.get("/admin/search")
async def admin_search(
    current_user: User = Depends(require_strict_access(allowed_roles=["admin"], require_pipeline_access=False))
):
    """
    Advanced Search Intelligence page - ADMIN ONLY
    
    **STRICT ACCESS CONTROL:**
    - User MUST be authenticated and active
    - User MUST have admin role
    - Backend validates ALL access - frontend cannot bypass
    """
    try:
        logger.info(f"[ADMIN_SEARCH] ✅ STRICT AUTH: Admin user {current_user.username} (ID: {current_user.id}) accessing search page")
        return FileResponse("frontend/admin/search.html")
    except FileNotFoundError:
        return JSONResponse(content={"message": "Search page not found"})


@router.get("/admin/search-history")
async def admin_search_history(
    current_user: User = Depends(require_strict_access(allowed_roles=["admin"], require_pipeline_access=False))
):
    """
    Search History page - ADMIN ONLY
    
    View and manage past search operations.
    """
    try:
        logger.info(f"[ADMIN_SEARCH_HISTORY] ✅ STRICT AUTH: Admin user {current_user.username} (ID: {current_user.id}) accessing search history page")
        return FileResponse("frontend/admin/search-history.html")
    except FileNotFoundError:
        return JSONResponse(content={"message": "Search history page not found"})


@router.get("/admin/intelligence")
async def admin_intelligence(
    current_user: User = Depends(require_strict_access(allowed_roles=["admin"], require_pipeline_access=False))
):
    """
    Intelligence Analysis page - ADMIN ONLY
    
    Advanced analytics for identity investigation:
    - Related Identities (co-appearance analysis)
    - Temporal Patterns (when do they appear)
    - Cross-Camera Tracking (movement across locations)
    """
    try:
        logger.info(f"[ADMIN_INTELLIGENCE] ✅ STRICT AUTH: Admin user {current_user.username} (ID: {current_user.id}) accessing intelligence page")
        return FileResponse("frontend/admin/intelligence.html")
    except FileNotFoundError:
        return JSONResponse(content={"message": "Intelligence page not found"})


@router.get("/admin/security-intelligence")
async def admin_security_intelligence(
    current_user: User = Depends(require_strict_access(allowed_roles=["admin"], require_pipeline_access=False))
):
    """
    Security Intelligence page - ADMIN ONLY
    
    Advanced security analysis:
    - Social Network Analysis
    - Suspicious Pattern Detection
    - Anomaly Detection
    - Threat Assessment
    """
    try:
        logger.info(f"[ADMIN_SECURITY_INTEL] ✅ STRICT AUTH: Admin user {current_user.username} (ID: {current_user.id}) accessing security intelligence page")
        return FileResponse("frontend/admin/security-intelligence.html")
    except FileNotFoundError:
        logger.warning(f"[ADMIN_SECURITY_INTEL] Security Intelligence page not found")
        return JSONResponse(content={"message": "Security Intelligence page not found"})
    except Exception as e:
        logger.error(f"[ADMIN_SECURITY_INTEL] Error serving Security Intelligence page: {e}")
        return JSONResponse(content={"message": f"Error: {str(e)}"}, status_code=500)

@router.get("/admin/watchlists")
async def admin_watchlists(
    current_user: User = Depends(require_strict_access(allowed_roles=["admin"], require_pipeline_access=False))
):
    """
    Watchlist management page - ADMIN ONLY
    
    **STRICT ACCESS CONTROL:**
    - User MUST be authenticated and active
    - User MUST have admin role
    - Backend validates ALL access - frontend cannot bypass
    """
    try:
        logger.info(f"[ADMIN_WATCHLISTS] ✅ STRICT AUTH: Admin user {current_user.username} (ID: {current_user.id}) accessing watchlists page")
        return FileResponse("frontend/admin/watchlists.html")
    except FileNotFoundError:
        return JSONResponse(content={"message": "Watchlists page not found"})


@router.get("/admin/ml-ops")
async def admin_ml_ops(
    current_user: User = Depends(require_strict_access(allowed_roles=["admin"], require_pipeline_access=False))
):
    """ML Operations page — anomaly pipeline, shadow evaluation, drift, labels."""
    return FileResponse("frontend/admin/ml-ops.html")


@router.get("/admin/ml-model")
async def admin_ml_model(
    current_user: User = Depends(require_strict_access(allowed_roles=["admin"], require_pipeline_access=False)),
    db: AsyncSession = Depends(get_db)
):
    """
    ML Similarity Model management page - ADMIN ONLY (strict auth).

    Serves the static page only. Operational model status is loaded by the
    frontend through the authenticated no-store API
    (/api/admin/merge-suggestions/model-status) — no server-side JSON is
    concatenated into a <script> tag, which removes the script-context
    injection surface entirely.
    """
    try:
        logger.info(f"[ADMIN_ML_MODEL] admin user_id={current_user.id} accessing ML model page")
        html_content = await read_file_off_loop("frontend/admin/ml-model.html")
        return HTMLResponse(content=html_content)
    except FileNotFoundError:
        logger.error("[ML_MODEL] HTML file not found")
        return JSONResponse(status_code=404, content={"message": "ML Model page not found"})
    except Exception as e:
        logger.error(f"[ML_MODEL] Error serving page: {e}", exc_info=True)
        return JSONResponse(status_code=500, content={"message": "Error loading ML Model page"})


@router.get("/admin/live-alerts")
async def admin_live_alerts(
    request: Request,
    authorization: Optional[str] = Header(None, alias="Authorization"),
    db: AsyncSession = Depends(get_db)
):
    """
    Live alerts management page.
    
    **ACCESS CONTROL (Backend-Enforced):**
    ======================================
    ✅ ADMIN USERS: Always allowed (full access)
    ✅ REGULAR USERS WITH PIPELINE ACCESS: Allowed (can see/manage alerts for identities from their assigned pipelines)
    ❌ REGULAR USERS WITHOUT PIPELINE ACCESS: Redirected to /dashboard
    
    **How to Grant Access:**
    - Admin must go to: Admin → Users → Edit User
    - Check at least ONE pipeline in "VIDEO PIPELINE ACCESS" section
    - Click "Save"
    - User will immediately have access to Live Alerts page
    
    **Security:**
    - All access decisions are made by the backend (no frontend logic)
    - Token validation and user verification performed server-side
    - Pipeline access checked via user_pipeline_access table
    """
    from backend.auth.auth_service import AuthService
    from fastapi.responses import FileResponse, RedirectResponse, JSONResponse
    
    # Try to get token from Authorization header first
    token = None
    if authorization and authorization.startswith("Bearer "):
        token = authorization.replace("Bearer ", "").strip()
    
    # If no token in header, check cookies
    if not token:
        token = request.cookies.get("__Host-access_token") or request.cookies.get("access_token")
    
    # If still no token, redirect to signin
    if not token:
        return RedirectResponse(url="/signin", status_code=302)
    
    # Validate token and check user access (BACKEND HANDLES ALL LOGIC)
    try:
        payload = AuthService.decode_token(token)
        if not payload:
            return RedirectResponse(url="/signin", status_code=302)
        
        user_id_str = payload.get("sub")
        if not user_id_str:
            return RedirectResponse(url="/signin", status_code=302)
        
        user_id = int(user_id_str)
        result = await db.execute(select(User).where(User.id == user_id))
        current_user = result.scalar_one_or_none()
        
        if not current_user:
            return RedirectResponse(url="/signin", status_code=302)
        
        if not current_user.is_active:
            return RedirectResponse(url="/signin", status_code=302)
        
        # BACKEND ACCESS CONTROL LOGIC
        # ============================================
        # ACCESS RULES:
        # 1. Admin users: ALWAYS ALLOWED
        # 2. Regular users: ALLOWED if they have at least ONE pipeline assigned
        # 3. Regular users without pipelines: REDIRECTED to /dashboard
        # ============================================
        
        if current_user.role != "admin":
            # Check if user has pipeline access
            user_pipelines = await AuthService.get_user_pipelines(current_user.id, db)
            if not user_pipelines:
                logger.info(f"[LIVE_ALERTS] User {current_user.username} (ID: {current_user.id}) has no pipeline access, redirecting to /dashboard")
                return RedirectResponse(url="/dashboard", status_code=302)
        
        # User has access - serve the page
        try:
            return FileResponse("frontend/admin/live-alerts.html")
        except:
            return JSONResponse(content={"message": "Live alerts page not found"})
            
    except Exception as e:
        logger.error(f"[LIVE_ALERTS] Error checking access: {e}", exc_info=True)
        return RedirectResponse(url="/signin", status_code=302)


@router.get("/admin/ingest-credentials")
async def ingest_credentials_page(
    current_user: User = Depends(require_strict_access(allowed_roles=["admin"], require_pipeline_access=False))
):
    """
    Ingest credential management page - ADMIN ONLY

    Issue a named credential per external sender, and revoke by deleting it.
    The page never receives a stored token: the raw value exists only in the
    single POST response that mints it.
    """
    try:
        logger.info(f"[INGEST_CREDENTIALS] ✅ STRICT AUTH: Admin user {current_user.username} (ID: {current_user.id}) accessing ingest credentials page")
        return FileResponse("frontend/admin/ingest-credentials.html")
    except FileNotFoundError:
        return JSONResponse(content={"message": "Ingest credentials page not found"})


@router.get("/admin/logs")
async def logs_page(
    current_user: User = Depends(require_strict_access(allowed_roles=["admin"], require_pipeline_access=False))
):
    """
    Error logs viewer page - ADMIN ONLY
    
    **STRICT ACCESS CONTROL:**
    - User MUST be authenticated and active
    - User MUST have admin role
    - Backend validates ALL access - frontend cannot bypass
    """
    try:
        logger.info(f"[ADMIN_LOGS] ✅ STRICT AUTH: Admin user {current_user.username} (ID: {current_user.id}) accessing logs page")
        return FileResponse("frontend/admin/logs.html")
    except FileNotFoundError:
        return JSONResponse(content={"message": "Logs page not found"})


@router.get("/tracking-people")
async def tracking_people(current_user: User = Depends(require_chatbot_access())):
    """People Tracking Intelligence page (requires chatbot access).

    The docstring has always said "requires chatbot access", but the route
    carried NO dependency at all — every other admin page in this module is
    gated and this one was reachable by anyone who knew the path. Code now
    matches the stated contract.
    """
    try:
        return FileResponse("frontend/tracking-people.html")
    except FileNotFoundError:
        return JSONResponse(content={"message": "Tracking page not found"})


@router.get("/")
async def root():
    """Redirect root to signin page - all users must sign in first"""
    return RedirectResponse(url="/signin", status_code=302)


@router.get("/docs/overview")
async def api_overview():
    """Comprehensive API overview with examples for all endpoints"""
    return {
        "overview": "Face Recognition Service API",
        "version": "2.0.0",
        "base_url": "http://localhost:8000",
        "authentication": {
            "type": "Bearer Token (JWT)",
            "header": "Authorization: Bearer <token>",
            "login_endpoint": "/api/auth/login",
            "note": "Most endpoints require authentication. Admin endpoints require admin role."
        },
        "quick_start": {
            "1_webhook": {
                "description": "Send images for face recognition",
                "method": "POST",
                "endpoint": "/webhook/{pipeline_id}",
                "curl_example": """curl -X POST "http://localhost:8000/webhook/pipeline123" \\
  -H "Content-Type: application/json" \\
  -d '{
    "predictions": [
      {
        "class_name": "person",
        "bbox": [100, 100, 200, 200],
        "confidence": 0.95
      }
    ],
    "image": "base64_encoded_image_data"
  }'"""
            },
            "2_check_detections": {
                "description": "Get recent detections",
                "method": "GET",
                "endpoint": "/api/detections/{pipeline_id}",
                "curl_example": 'curl -H "Authorization: Bearer <token>" "http://localhost:8000/api/detections/pipeline123"'
            },
            "3_upload_person": {
                "description": "Add new person to database",
                "method": "POST",
                "endpoint": "/api/upload-person",
                "curl_example": """curl -X POST "http://localhost:8000/api/upload-person" \\
  -H "Authorization: Bearer <token>" \\
  -F "person_name=John Doe" \\
  -F "photo=@/path/to/photo.jpg" """
            }
        },
        "endpoint_categories": {
            "authentication": {
                "description": "User authentication and authorization",
                "endpoints": [
                    {
                        "method": "POST",
                        "path": "/api/auth/login",
                        "description": "Login and get access token",
                        "auth_required": False
                    },
                    {
                        "method": "POST",
                        "path": "/api/auth/logout",
                        "description": "Logout and invalidate token",
                        "auth_required": True
                    },
                    {
                        "method": "GET",
                        "path": "/api/auth/me",
                        "description": "Get current user information",
                        "auth_required": True
                    }
                ]
            },
            "webhook": {
                "description": "Receive images from pipelines for face recognition",
                "endpoints": [
                    {
                        "method": "POST",
                        "path": "/webhook/{pipeline_id}",
                        "description": "Send image with detections for processing",
                        "auth_required": False
                    }
                ]
            },
            "detections": {
                "description": "Query face detections and recognition results",
                "endpoints": [
                    {
                        "method": "GET",
                        "path": "/api/detections/{pipeline_id}",
                        "description": "Get recent detections for a pipeline",
                        "auth_required": True
                    },
                    {
                        "method": "GET",
                        "path": "/api/detections",
                        "description": "Get all recent detections",
                        "auth_required": True
                    }
                ]
            },
            "person_management": {
                "description": "Manage known persons in the database",
                "endpoints": [
                    {
                        "method": "POST",
                        "path": "/api/upload-person",
                        "description": "Add a new person to the database",
                        "auth_required": True
                    },
                    {
                        "method": "GET",
                        "path": "/api/persons",
                        "description": "List all persons in database",
                        "auth_required": True
                    },
                    {
                        "method": "DELETE",
                        "path": "/api/persons/{person_id}",
                        "description": "Delete a person from database",
                        "auth_required": True,
                        "admin_only": True
                    }
                ]
            },
            "identity_management": {
                "description": "Advanced identity management for unknown/known faces (Admin or users with pipeline access)",
                "endpoints": [
                    {
                        "method": "GET",
                        "path": "/api/admin/unknown",
                        "description": "List unknown identities with pagination and filters",
                        "auth_required": True,
                        "admin_only": False,
                        "access_note": "Admin: sees all. Regular users: only identities from their assigned pipelines",
                        "query_params": ["page", "page_size", "date_from", "date_to", "pipeline_id", "min_appearances"]
                    },
                    {
                        "method": "GET",
                        "path": "/api/admin/identity/{identity_id}",
                        "description": "Get detailed identity information including timeline",
                        "auth_required": True,
                        "admin_only": False,
                        "access_note": "Admin: all identities. Regular users: only identities from their assigned pipelines"
                    },
                    {
                        "method": "POST",
                        "path": "/api/admin/unknown/{identity_id}/promote",
                        "description": "Promote an unknown identity to known status",
                        "auth_required": True,
                        "admin_only": False,
                        "access_note": "Admin: can promote any. Regular users: only identities from their assigned pipelines",
                        "body": {
                            "display_name": "string (required)",
                            "person_code": "string (optional)",
                            "decision": "string (required) - 'create_new'"
                        }
                    },
                    {
                        "method": "POST",
                        "path": "/api/admin/identities/merge",
                        "description": "Merge two identities into one",
                        "auth_required": True,
                        "admin_only": False,
                        "access_note": "Admin: can merge any. Regular users: both identities must be from their assigned pipelines",
                        "body": {
                            "from_identity_id": "string (required)",
                            "to_identity_id": "string (required)",
                            "notes": "string (optional)",
                            "decision": "string (required) - 'merge_existing'"
                        }
                    },
                    {
                        "method": "POST",
                        "path": "/api/admin/identities/merge-multiple",
                        "description": "Merge multiple identities (3+) into one efficiently with smart target selection (O(n) time complexity)",
                        "auth_required": True,
                        "admin_only": False,
                        "access_note": "Admin: can merge any. Regular users: all identities must be from their assigned pipelines",
                        "body": {
                            "identity_ids": "array of strings (required, min 2)",
                            "target_identity_id": "string (optional, null = auto-select best identity)",
                            "notes": "string (optional)"
                        },
                        "features": [
                            "Automatically finds best target identity (most appearances, best quality snapshot)",
                            "O(n) time complexity - efficient for large merges",
                            "Batch operation - merges all identities in single transaction",
                            "Smart selection algorithm with scoring system"
                        ]
                    },
                    {
                        "method": "POST",
                        "path": "/api/search/by-image",
                        "description": "Search for identities by uploading an image",
                        "auth_required": True,
                        "admin_only": True,
                        "form_data": {
                            "image": "file (required)",
                            "scope": "string (known|unknown|both, default: both)",
                            "top_k": "integer (default: 10)",
                            "date_from": "string (optional)",
                            "date_to": "string (optional)",
                            "pipeline_id": "string (optional)"
                        }
                    },
                    {
                        "method": "POST",
                        "path": "/api/search/advanced",
                        "description": "Advanced multi-face search with quality scoring, watchlist checking, and negative search",
                        "auth_required": True,
                        "admin_only": True,
                        "form_data": {
                            "image": "file (required)",
                            "scope": "string (known|unknown|both, default: both)",
                            "top_k": "integer (default: 10)",
                            "min_quality": "float (optional, 0-1)",
                            "check_watchlist": "boolean (default: true)",
                            "exclude_identity_ids": "string (optional, comma-separated UUIDs)",
                            "exclude_watchlist_ids": "string (optional, comma-separated watchlist IDs)",
                            "date_from": "string (optional)",
                            "date_to": "string (optional)",
                            "pipeline_id": "string (optional)"
                        },
                        "features": [
                            "Multi-face detection (searches all faces in image)",
                            "Face quality scoring (blur, lighting, angle, size)",
                            "Watchlist checking (VIP, Threat, POI)",
                            "Negative search (exclude identities/watchlists)",
                            "Confidence bands (Very High, High, Medium, Low)",
                            "Automatic search history logging"
                        ]
                    },
                    {
                        "method": "POST",
                        "path": "/api/search/batch",
                        "description": "Batch search multiple images at once",
                        "auth_required": True,
                        "admin_only": True,
                        "form_data": {
                            "images": "array of files (required, max 100)",
                            "scope": "string (known|unknown|both, default: both)",
                            "top_k": "integer (default: 5)",
                            "min_quality": "float (optional)",
                            "check_watchlist": "boolean (default: true)"
                        },
                        "features": [
                            "Process multiple images in parallel",
                            "Aggregated results with unique identity tracking",
                            "Watchlist alerts across all images",
                            "Progress tracking per image"
                        ]
                    },
                    {
                        "method": "POST",
                        "path": "/api/search/quality-check",
                        "description": "Check image quality without performing search",
                        "auth_required": True,
                        "admin_only": True,
                        "form_data": {
                            "image": "file (required)"
                        },
                        "returns": {
                            "overall_score": "float (0-1)",
                            "band": "string (excellent|good|moderate|poor|unusable)",
                            "details": "object (blur, lighting, face_size, angle)",
                            "warnings": "array of strings",
                            "proceed_recommendation": "boolean"
                        }
                    },
                    {
                        "method": "GET",
                        "path": "/api/search/config",
                        "description": "Get search configuration (quality thresholds, confidence bands)",
                        "auth_required": True,
                        "admin_only": True
                    },
                    {
                        "method": "GET",
                        "path": "/api/search/history",
                        "description": "Get search history with filters",
                        "auth_required": True,
                        "admin_only": True,
                        "query_params": {
                            "days_back": "integer (default: 30)",
                            "search_type": "string (optional: single|multi|batch)",
                            "limit": "integer (default: 100, max: 500)",
                            "offset": "integer (default: 0)"
                        }
                    },
                    {
                        "method": "GET",
                        "path": "/api/search/history/export",
                        "description": "Export search history to CSV or JSON",
                        "auth_required": True,
                        "admin_only": True,
                        "query_params": {
                            "format": "string (csv|json, default: csv)",
                            "days_back": "integer (default: 30)"
                        }
                    },
                    {
                        "method": "POST",
                        "path": "/api/search/export",
                        "description": "Export search results to CSV, JSON, or PDF",
                        "auth_required": True,
                        "admin_only": True,
                        "query_params": {
                            "format": "string (csv|json|pdf, default: csv)",
                            "include_images": "boolean (default: false, JSON only)",
                            "include_quality": "boolean (default: true)"
                        },
                        "body": "Search results JSON from /api/search/advanced"
                    },
                    {
                        "method": "POST",
                        "path": "/api/search/batch/export",
                        "description": "Export batch search results to CSV or JSON",
                        "auth_required": True,
                        "admin_only": True,
                        "query_params": {
                            "format": "string (csv|json, default: csv)"
                        },
                        "body": "Batch search results JSON from /api/search/batch"
                    },
                    {
                        "method": "GET",
                        "path": "/api/admin/merge-suggestions",
                        "description": "Get pending merge suggestions",
                        "auth_required": True,
                        "admin_only": False,
                        "access_note": "Admin: sees all suggestions. Regular users: only suggestions for identities from their assigned pipelines"
                    },
                    {
                        "method": "POST",
                        "path": "/api/admin/merge-suggestions/{suggestion_id}/approve",
                        "description": "Approve and execute a merge suggestion",
                        "auth_required": True,
                        "admin_only": False,
                        "access_note": "Admin: can approve any. Regular users: only suggestions for identities from their assigned pipelines"
                    },
                    {
                        "method": "POST",
                        "path": "/api/admin/merge-suggestions/{suggestion_id}/reject",
                        "description": "Reject a merge suggestion",
                        "auth_required": True,
                        "admin_only": False,
                        "access_note": "Admin: can reject any. Regular users: only suggestions for identities from their assigned pipelines"
                    },
                    {
                        "method": "GET",
                        "path": "/api/admin/identities/status",
                        "description": "Check identity service status and availability",
                        "auth_required": True,
                        "admin_only": True
                    }
                ]
            },
            "live_alerts": {
                "description": "Real-time alerts for tracking specific identities when detected",
                "endpoints": [
                    {
                        "method": "GET",
                        "path": "/api/live-alerts/defaults/{identity_id}",
                        "description": "Get default settings for creating a live alert (backend provides all defaults including identity ID)",
                        "auth_required": True,
                        "admin_only": True,
                        "note": "Identity ID is always displayed in the response and form"
                    },
                    {
                        "method": "POST",
                        "path": "/api/live-alerts",
                        "description": "Create a live alert to get notified when an identity is detected",
                        "auth_required": True,
                        "admin_only": True,
                        "body": {
                            "name": "string (required)",
                            "identity_id": "string (required, UUID)",
                            "min_similarity": "float (default: 0.75)",
                            "notify_dashboard": "boolean (default: true)",
                            "sound_alert": "boolean (default: true)",
                            "auto_capture_snapshot": "boolean (default: true)"
                        }
                    },
                    {
                        "method": "GET",
                        "path": "/api/live-alerts",
                        "description": "List all live alerts",
                        "auth_required": True,
                        "admin_only": True
                    },
                    {
                        "method": "GET",
                        "path": "/api/live-alerts/{alert_id}",
                        "description": "Get alert details",
                        "auth_required": True,
                        "admin_only": True
                    },
                    {
                        "method": "PUT",
                        "path": "/api/live-alerts/{alert_id}",
                        "description": "Update alert settings",
                        "auth_required": True,
                        "admin_only": True
                    },
                    {
                        "method": "POST",
                        "path": "/api/live-alerts/{alert_id}/pause",
                        "description": "Pause an alert",
                        "auth_required": True,
                        "admin_only": True
                    },
                    {
                        "method": "POST",
                        "path": "/api/live-alerts/{alert_id}/resume",
                        "description": "Resume a paused alert",
                        "auth_required": True,
                        "admin_only": True
                    },
                    {
                        "method": "DELETE",
                        "path": "/api/live-alerts/{alert_id}",
                        "description": "Delete an alert",
                        "auth_required": True,
                        "admin_only": True
                    }
                ]
            },
            "intelligence": {
                "description": "Intelligence analysis endpoints for identity investigation",
                "endpoints": [
                    {
                        "method": "GET",
                        "path": "/api/identities/{identity_id}/related",
                        "description": "Get related identities (people who frequently appear together)",
                        "auth_required": True,
                        "admin_only": True,
                        "query_params": {
                            "min_co_appearances": "integer (optional, default from config)",
                            "time_window_minutes": "integer (optional, default: 5, max: 60)",
                            "limit": "integer (default: 20, max: 100)"
                        },
                        "returns": {
                            "identity_id": "string",
                            "display_name": "string or null",
                            "identity_type": "string (known|unknown)",
                            "co_appearance_count": "integer",
                            "co_appearance_percentage": "float",
                            "relationship_strength": "string (strong|moderate|weak)",
                            "common_pipelines": "array of strings",
                            "first_co_appearance": "string (ISO datetime)",
                            "last_co_appearance": "string (ISO datetime)"
                        }
                    },
                    {
                        "method": "POST",
                        "path": "/api/identities/{identity_id}/related/refresh",
                        "description": "Refresh and recalculate relationship cache for an identity",
                        "auth_required": True,
                        "admin_only": True
                    },
                    {
                        "method": "GET",
                        "path": "/api/identities/{identity_id}/temporal-patterns",
                        "description": "Get temporal patterns (when and where identity typically appears)",
                        "auth_required": True,
                        "admin_only": True,
                        "query_params": {
                            "days_back": "integer (default: 90, max: 365)"
                        },
                        "returns": {
                            "identity_id": "string",
                            "hourly_distribution": "object (0-23 hour counts)",
                            "daily_distribution": "object (monday-sunday counts)",
                            "peak_hours": "array of integers",
                            "peak_days": "array of strings",
                            "most_common_pipelines": "array of objects",
                            "total_appearances": "integer",
                            "first_appearance": "string (ISO datetime)",
                            "last_appearance": "string (ISO datetime)",
                            "average_appearances_per_day": "float"
                        }
                    },
                    {
                        "method": "GET",
                        "path": "/api/identities/{identity_id}/cross-camera",
                        "description": "Get cross-camera tracking (movement path across cameras)",
                        "auth_required": True,
                        "admin_only": True,
                        "query_params": {
                            "date": "string (optional, YYYY-MM-DD)",
                            "days_back": "integer (default: 7, max: 30, used if date not provided)"
                        },
                        "returns": {
                            "identity_id": "string",
                            "display_name": "string or null",
                            "date": "string (YYYY-MM-DD)",
                            "movements": "array of movement objects",
                            "total_cameras": "integer",
                            "first_seen": "string (ISO datetime)",
                            "last_seen": "string (ISO datetime)",
                            "total_duration_minutes": "integer"
                        }
                    },
                    {
                        "method": "GET",
                        "path": "/api/identities/{identity_id}/timeline",
                        "description": "Get simplified movement timeline for dashboard display",
                        "auth_required": True,
                        "admin_only": True,
                        "query_params": {
                            "hours_back": "integer (default: 24, max: 168)"
                        }
                    },
                    {
                        "method": "GET",
                        "path": "/api/identities/{identity_id}/analyze",
                        "description": "Get complete intelligence analysis (related identities, temporal patterns, cross-camera tracking combined)",
                        "auth_required": True,
                        "admin_only": True
                    }
                ]
            },
            "watchlists": {
                "description": "Manage watchlists (VIP, Threat, POI) and add identities to them",
                "endpoints": [
                    {
                        "method": "GET",
                        "path": "/api/watchlists",
                        "description": "List all watchlists",
                        "auth_required": True,
                        "admin_only": True
                    },
                    {
                        "method": "POST",
                        "path": "/api/watchlists",
                        "description": "Create a new watchlist",
                        "auth_required": True,
                        "admin_only": True
                    },
                    {
                        "method": "GET",
                        "path": "/api/watchlists/add-identity/{identity_id}/defaults",
                        "description": "Get default settings and available watchlists for adding an identity (backend provides all defaults)",
                        "auth_required": True,
                        "admin_only": True,
                        "note": "Shows which watchlists the identity is already on"
                    },
                    {
                        "method": "POST",
                        "path": "/api/watchlists/{watchlist_id}/entries",
                        "description": "Add an identity to a watchlist",
                        "auth_required": True,
                        "admin_only": True,
                        "body": {
                            "identity_id": "string (required, UUID)",
                            "priority": "string (low|normal|high|critical, default: normal)",
                            "notes": "string (optional)",
                            "action_instructions": "string (optional)",
                            "expires_at": "string (optional, ISO format)"
                        }
                    },
                    {
                        "method": "GET",
                        "path": "/api/watchlists/{watchlist_id}/entries",
                        "description": "List all entries (identities) on a watchlist",
                        "auth_required": True,
                        "admin_only": True
                    },
                    {
                        "method": "DELETE",
                        "path": "/api/watchlists/{watchlist_id}/entries/{identity_id}",
                        "description": "Remove an identity from a watchlist",
                        "auth_required": True,
                        "admin_only": True
                    },
                    {
                        "method": "GET",
                        "path": "/api/identities/{identity_id}/watchlists",
                        "description": "Get all watchlists an identity is on",
                        "auth_required": True,
                        "admin_only": True
                    }
                ]
            },
            "statistics": {
                "description": "Get system statistics and metrics",
                "endpoints": [
                    {
                        "method": "GET",
                        "path": "/api/stats",
                        "description": "Get overall system statistics",
                        "auth_required": True
                    },
                    {
                        "method": "GET",
                        "path": "/api/stats/pipeline/{pipeline_id}",
                        "description": "Get statistics for a specific pipeline",
                        "auth_required": True
                    }
                ]
            },
            "user_management": {
                "description": "User management endpoints (Admin only)",
                "endpoints": [
                    {
                        "method": "GET",
                        "path": "/api/admin/users",
                        "description": "List all users",
                        "auth_required": True,
                        "admin_only": True
                    },
                    {
                        "method": "POST",
                        "path": "/api/admin/users",
                        "description": "Create a new user",
                        "auth_required": True,
                        "admin_only": True
                    },
                    {
                        "method": "PUT",
                        "path": "/api/admin/users/{user_id}",
                        "description": "Update a user",
                        "auth_required": True,
                        "admin_only": True
                    },
                    {
                        "method": "DELETE",
                        "path": "/api/admin/users/{user_id}",
                        "description": "Delete a user",
                        "auth_required": True,
                        "admin_only": True
                    }
                ]
            },
            "audit_logs": {
                "description": "Audit log endpoints (Admin only)",
                "endpoints": [
                    {
                        "method": "GET",
                        "path": "/api/admin/audit",
                        "description": "Get audit log entries with filters and pagination",
                        "auth_required": True,
                        "admin_only": True
                    }
                ]
            },
            "health": {
                "description": "Health check and system status",
                "endpoints": [
                    {
                        "method": "GET",
                        "path": "/api/health",
                        "description": "Check system health",
                        "auth_required": False
                    },
                    {
                        "method": "GET",
                        "path": "/ping",
                        "description": "Simple ping endpoint",
                        "auth_required": False
                    }
                ]
            }
        },
        "examples": {
            "identity_management": {
                "list_unknown": {
                    "description": "List unknown identities with filters",
                    "curl": """curl -X GET "http://localhost:8000/api/admin/unknown?page=1&page_size=20&min_appearances=2" \\
  -H "Authorization: Bearer <admin_token>" """
                },
                "promote_identity": {
                    "description": "Promote unknown identity to known",
                    "curl": """curl -X POST "http://localhost:8000/api/admin/unknown/{identity_id}/promote" \\
  -H "Authorization: Bearer <admin_token>" \\
  -H "Content-Type: application/json" \\
  -d '{
    "display_name": "John Doe",
    "person_code": "JD001"
  }'"""
                },
                "search_by_image": {
                    "description": "Search for identities by image",
                    "curl": """curl -X POST "http://localhost:8000/api/search/by-image" \\
  -H "Authorization: Bearer <admin_token>" \\
  -F "image=@/path/to/image.jpg" \\
  -F "scope=both" \\
  -F "top_k=10" """
                },
                "merge_identities": {
                    "description": "Merge two identities into one",
                    "curl": """curl -X POST "http://localhost:8000/api/admin/identities/merge" \\
  -H "Authorization: Bearer <admin_token>" \\
  -H "Content-Type: application/json" \\
  -d '{
    "from_identity_id": "source-uuid",
    "to_identity_id": "target-uuid",
    "notes": "Optional notes"
  }'"""
                },
                "merge_multiple_identities": {
                    "description": "Merge multiple identities (3+) into one efficiently with smart target selection",
                    "curl": """curl -X POST "http://localhost:8000/api/admin/identities/merge-multiple" \\
  -H "Authorization: Bearer <admin_token>" \\
  -H "Content-Type: application/json" \\
  -d '{
    "identity_ids": ["uuid-1", "uuid-2", "uuid-3"],
    "target_identity_id": null,
    "notes": "Merging duplicate identities"
  }'"""
                },
                "get_live_alert_defaults": {
                    "description": "Get default settings for creating a live alert (backend provides all defaults including identity ID)",
                    "curl": """curl -X GET "http://localhost:8000/api/live-alerts/defaults/{identity_id}" \\
  -H "Authorization: Bearer <admin_token>" """
                },
                "create_live_alert": {
                    "description": "Create a live search alert to get notified when a tracked person is detected. Identity ID is always displayed in the form.",
                    "curl": """curl -X POST "http://localhost:8000/api/live-alerts" \\
  -H "Authorization: Bearer <admin_token>" \\
  -H "Content-Type: application/json" \\
  -d '{
    "name": "Track John Doe - Investigation #123",
    "identity_id": "uuid-of-identity",
    "min_similarity": 0.75,
    "notify_dashboard": true,
    "sound_alert": true,
    "auto_capture_snapshot": true
  }'"""
                },
                "get_watchlist_defaults": {
                    "description": "Get default settings and available watchlists for adding an identity (backend provides all defaults)",
                    "curl": """curl -X GET "http://localhost:8000/api/watchlists/add-identity/{identity_id}/defaults" \\
  -H "Authorization: Bearer <admin_token>" """
                },
                "add_to_watchlist": {
                    "description": "Add an identity to a watchlist (VIP, Threat, POI, etc.)",
                    "curl": """curl -X POST "http://localhost:8000/api/watchlists/{watchlist_id}/entries" \\
  -H "Authorization: Bearer <admin_token>" \\
  -H "Content-Type: application/json" \\
  -d '{
    "identity_id": "uuid-of-identity",
    "priority": "normal",
    "notes": "Optional notes",
    "action_instructions": "Optional instructions"
  }'"""
                },
                "advanced_search": {
                    "description": "Advanced multi-face search with quality scoring, watchlist checking, and negative search",
                    "curl": """curl -X POST "http://localhost:8000/api/search/advanced" \\
  -H "Authorization: Bearer <admin_token>" \\
  -F "image=@/path/to/image.jpg" \\
  -F "scope=both" \\
  -F "top_k=10" \\
  -F "check_watchlist=true" \\
  -F "exclude_identity_ids=uuid1,uuid2" \\
  -F "exclude_watchlist_ids=watchlist1" """
                },
                "batch_search": {
                    "description": "Batch search multiple images at once",
                    "curl": """curl -X POST "http://localhost:8000/api/search/batch" \\
  -H "Authorization: Bearer <admin_token>" \\
  -F "images=@/path/to/image1.jpg" \\
  -F "images=@/path/to/image2.jpg" \\
  -F "scope=both" \\
  -F "top_k=5" """
                },
                "get_search_history": {
                    "description": "Get search history with filters",
                    "curl": """curl -X GET "http://localhost:8000/api/search/history?days_back=30&search_type=batch&limit=50" \\
  -H "Authorization: Bearer <admin_token>" """
                },
                "export_search_results": {
                    "description": "Export search results to CSV, JSON, or PDF",
                    "curl": """curl -X POST "http://localhost:8000/api/search/export?format=pdf&include_quality=true" \\
  -H "Authorization: Bearer <admin_token>" \\
  -H "Content-Type: application/json" \\
  -d '{"search_id": "...", "faces": [...]}' """
                },
                "get_related_identities": {
                    "description": "Get related identities (people who frequently appear together)",
                    "curl": """curl -X GET "http://localhost:8000/api/identities/{identity_id}/related?min_co_appearances=3&time_window_minutes=5" \\
  -H "Authorization: Bearer <admin_token>" """
                },
                "get_temporal_patterns": {
                    "description": "Get temporal patterns (when and where identity typically appears)",
                    "curl": """curl -X GET "http://localhost:8000/api/identities/{identity_id}/temporal-patterns?days_back=90" \\
  -H "Authorization: Bearer <admin_token>" """
                },
                "get_cross_camera_tracking": {
                    "description": "Get cross-camera tracking (movement path across cameras)",
                    "curl": """curl -X GET "http://localhost:8000/api/identities/{identity_id}/cross-camera?date=2025-01-05" \\
  -H "Authorization: Bearer <admin_token>" """
                },
                "get_complete_analysis": {
                    "description": "Get complete intelligence analysis (all features combined)",
                    "curl": """curl -X GET "http://localhost:8000/api/identities/{identity_id}/analyze" \\
  -H "Authorization: Bearer <admin_token>" """
                }
            }
        },
        "documentation": {
            "swagger_ui": "/docs",
            "redoc": "/redoc",
            "openapi_json": "/openapi.json"
        },
        "notes": [
            "All admin endpoints require both authentication and admin role",
            "Identity management endpoints require the identity service to be initialized",
            "Image upload endpoints accept multipart/form-data",
            "Most endpoints return JSON responses",
            "Error responses follow standard HTTP status codes with detail messages"
        ]
    }


@router.get("/ping")
def ping():
    """Constant {"status": "ok"} with no authentication and no I/O — the cheapest possible reachability probe."""
    return {"status": "ok"}


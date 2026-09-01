"""
Production-Ready Face Recognition Service
==========================================
Main FastAPI application entry point.
"""

import os
import sys
import logging
import traceback

# Add parent directory to path for imports
parent_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if parent_dir not in sys.path:
    sys.path.insert(0, parent_dir)

from fastapi import FastAPI, Request, HTTPException, status, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import JSONResponse, RedirectResponse, FileResponse
from backend.auth.auth_service import get_current_user as _get_current_user_dep
from config import settings
from backend.lifespan import lifespan
import time

# Setup logging - unified to logs/app.log
from utils.logging import set_request_id, setup_logging
setup_logging(log_to_file=True)  # Enable file logging to logs/app.log
logger = logging.getLogger(__name__)

# Interactive docs publish the entire API surface, including every admin route,
# to anyone who can reach the port. Production serves none of them; the guard
# refuses to start if ENABLE_API_DOCS is left on there.
_docs_enabled = bool(settings.ENABLE_API_DOCS) and not settings.is_production
# docs_url/redoc_url are None even when docs ARE enabled: FastAPI's built-in
# pages load Swagger/ReDoc from cdn.jsdelivr.net and emit their bootstrap as
# an INLINE <script>. This deployment is offline and sends
# `script-src 'self'` without 'unsafe-inline', so both were blocked and the
# page rendered blank. Custom routes below serve the same UIs from vendored,
# same-origin assets, bootstrapped by a separate LOCAL script file
# (/frontend/js/docs-init.js) instead of an inline <script> — "external" only
# in the HTML sense of src=, never an internet host. openapi_url stays built-in.
_docs_url = None
_redoc_url = None
_openapi_url = "/openapi.json" if _docs_enabled else None

# Tag descriptions for the docs sidebar. A tag listed here but unused is
# harmless; an operation whose tag is missing here still renders, just without
# a description. Keep the names identical to the `tags=` on each router.
OPENAPI_TAGS = [
    {"name": "Authentication",
     "description": "Log in, inspect the current session, change your own "
                    "password, log out. Start here: every other bearer-token "
                    "endpoint needs a token from `POST /api/auth/login`. If "
                    "that login answers `rotation_required: true`, the account "
                    "still holds a seeded or admin-assigned password and every "
                    "endpoint outside this group returns "
                    "`403 PASSWORD_ROTATION_REQUIRED` until "
                    "`POST /api/auth/change-password` succeeds."},
    {"name": "Health",
     "description": "Liveness, readiness and per-component detail. "
                    "`/health/live` does no I/O. `/health/ready` returns 503 "
                    "**only** when the database or the models are unavailable "
                    "— a degraded cache or a stalled background service "
                    "deliberately does not take the API out of a load "
                    "balancer. `/health/detailed` is the one to read when "
                    "something is wrong."},
    {"name": "Ingest (Webhook)",
     "description": "Where cameras send frames. Authenticated with an **ingest "
                    "key**, not a bearer token: `X-Webhook-Key: <key>`. "
                    "Returns 202 when queued, 503 only when every image in the "
                    "request was rejected because the queue is full."},
    {"name": "Identity Management",
     "description": "The core domain: unknown faces, promotion to a known "
                    "person, merging duplicates, enrolment images, and "
                    "per-identity detail."},
    {"name": "Detections", "description": "Raw detection records, by pipeline or across all of them."},
    {"name": "Search",
     "description": "Search by image and advanced multi-face search, including "
                    "quality pre-checks and batch submission."},
    {"name": "Watchlists", "description": "Watchlists and their entries. Deletion is a reversible soft delete."},
    {"name": "Live Alerts",
     "description": "Rules that fire when a tracked person is seen, and the "
                    "trigger history they produce."},
    {"name": "Watchlist Alerts", "description": "Alerts raised by watchlist membership."},
    {"name": "Intelligence",
     "description": "Cross-camera tracking, related identities, temporal "
                    "patterns and movement timelines. `GET "
                    "/api/identities/{id}/cross-camera` returns **one track "
                    "per calendar day**, not one flat list."},
    {"name": "Intelligence - Advanced Features",
     "description": "Social-network analysis and the heavier derived analytics."},
    {"name": "Security Intelligence",
     "description": "Risk assessment, suspicious-pattern detection and "
                    "behavioural anomalies."},
    {"name": "Map Service",
     "description": "Server-rendered offline maps and GeoJSON. Tiles are "
                    "served locally (z10-16, Lebanon); a camera with no "
                    "coordinates is reported as `coordinates: null` and never "
                    "plotted at 0,0."},
    {"name": "ML Operations",
     "description": "The model lifecycle: features, labels, datasets, training "
                    "jobs, candidates, drift and audit. Training produces a "
                    "reviewable *candidate* rather than replacing the live "
                    "model."},
    {"name": "SQL Agent", "description": "Natural-language querying, executed under a read-only database role."},
    {"name": "Conversations", "description": "Chatbot conversation history, branching and feedback."},
    {"name": "Users", "description": "User accounts, roles, pipeline access and password resets. Admin only."},
    {"name": "Settings Management",
     "description": "Runtime settings. Security-critical keys cannot be "
                    "changed here — they are fixed at startup by the "
                    "configuration guard."},
    {"name": "Pipelines", "description": "Cameras: naming and geographic coordinates."},
    {"name": "Ingest Credentials", "description": "Issue and revoke per-camera ingest keys."},
    {"name": "Upload & Enrollment", "description": "Enrolling a person from images, and managing their photos."},
    {"name": "Enrollment Review", "description": "Reviewing and confirming pending enrolments."},
    {"name": "Background Tasks",
     "description": "Long-running jobs. Expensive operations return `202 + "
                    "job_id`; poll the job rather than blocking."},
    {"name": "Export", "description": "Batch export of search results and identity data."},
    {"name": "Retention", "description": "Data-retention policy and the jobs that enforce it."},
    {"name": "Audit", "description": "Audit trail, including chatbot query history."},
    {"name": "Logs", "description": "Reading and cleaning the application log. Admin only."},
    {"name": "Statistics", "description": "Aggregate counts and dashboard summaries."},
    {"name": "Cache", "description": "Cache inspection, warming and clearing. Admin only."},
    {"name": "System Management", "description": "Cleanup, face-tracker reset and circuit-breaker state. Admin only."},
    {"name": "Metrics", "description": "Prometheus exposition. IP-restricted at nginx."},
    {"name": "WebSocket", "description": "Live dashboard updates."},
    {"name": "Admin Tutorial", "description": "The in-app tutorial content, which always matches the running build."},
    {"name": "Admin Pages",
     "description": "Server-rendered HTML pages for the admin console. These "
                    "return pages, not JSON, and are listed here only for "
                    "completeness."},
]

# Create FastAPI app
app = FastAPI(
    title=settings.APP_NAME,
    version=settings.VERSION,
    lifespan=lifespan,
    docs_url=_docs_url,
    redoc_url=_redoc_url,
    openapi_url=_openapi_url,
    description="""
    Face Recognition Service API
    
    ## 🔐 Authentication
    
    Most endpoints require authentication using JWT Bearer tokens.
    
    **To authenticate:**
    1. **Login** at `POST /api/auth/login` with username and password
    2. **Receive** an access token in the response: `{"access_token": "eyJ...", "token_type": "bearer"}`
    3. **Include** the token in requests: `Authorization: Bearer <token>`
    
    **Example cURL:**
    ```bash
    # Step 1: Login
    curl -X POST "http://localhost/api/auth/login" \\
      -H "Content-Type: application/json" \\
      -d '{"username": "admin", "password": "your_password"}'
    
    # Step 2: Use token in requests
    curl -X GET "http://localhost/api/settings" \\
      -H "Authorization: Bearer YOUR_TOKEN_HERE"
    ```
    
    **In Swagger UI (/docs):**
    1. Click the **"Authorize"** button (🔓) at the top right
    2. Enter: `Bearer YOUR_TOKEN_HERE` (include the word "Bearer" and a space)
    3. Click **"Authorize"**
    4. All authenticated endpoints will now work
    
    **Admin Endpoints (require admin role):**
    - Settings Management (`/api/settings/*`) - Admin only
    - User Management (`/api/users/*`) - Admin only
    - Audit Logs (`/api/audit/*`) - Admin only
    
    **Identity Management Endpoints (`/api/admin/*`):**
    - **Admin**: Full access to all identities and merge suggestions
    - **Regular Users with Pipeline Access**: Can view/manage identities and merge suggestions from their assigned pipelines only
    - Endpoints include: `/api/admin/unknown`, `/api/admin/identity/{id}`, `/api/admin/merge-suggestions`, etc.
    
    **Intelligence Endpoints (`/api/identities/{id}/*`):**
    - Related Identities: Find people who appear together
    - Temporal Patterns: Analyze when identities typically appear
    - Cross-Camera Tracking: Track movement across cameras
    - Movement Timeline: Recent movement history
    - Complete Analysis: Comprehensive intelligence gathering
    
    **Map Service Endpoints (`/api/identities/{id}/map*`):**
    - Interactive Map: Generate HTML maps with Folium
    - GeoJSON Data: Get tracking data in GeoJSON format
    - Map Statistics: Monitor map service performance
    - Security Features: Pattern detection, risk scoring, threat visualization
    
    **Security Intelligence Endpoints (`/api/security/*`):**
    - Social Network Analysis: Build relationship networks
    - Suspicious Patterns: Detect behavioral patterns
    - Behavioral Anomalies: Identify unusual activities
    - Threat Assessment: Comprehensive risk evaluation
    
    **User Privileges:**
    - Get your privileges: `GET /api/auth/me/privileges`
    - Returns pipeline access, identity management permissions, and access flags
    - Used by frontend to customize UI and control feature access
    
    **Endpoints that need no bearer token:**
    - Health checks (`/health/live`, `/health/ready`, `/health`, `/health/detailed`)
    - Login (`/api/auth/login`)
    - Metrics (`/metrics`) — *unauthenticated at the application, but nginx
      restricts it by source IP, so it is not reachable from the internet*
    - This documentation (`/docs`, `/redoc`, `/openapi.json`) — *development
      deployments only; it is disabled in production because it publishes
      every admin route*

    **Camera ingest is authenticated separately**, with an ingest key rather
    than a bearer token: send `X-Webhook-Key: <key>` (or
    `Authorization: Bearer <key>`) to `POST /webhook/{pipeline_id}`. Use
    `GET /webhook/test` to check a key: 200 means it works, 401 means fix it.
    """,
    terms_of_service="https://example.com/terms/",
    contact={
        "name": "ITDIR-AI DEPARTMENT",
        "email": "support@example.com",
    },
    license_info={
        "name": "Proprietary",
    },
    # Swagger UI groups operations by tag and renders these descriptions above
    # each group. Without them every group is a bare name, and an operation
    # with no tag at all lands in "default" — 119 of 254 operations used to.
    # Order here is the order shown in the sidebar: the things an operator
    # reaches for first, then the analytical surface, then internals.
    openapi_tags=OPENAPI_TAGS,
)

# SQL Agent router (if available)
try:
    from sql_agent.api import router as sql_agent_router
    from sql_agent.api import sql_agent_websocket, set_sql_agent_instance
    
    app.include_router(sql_agent_router)
    logger.info("✅ SQL Agent API router included in FastAPI application")
    
    # Register WebSocket separately to maintain /ws/sql-agent path
    try:
        app.websocket("/ws/sql-agent")(sql_agent_websocket)
        logger.info("✅ SQL Agent WebSocket registered at /ws/sql-agent")
    except Exception as e:
        logger.warning(f"⚠️ SQL Agent WebSocket registration failed: {e}")
except Exception as e:
    logger.warning(f"⚠️ SQL Agent API router not included (not available): {e}")

# Structured request middleware: request_id + duration on every response,
# WARN on slow requests (>2s), DEBUG-level per-request logs (the previous
# version logged 2 INFO lines per request plus FULL webhook headers —
# including Authorization — which is both noisy and a credential leak).
import uuid as _uuid

_QUIET_PATHS = {"/health", "/health/live", "/health/ready", "/metrics", "/favicon.ico"}
_SLOW_REQUEST_SECONDS = 2.0


@app.middleware("http")
async def log_requests(request: Request, call_next):
    start_time = time.time()
    request_id = request.headers.get("x-request-id") or _uuid.uuid4().hex[:12]
    # Make the id available to route handlers (e.g. auth logging)
    request.state.request_id = request_id
    # ...and to the LOGGER, so every record emitted while serving this request
    # carries req=<id> without each call site having to pass it along.
    set_request_id(request_id)

    try:
        response = await call_next(request)
    except Exception as e:
        duration = time.time() - start_time
        logger.error(
            "[REQUEST] request_id=%s %s %s -> 500 in %.3fs: %s",
            request_id, request.method, request.url.path, duration, e, exc_info=True,
        )
        return JSONResponse(
            status_code=500,
            content={"error": "Internal server error", "request_id": request_id},
        )

    duration = time.time() - start_time
    response.headers["X-Request-ID"] = request_id

    try:
        from backend.core.metrics import metrics_request_duration
        if metrics_request_duration is not None and request.url.path not in _QUIET_PATHS:
            metrics_request_duration.labels(
                method=request.method, status=str(response.status_code)
            ).observe(duration)
    except Exception:
        pass

    if duration > _SLOW_REQUEST_SECONDS:
        logger.warning(
            "[REQUEST] 🐢 SLOW request_id=%s %s %s -> %s in %.3fs",
            request_id, request.method, request.url.path, response.status_code, duration,
        )
    elif request.url.path not in _QUIET_PATHS:
        logger.debug(
            "[REQUEST] request_id=%s %s %s -> %s in %.3fs",
            request_id, request.method, request.url.path, response.status_code, duration,
        )

    # HTML pages: revalidate on every use. FileResponse sends ETag and
    # Last-Modified but NO Cache-Control, and without one browsers apply
    # HEURISTIC caching (roughly 10% of the file's age) — on plain
    # navigations they reuse the cached page WITHOUT revalidating. In
    # practice: a user clicking through the navbar kept getting week-old
    # copies of pages whose links had moved ("Add Person" still pointing at
    # the pre-split /dashboard). `no-cache` means cache-but-revalidate; the
    # ETag turns that into a cheap 304 on every hit, so this costs nothing.
    # Versioned static assets (?v=...) are unaffected — nginx serves those
    # with immutable caching, which is correct for them.
    content_type = response.headers.get("content-type", "")
    if content_type.startswith("text/html") and "cache-control" not in response.headers:
        response.headers["Cache-Control"] = "no-cache"

    return response

# CORS. Credentials are dropped automatically when a wildcard origin is
# configured — the two together let any site read authenticated responses.
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins_list,
    allow_methods=["*"],
    allow_headers=["*"],
    allow_credentials=settings.cors_allow_credentials,
)

# Static files for frontend (CSS, JS)
try:
    app.mount("/frontend", StaticFiles(directory="frontend"), name="frontend")
    logger.info("✅ Static files mounted at /frontend")
except Exception as e:
    logger.warning(f"⚠️ Failed to mount static files: {e}")


# =====================================================
# API documentation — offline and CSP-compliant
# =====================================================
# FastAPI's stock /docs and /redoc were unusable here for two independent
# reasons, either of which alone produces a blank page:
#   1. they load Swagger UI / ReDoc from cdn.jsdelivr.net (and Google Fonts),
#      which does not exist in an offline deployment;
#   2. the page's bootstrap is an INLINE <script>, and this deployment sends
#      `script-src 'self'` with no 'unsafe-inline'.
# These routes serve the same two UIs from vendored same-origin assets under
# /frontend/vendor/swagger/, with the bootstrap in /frontend/js/docs-init.js.
# Disabled together with the rest of the docs surface in production.
if _docs_enabled:
    from fastapi.responses import HTMLResponse as _DocsHTMLResponse

    _DOCS_ASSETS = "/frontend/vendor/swagger"

    @app.get("/docs", include_in_schema=False)
    async def swagger_ui_offline():
        """Swagger UI, served entirely from local assets."""
        return _DocsHTMLResponse(f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{settings.APP_NAME} - API Docs</title>
  <link rel="stylesheet" href="{_DOCS_ASSETS}/swagger-ui.css">
  <link rel="icon" href="data:,">
</head>
<body>
  <div id="swagger-ui"></div>
  <script src="{_DOCS_ASSETS}/swagger-ui-bundle.js"></script>
  <script src="{_DOCS_ASSETS}/swagger-ui-standalone-preset.js"></script>
  <script src="/frontend/js/docs-init.js"
          data-mode="swagger" data-openapi-url="{_openapi_url}"></script>
</body>
</html>""")

    @app.get("/redoc", include_in_schema=False)
    async def redoc_offline():
        """ReDoc, served entirely from local assets (no Google Fonts)."""
        return _DocsHTMLResponse(f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{settings.APP_NAME} - API Reference</title>
  <link rel="icon" href="data:,">
  <style>body {{ margin: 0; padding: 0; }}</style>
</head>
<body>
  <redoc id="redoc-container" spec-url="{_openapi_url}"></redoc>
  <script src="{_DOCS_ASSETS}/redoc.standalone.js"></script>
  <script src="/frontend/js/docs-init.js"
          data-mode="redoc" data-openapi-url="{_openapi_url}"></script>
</body>
</html>""")

    @app.get("/docs/oauth2-redirect", include_in_schema=False)
    async def swagger_oauth2_redirect():
        from fastapi.openapi.docs import get_swagger_ui_oauth2_redirect_html
        return get_swagger_ui_oauth2_redirect_html()

# Storage (face images) - served through an AUTHENTICATED route, not a public
# static mount. Surveillance face crops must never be world-readable; auth comes
# from the HttpOnly cookie (browser <img> tags) or a Bearer token (API clients).
_STORAGE_DIR = os.path.realpath(settings.STORAGE_DIR)

@app.get("/storage/{file_path:path}", include_in_schema=False)
async def serve_storage_image(file_path: str, current_user=Depends(_get_current_user_dep)):
    full_path = os.path.realpath(os.path.join(_STORAGE_DIR, file_path))
    # Prevent path traversal outside the storage directory
    if not full_path.startswith(_STORAGE_DIR + os.sep) and full_path != _STORAGE_DIR:
        raise HTTPException(status_code=404, detail="Not found")
    if not os.path.isfile(full_path):
        raise HTTPException(status_code=404, detail="Not found")
    return FileResponse(full_path)

logger.info(f"✅ Storage images served with authentication at /storage from {_STORAGE_DIR}")


# Import and register all routes from backend/routes/
from backend.routes import (
    webhook_router, detections_router, stats_router, upload_router,
    enrollment_review_router,
    health_router, metrics_router, websocket_router, dashboard_router,
    cache_router, management_router, auth_router, users_router, audit_router
)

# Import identities router
try:
    from backend.routes.identities import router as identities_router
    logger.info("✅ Identities router imported successfully")
except ImportError as e:
    identities_router = None
    logger.warning(f"⚠️ Identities router not available: {e}")

# Import admin tutorial router
try:
    from backend.routes.admin_tutorial import router as admin_tutorial_router
    logger.info("✅ Admin tutorial router imported successfully")
except ImportError as e:
    admin_tutorial_router = None
    logger.warning(f"⚠️ Admin tutorial router not available: {e}")

# Import settings router
try:
    from backend.routes.settings import router as settings_router
    logger.info("✅ Settings router imported successfully")
except ImportError as e:
    settings_router = None
    logger.warning(f"⚠️ Settings router not available: {e}")

# Import retention router (status + dry-run/manual execution)
try:
    from backend.routes.retention import router as retention_router
    logger.info("✅ Retention router imported successfully")
except ImportError as e:
    retention_router = None
    logger.warning(f"⚠️ Retention router not available: {e}")

# Import ingest-credential router (issue/list/revoke webhook credentials)
try:
    from backend.routes.webhook_credentials import router as webhook_credentials_router
    logger.info("✅ Ingest credential router imported successfully")
except ImportError as e:
    webhook_credentials_router = None
    logger.warning(f"⚠️ Ingest credential router not available: {e}")

# Import logs router
try:
    from backend.routes.logs import router as logs_router
    logger.info("✅ Logs router imported successfully")
except ImportError as e:
    logs_router = None
    logger.warning(f"⚠️ Logs router not available: {e}")

# Import advanced search router
try:
    from backend.routes.advanced_search import router as advanced_search_router
    if advanced_search_router:
        logger.info("✅ Advanced search router imported successfully")
    else:
        logger.error("❌ Advanced search router is None after import")
        advanced_search_router = None
except ImportError as e:
    advanced_search_router = None
    logger.error(f"❌ Advanced search router import FAILED (ImportError): {e}")
    logger.error(f"   Traceback: {traceback.format_exc()}")
except Exception as e:
    advanced_search_router = None
    logger.error(f"❌ Advanced search router import FAILED: {e}")
    logger.error(f"   Traceback: {traceback.format_exc()}")

# Import watchlist router
try:
    from backend.routes.watchlists import router as watchlists_router
    logger.info("✅ Watchlists router imported successfully")
except Exception as e:
    watchlists_router = None
    logger.error(f"❌ Watchlists router import FAILED: {e}")
    logger.error(f"   Traceback: {traceback.format_exc()}")

# Import live alerts router
try:
    from backend.routes.live_alerts import router as live_alerts_router
    logger.info("✅ Live alerts router imported successfully")
except Exception as e:
    live_alerts_router = None
    logger.error(f"❌ Live alerts router import FAILED: {e}")
    logger.error(f"   Traceback: {traceback.format_exc()}")

# Import intelligence router
try:
    from backend.routes.intelligence import router as intelligence_router
    logger.info("✅ Intelligence router imported successfully")
except Exception as e:
    intelligence_router = None
    logger.error(f"❌ Intelligence router import FAILED: {e}")
    logger.error(f"   Traceback: {traceback.format_exc()}")

# Import risk-assessment router (persisted unified risk engine)
try:
    from backend.routes.risk_assessments import router as risk_assessments_router
    logger.info("✅ Risk assessments router imported successfully")
except Exception as e:
    risk_assessments_router = None
    logger.error(f"❌ Risk assessments router import FAILED: {e}")
    logger.error(f"   Traceback: {traceback.format_exc()}")

# Import ML operations router (first-release ML pipeline; rules stay default)
try:
    from backend.routes.ml_ops import router as ml_ops_router
    logger.info("✅ ML operations router imported successfully")
except Exception as e:
    ml_ops_router = None
    logger.error(f"❌ ML operations router import FAILED: {e}")
    logger.error(f"   Traceback: {traceback.format_exc()}")

# Import batch search & export router
try:
    from backend.routes.batch_export import router as batch_export_router
    logger.info("✅ Batch export router imported successfully")
except Exception as e:
    batch_export_router = None
    logger.error(f"❌ Batch export router import FAILED: {e}")
    logger.error(f"   Traceback: {traceback.format_exc()}")

# Import task history router
try:
    from backend.routes.task_history import router as task_history_router
    logger.info("✅ Task history router imported successfully")
except Exception as e:
    task_history_router = None
    logger.error(f"❌ Task history router import FAILED: {e}")
    logger.error(f"   Traceback: {traceback.format_exc()}")

# Register all routers
# IMPORTANT: dashboard_router must be registered BEFORE identities_router
# because both have /admin/unknown route, and we want the HTML page route
# (in dashboard_router) to take precedence over the API route (in identities_router)
app.include_router(auth_router)  # Auth routes (login, logout, me)
app.include_router(users_router)  # User management (admin only)
app.include_router(audit_router)  # Audit logs (admin only)

# Conversation domain (/api/v1) — guarded import like the optional routers:
# a failure here must not take down face recognition, but it is logged loudly
# because chat history would be running on the legacy path only.
try:
    from backend.routes.conversations import router as conversations_router
    app.include_router(conversations_router)
    logger.info("✅ Conversations router registered (/api/v1)")
except Exception as _conv_error:  # pragma: no cover - import-time wiring
    logger.error(f"❌ Conversations router failed to load: {_conv_error}", exc_info=True)
if settings_router:
    app.include_router(settings_router)  # Settings management (admin only)
    logger.info("✅ Settings router registered")
if retention_router:
    app.include_router(retention_router)  # Retention status + dry-run (admin only)
if webhook_credentials_router:
    app.include_router(webhook_credentials_router)  # Issue/revoke ingest credentials (admin only)
    logger.info("✅ Webhook credentials router registered")
if logs_router:
    app.include_router(logs_router)  # Error logs viewer (admin only)
    logger.info("✅ Logs router registered")
if admin_tutorial_router:
    app.include_router(admin_tutorial_router)  # Admin tutorial and learning (admin only)
    logger.info("✅ Admin tutorial router registered")
else:
    logger.warning("⚠️ Admin tutorial router not registered - tutorial endpoints unavailable")
app.include_router(dashboard_router)  # HTML pages (must be before identities_router for /admin/unknown)
if identities_router:
    app.include_router(identities_router)  # Identity management (admin only) - API routes
    logger.info("✅ Identities router registered with all endpoints")
    # Log available routes for debugging
    for route in identities_router.routes:
        if hasattr(route, 'methods') and hasattr(route, 'path'):
            methods = ', '.join(route.methods) if route.methods else 'N/A'
            logger.info(f"   📍 {methods} {route.path}")
else:
    logger.warning("⚠️ Identities router not registered - identity management endpoints unavailable")
app.include_router(webhook_router)
app.include_router(detections_router)
app.include_router(stats_router)
app.include_router(upload_router)
# Phase two of a name-based upload: the administrator's identity decision.
app.include_router(enrollment_review_router)
app.include_router(health_router)
app.include_router(metrics_router)
app.include_router(websocket_router)  # WebSocket route is included via router
app.include_router(cache_router)
app.include_router(management_router)

# Advanced Search and Watchlist routers
if advanced_search_router:
    app.include_router(advanced_search_router)
    logger.info("✅ Advanced search router registered")
    # Log available routes
    for route in advanced_search_router.routes:
        if hasattr(route, 'methods') and hasattr(route, 'path'):
            methods = ', '.join(route.methods) if route.methods else 'N/A'
            logger.info(f"   📍 {methods} {route.path}")
else:
    logger.error("❌ Advanced search router NOT registered - /api/search/advanced will return 404")
if watchlists_router:
    app.include_router(watchlists_router)
    logger.info("✅ Watchlists router registered")
if live_alerts_router:
    app.include_router(live_alerts_router)
    logger.info("✅ Live alerts router registered")
if intelligence_router:
    app.include_router(intelligence_router)
    logger.info("✅ Intelligence router registered")
if risk_assessments_router:
    app.include_router(risk_assessments_router)
    logger.info("✅ Risk assessments router registered")
if ml_ops_router:
    app.include_router(ml_ops_router)
    logger.info("✅ ML operations router registered")
    # Log available routes for debugging.
    # This iterated intelligence_router.routes — a copy-paste from the block
    # above. If ml_ops imported but intelligence did NOT, intelligence_router
    # is None and this raised AttributeError at import time, taking the whole
    # application down during startup. The `else` branch named the wrong
    # router too, so the log accused intelligence when ML Ops was missing.
    for route in ml_ops_router.routes:
        if hasattr(route, 'methods') and hasattr(route, 'path'):
            methods = ', '.join(route.methods) if route.methods else 'N/A'
            logger.info(f"   📍 {methods} {route.path}")
else:
    logger.error("❌ ML operations router NOT registered - /api/ml/* endpoints will return 404")
if batch_export_router:
    app.include_router(batch_export_router)
    logger.info("✅ Batch export router registered")

if task_history_router:
    app.include_router(task_history_router)
    logger.info("✅ Task history router registered")
else:
    logger.warning("⚠️ Task history router not registered - task history endpoints unavailable")

# The webhook used to be registered a SECOND time here, directly on the app, for
# compatibility with an older entry point. It was already unreachable: the router
# is included above, Starlette matches routes in registration order and takes the
# first full match, so POST /webhook/{pipeline_id} always resolved to the router's
# route. Dead — but a trap, because it carried no authentication dependency and
# would have become a live bypass the moment anyone moved the include_router call
# below it. Deleted rather than duplicating the guard onto it; a route-inventory
# test now fails if any webhook path+method pair is ever registered twice.

logger.info("✅ All routes registered from backend/routes/")
try:
    from backend.security import webhook_auth as _webhook_auth
    logger.info("✅ Webhook ingest auth: mode=%s keys=%d",
                _webhook_auth.auth_mode(settings),
                len(_webhook_auth.parse_keys(settings.WEBHOOK_API_KEYS)))
except Exception as _wh_exc:  # pragma: no cover - never block startup on a log line
    logger.warning("Could not report webhook auth mode: %s", _wh_exc)

# Exception handler for 401/403 - redirect HTML requests to signin/dashboard, return JSON for API requests
@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    """
    Handle HTTP exceptions (401, 403, etc.)
    - For HTML page requests: Redirect to signin (401) or dashboard (403)
    - For API requests: Return JSON error response
    """
    # Machine-to-machine endpoints never get a redirect. `/webhook/...` does not
    # start with `/api/`, so the catch-all below classified it as a browser page
    # and answered an unauthenticated camera with `302 -> /signin`. A client that
    # follows redirects then receives 200 and an HTML login page, and reads that
    # as a successful ingest — a rejection that looks like success.
    path = request.url.path
    is_machine_endpoint = path.startswith("/webhook/") or path == "/webhook"

    # Check if this is an HTML page request
    accept_header = request.headers.get("accept", "").lower()
    is_html_request = (not is_machine_endpoint) and (
        "text/html" in accept_header or
        request.url.path.startswith("/admin/") or
        request.url.path.startswith("/dashboard") or
        request.url.path.startswith("/home") or
        request.url.path.startswith("/tracking-people") or
        request.url.path.endswith(".html") or
        (not request.url.path.startswith("/api/") and not request.url.path.startswith("/docs") and not request.url.path.startswith("/openapi.json"))
    )

    # For 401 (Unauthorized) errors on HTML pages, redirect to signin
    if exc.status_code == status.HTTP_401_UNAUTHORIZED and is_html_request:
        logger.warning(f"[AUTH] HTML page request to {request.url.path} returned 401, redirecting to /signin")
        return RedirectResponse(url="/signin", status_code=302)
    
    # For 403 (Forbidden) errors on HTML pages, redirect to dashboard —
    # EXCEPT a pending password rotation, which must go to /change-password.
    # /dashboard is itself gated, so sending a pending user there would 403
    # again and bounce them between the two forever.
    if exc.status_code == status.HTTP_403_FORBIDDEN and is_html_request:
        detail = exc.detail
        if isinstance(detail, dict) and detail.get("code") == "PASSWORD_ROTATION_REQUIRED":
            logger.info("[AUTH] HTML page request to %s blocked pending password rotation, "
                        "redirecting to /change-password", request.url.path)
            return RedirectResponse(url="/change-password", status_code=302)
        logger.warning(f"[AUTH] HTML page request to {request.url.path} returned 403, redirecting to /dashboard")
        return RedirectResponse(url="/dashboard", status_code=302)
    
    # For API requests or other errors, return JSON
    #
    # `headers=exc.headers` is not optional decoration. This handler REPLACES
    # Starlette's, so every header a raise site attached was silently dropped:
    # `WWW-Authenticate` on 401s (the webhook challenge and the auth service's
    # Bearer challenges alike) and `Retry-After` on 503/429. The raise sites all
    # set them and none of them ever arrived, which is invisible from the status
    # code alone — a client is simply never told how to authenticate or when to
    # retry.
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": exc.detail, "status_code": exc.status_code},
        headers=getattr(exc, "headers", None),
    )

# if __name__ == "__main__":
#     import uvicorn
#     uvicorn.run(app, host="0.0.0.0", port=8000)

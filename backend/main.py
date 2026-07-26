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
from utils.logging import setup_logging
setup_logging(log_to_file=True)  # Enable file logging to logs/app.log
logger = logging.getLogger(__name__)

# Interactive docs publish the entire API surface, including every admin route,
# to anyone who can reach the port. Production serves none of them; the guard
# refuses to start if ENABLE_API_DOCS is left on there.
_docs_enabled = bool(getattr(settings, "ENABLE_API_DOCS", True)) and not settings.is_production
_docs_url = "/docs" if _docs_enabled else None
_redoc_url = "/redoc" if _docs_enabled else None
_openapi_url = "/openapi.json" if _docs_enabled else None

# Create FastAPI app
app = FastAPI(
    title=getattr(settings, 'APP_NAME', 'Face Recognition Service'),
    version=getattr(settings, 'VERSION', 'V1.0.0'),
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
    
    **Public Endpoints (no authentication):**
    - Health check (`/health`)
    - Metrics (`/metrics`)
    - API Documentation (`/docs`)
    - Login (`/api/auth/login`)
    """,
    terms_of_service="https://example.com/terms/",
    contact={
        "name": "ITDIR-AI DEPARTMENT",
        "email": "support@example.com",
    },
    license_info={
        "name": "Proprietary",
    },
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

# Storage (face images) - served through an AUTHENTICATED route, not a public
# static mount. Surveillance face crops must never be world-readable; auth comes
# from the HttpOnly cookie (browser <img> tags) or a Bearer token (API clients).
_STORAGE_DIR = os.path.realpath(getattr(settings, 'STORAGE_DIR', './storage'))

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

# Static files for offline map tiles (if enabled)
try:
    tiles_path = getattr(settings, 'MAP_OFFLINE_TILES_PATH', None)
    tiles_enabled = getattr(settings, 'MAP_OFFLINE_TILES_ENABLED', False)
    if tiles_enabled and tiles_path and os.path.exists(tiles_path) and os.path.isdir(tiles_path):
        app.mount("/tiles", StaticFiles(directory=tiles_path), name="tiles")
        logger.info(f"✅ Offline map tiles mounted at /tiles from {tiles_path}")
        logger.info(f"   Tiles will be served from: /tiles/{{z}}/{{x}}/{{y}}.png")
    elif tiles_enabled and tiles_path:
        logger.warning(f"⚠️ Offline tiles enabled but path not found: {tiles_path}")
        logger.info("   Download tiles to this directory with structure: {z}/{x}/{y}.png")
except Exception as e:
    logger.warning(f"⚠️ Failed to mount tiles directory: {e}")

# Import and register all routes from backend/routes/
from backend.routes import (
    webhook_router, detections_router, stats_router, upload_router,
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
if settings_router:
    app.include_router(settings_router)  # Settings management (admin only)
    logger.info("✅ Settings router registered")
if retention_router:
    app.include_router(retention_router)  # Retention status + dry-run (admin only)
    logger.info("✅ Retention router registered")
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
    # Log available routes for debugging
    for route in intelligence_router.routes:
        if hasattr(route, 'methods') and hasattr(route, 'path'):
            methods = ', '.join(route.methods) if route.methods else 'N/A'
            logger.info(f"   📍 {methods} {route.path}")
else:
    logger.error("❌ Intelligence router NOT registered - intelligence endpoints will return 404")
if batch_export_router:
    app.include_router(batch_export_router)
    logger.info("✅ Batch export router registered")

if task_history_router:
    app.include_router(task_history_router)
    logger.info("✅ Task history router registered")
else:
    logger.warning("⚠️ Task history router not registered - task history endpoints unavailable")

# IMPORTANT: Also register webhook endpoint directly on app (like app_production.py)
# This ensures compatibility with pipelines that were working with the old implementation
from backend.routes.webhook import webhook_handler, parse_webhook_request
from fastapi import BackgroundTasks, Request
import json

@app.post("/webhook/{pipeline_id}")
async def webhook_direct(pipeline_id: str, request: Request, background_tasks: BackgroundTasks):
    """Webhook endpoint - Direct registration on app (for compatibility with app_production.py)"""
    payload = await parse_webhook_request(request)
    return await webhook_handler(pipeline_id, payload, background_tasks)

logger.info("✅ All routes registered from backend/routes/")
logger.info("✅ Webhook endpoint registered both via router and directly on app")

# Exception handler for 401/403 - redirect HTML requests to signin/dashboard, return JSON for API requests
@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    """
    Handle HTTP exceptions (401, 403, etc.)
    - For HTML page requests: Redirect to signin (401) or dashboard (403)
    - For API requests: Return JSON error response
    """
    # Check if this is an HTML page request
    accept_header = request.headers.get("accept", "").lower()
    is_html_request = (
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
    
    # For 403 (Forbidden) errors on HTML pages, redirect to dashboard
    if exc.status_code == status.HTTP_403_FORBIDDEN and is_html_request:
        logger.warning(f"[AUTH] HTML page request to {request.url.path} returned 403, redirecting to /dashboard")
        return RedirectResponse(url="/dashboard", status_code=302)
    
    # For API requests or other errors, return JSON
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": exc.detail, "status_code": exc.status_code}
    )

# if __name__ == "__main__":
#     import uvicorn
#     uvicorn.run(app, host="0.0.0.0", port=8000)

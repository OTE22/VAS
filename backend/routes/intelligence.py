"""
Intelligence API Routes
========================
Provides endpoints for identity intelligence features:
- Related Identities (co-appearance analysis)
- Temporal Patterns (when do they appear)
- Cross-Camera Tracking (movement across locations)
"""

import asyncio
import logging
import threading
import time
import uuid as uuid_mod
from datetime import datetime
from typing import List, Optional, Dict

from fastapi import APIRouter, HTTPException, Depends, Query, Request, BackgroundTasks, status as http_status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from config import settings
from db_connection import get_db
from db_models import Pipeline, Identity
from sqlalchemy import select
from backend.auth.auth_service import get_current_user, require_admin
from backend.core.intelligence_service import intelligence_service
from backend.core.security_intelligence_service import (
    security_intelligence_service,
    THREAT_ALGORITHM_VERSION,
    NETWORK_RISK_VERSION,
    PATTERN_ALGORITHM_VERSION,
    ANOMALY_ALGORITHM_VERSION,
)
from backend.core.relationship_calculation_task import calculate_all_relationships
from backend.core.threshold_learner import threshold_learner
from backend.core.trajectory_predictor import trajectory_predictor
from backend.core.activity_correlation import activity_correlation_analyzer
from backend.core.distributed_lock import DistributedLock, peek_holder
from backend.core.rate_limiter import rate_limited
from backend.utils.path_utils import path_to_url
from fastapi.responses import HTMLResponse, JSONResponse
from backend.utils.time_utils import iso_utc, utc_now

logger = logging.getLogger(__name__)
router = APIRouter(tags=["Intelligence"])


# =====================================================
# Security / hygiene helpers
# =====================================================

MAP_STYLE_ALLOWLIST = frozenset({"dark", "light", "satellite", "terrain"})

# Social-network graph is ALWAYS bounded — the full graph is never sent.
NETWORK_MAX_NODES = 300          # absolute server ceiling
NETWORK_DEFAULT_MAX_NODES = 100  # default page of highest-signal nodes
NETWORK_MAX_EDGES = 1000

# Relationship strength policy — single source of truth, returned in API
# metadata so the UI never hard-codes divergent thresholds.
RELATIONSHIP_THRESHOLDS = {
    "strong": {"min_percentage": 50, "min_co_appearances": 20},
    "moderate": {"min_percentage": 25, "min_co_appearances": 10},
    "weak": {"min_percentage": 0, "min_co_appearances": 0},
}


def _reference_id() -> str:
    return f"INTEL-{uuid_mod.uuid4().hex[:8]}"


def _iso_z(dt) -> Optional[str]:
    """Timezone-aware ISO 8601. Naive datetimes in this codebase are UTC."""
    if dt is None:
        return None
    return iso_utc(dt)


def _safe_500(action: str, exc: Exception) -> HTTPException:
    """Log the real exception server-side, return only a reference id.

    Never sends str(exc) (may contain SQL fragments, paths, internals)
    to the browser.
    """
    ref = _reference_id()
    logger.error("[INTELLIGENCE] action=%s status=error reference_id=%s error=%s",
                 action, ref, exc, exc_info=True)
    return HTTPException(
        status_code=500,
        detail=f"Internal error during {action}. Reference: {ref}",
    )


async def _get_identity_or_404(db: AsyncSession, identity_id: str) -> Identity:
    """Object-level guard: the identity must exist and be a valid id.

    Returns 404 for both malformed and unknown ids so callers cannot
    distinguish 'bad id' from 'exists but hidden'.
    """
    try:
        identity_uuid = uuid_mod.UUID(str(identity_id))
    except (ValueError, AttributeError, TypeError):
        raise HTTPException(status_code=404, detail="Identity not found")
    result = await db.execute(select(Identity).where(Identity.id == identity_uuid))
    identity = result.scalar_one_or_none()
    if identity is None:
        raise HTTPException(status_code=404, detail="Identity not found")
    return identity


def require_intel_csrf(request: Request):
    """CSRF defense for cookie-authenticated mutating requests.

    Cross-site pages cannot attach custom headers without a CORS
    preflight this API never grants. Bearer-token clients are exempt.
    """
    if request.headers.get("authorization"):
        return
    xrw = request.headers.get("x-requested-with", "")
    if xrw.lower() != "xmlhttprequest":
        raise HTTPException(
            status_code=http_status.HTTP_403_FORBIDDEN,
            detail="CSRF check failed: X-Requested-With header required",
        )


def _audit(action: str, current_user: dict, identity_id: Optional[str] = None,
           result: str = "success", duration_ms: Optional[int] = None, **fields):
    """One structured audit line per sensitive intelligence access.

    Safe fields only — never embeddings, snapshots, tokens or payloads.
    """
    extra = " ".join(f"{k}={v}" for k, v in fields.items() if v is not None)
    logger.info(
        "[INTEL_AUDIT] action=%s user_id=%s identity_id=%s result=%s duration_ms=%s %s",
        action,
        (current_user or {}).get("id") or (current_user or {}).get("user_id"),
        identity_id, result, duration_ms, extra,
    )


# ---- heavy-endpoint envelope: timeout + Prometheus observation ----
# Before this, an expensive analysis request rode until the DB statement
# timeout (DB_STATEMENT_TIMEOUT_MS) with zero observability. Read per call from
# INTEL_QUERY_TIMEOUT_SECONDS rather than held as a module constant, so raising
# it for a slow deployment does not need a rebuild.


def _intel_timeout_seconds() -> float:
    return float(settings.INTEL_QUERY_TIMEOUT_SECONDS)


def _observe_intel(feature: str, result: str, started: float) -> None:
    """fr_intel_requests_total{feature,result} + fr_intel_duration_seconds."""
    try:
        from backend.core import metrics as app_metrics
        if app_metrics.metrics_intel_requests is not None:
            app_metrics.metrics_intel_requests.labels(feature=feature, result=result).inc()
        if app_metrics.metrics_intel_duration is not None:
            app_metrics.metrics_intel_duration.labels(feature=feature).observe(
                time.monotonic() - started)
    except Exception:  # metrics must never break the request
        logger.debug("[INTELLIGENCE] metrics observation failed", exc_info=True)


async def _bounded_intel_call(feature: str, coro):
    """Run a heavy service coroutine under a timeout, observing metrics.

    Timeout surfaces as 503 with an opaque reference id — the analysis is
    too expensive right now, which is a capacity signal, not a crash.
    """
    started = time.monotonic()
    timeout_s = _intel_timeout_seconds()
    try:
        result = await asyncio.wait_for(coro, timeout=timeout_s)
    except asyncio.TimeoutError:
        ref = _reference_id()
        logger.error("[INTELLIGENCE] action=%s status=timeout reference_id=%s timeout_s=%s",
                     feature, ref, timeout_s)
        _observe_intel(feature, "timeout", started)
        raise HTTPException(
            status_code=503,
            detail=f"Analysis timed out after {timeout_s:g}s. Reference: {ref}",
        )
    except Exception:
        _observe_intel(feature, "error", started)
        raise
    _observe_intel(feature, "success", started)
    return result


# ---- calculate-all single-flight guard ----
# LAYERED: the in-process dict below is the first, always-available layer;
# the endpoints additionally hold a Redis DistributedLock (see
# backend/core/distributed_lock.py) so multiple API workers cannot run the
# same job concurrently. Without Redis the in-process layer alone still
# guarantees single-flight within one worker (the single-worker deployment),
# and startup logs a warning when WORKERS>1 without Redis. ----
_relationship_job_lock = threading.Lock()
_RELATIONSHIP_JOB = {"job_id": None, "started_at": None}
_RELATIONSHIP_JOB_MAX_AGE_SECONDS = 3 * 3600  # stale-guard: never wedge forever


def _try_acquire_relationship_job(job_id: str) -> Optional[str]:
    """Returns None on success, or the currently-running job_id on conflict."""
    with _relationship_job_lock:
        current = _RELATIONSHIP_JOB["job_id"]
        started = _RELATIONSHIP_JOB["started_at"]
        if current and started and (time.time() - started) < _RELATIONSHIP_JOB_MAX_AGE_SECONDS:
            return current
        _RELATIONSHIP_JOB["job_id"] = job_id
        _RELATIONSHIP_JOB["started_at"] = time.time()
        return None


def _release_relationship_job(job_id: str) -> None:
    with _relationship_job_lock:
        if _RELATIONSHIP_JOB["job_id"] == job_id:
            _RELATIONSHIP_JOB["job_id"] = None
            _RELATIONSHIP_JOB["started_at"] = None


# ---- threshold-learning single-flight guard (same pattern) ----
_threshold_job_lock = threading.Lock()
_THRESHOLD_JOB = {"job_id": None, "started_at": None}
_THRESHOLD_JOB_MAX_AGE_SECONDS = 3600  # threshold learning is minutes, not hours

THRESHOLD_ALGORITHM_VERSION = "threshold-v2"
TRAJECTORY_MODEL_VERSION = "trajectory-v2"
CORRELATION_ALGORITHM_VERSION = "xcca-v2"
CORRELATION_MIN_SEQUENCES = 3

CORRELATION_NOTE = (
    "Measures temporal and spatial association between two identities. "
    "Correlation does not prove causation."
)


def _try_acquire_threshold_job(job_id: str) -> Optional[str]:
    with _threshold_job_lock:
        current = _THRESHOLD_JOB["job_id"]
        started = _THRESHOLD_JOB["started_at"]
        if current and started and (time.time() - started) < _THRESHOLD_JOB_MAX_AGE_SECONDS:
            return current
        _THRESHOLD_JOB["job_id"] = job_id
        _THRESHOLD_JOB["started_at"] = time.time()
        return None


def _release_threshold_job(job_id: str) -> None:
    with _threshold_job_lock:
        if _THRESHOLD_JOB["job_id"] == job_id:
            _THRESHOLD_JOB["job_id"] = None
            _THRESHOLD_JOB["started_at"] = None


def _threshold_job_running() -> Optional[str]:
    with _threshold_job_lock:
        current = _THRESHOLD_JOB["job_id"]
        started = _THRESHOLD_JOB["started_at"]
        if current and started and (time.time() - started) < _THRESHOLD_JOB_MAX_AGE_SECONDS:
            return current
        return None


def _safe_error_page(title: str, message: str, reference_id: Optional[str] = None,
                     status_code: int = 500) -> HTMLResponse:
    """Static, hand-authored error HTML — no backend values interpolated
    except the opaque reference id (hex, generated server-side)."""
    ref_html = f"<p>Reference: {reference_id}</p>" if reference_id else ""
    resp = HTMLResponse(
        content=(
            "<!DOCTYPE html><html><head><title>Map Service</title></head>"
            "<body style=\"font-family: Arial; text-align: center; padding: 50px;\">"
            f"<h2>{title}</h2><p>{message}</p>{ref_html}"
            "</body></html>"
        ),
        status_code=status_code,
    )
    resp.headers["Cache-Control"] = "private, no-store"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Content-Security-Policy"] = "sandbox"
    return resp


# =====================================================
# Response Models
# =====================================================

class RelatedIdentityResponse(BaseModel):
    identity_id: str
    display_name: Optional[str]
    identity_type: str
    co_appearance_count: int
    co_appearance_percentage: float
    relationship_strength: str
    common_pipelines: List[str]
    first_co_appearance: Optional[str]
    last_co_appearance: Optional[str]
    best_snapshot_path: Optional[str]


class TemporalPatternResponse(BaseModel):
    identity_id: str
    hourly_distribution: dict
    daily_distribution: dict
    peak_hours: List[int]
    peak_days: List[str]
    most_common_pipelines: List[dict]
    total_appearances: int
    first_appearance: Optional[str]
    last_appearance: Optional[str]
    average_appearances_per_day: float


class CameraMovementResponse(BaseModel):
    pipeline_id: str
    pipeline_name: Optional[str]
    timestamp: str
    snapshot_path: Optional[str]
    duration_at_location: Optional[int]
    coordinates: Optional[Dict[str, float]] = None  # {"lat": float, "lng": float}


class CrossCameraTrackResponse(BaseModel):
    identity_id: str
    display_name: Optional[str]
    date: str
    movements: List[CameraMovementResponse]
    total_cameras: int
    first_seen: str
    last_seen: str
    total_duration_minutes: int


class TimelineEntryResponse(BaseModel):
    timestamp: str
    pipeline_id: str
    pipeline_name: str
    snapshot_path: Optional[str]
    duration_seconds: Optional[int]


class TimelineSummaryResponse(BaseModel):
    total_appearances: int
    unique_locations: int
    first_seen: Optional[str]
    last_seen: Optional[str]


class MovementTimelineResponse(BaseModel):
    identity_id: str
    hours_back: int
    timeline: List[TimelineEntryResponse]
    summary: TimelineSummaryResponse


class ThresholdData(BaseModel):
    camera_1: str
    camera_2: str
    optimal_time_window_minutes: float
    optimal_distance_meters: float
    actual_distance_meters: float
    confidence: float
    sample_count: int
    # threshold-v2: confidence is derived from sample sufficiency AND travel-
    # time dispersion; the ingredients are reported so the number is auditable.
    p95_minutes: Optional[float] = None
    spread_minutes: Optional[float] = None


class ThresholdLearningResponse(BaseModel):
    status: str
    learned_pairs: int
    thresholds: List[ThresholdData]


class TrajectoryPredictionItem(BaseModel):
    camera_id: str
    probability: float
    estimated_time: str
    confidence: str  # high | moderate | low


class TrajectoryPredictionResponse(BaseModel):
    identity_id: str
    current_camera: str
    predictions: List[TrajectoryPredictionItem]
    model_version: str
    insufficient_evidence: bool
    note: str


class ActivitySequenceResponse(BaseModel):
    from_camera: str
    to_camera: str
    time_diff_minutes: float
    from_time: str
    to_time: str


class ActivityCorrelationResponse(BaseModel):
    identity_a: str
    identity_b: str
    correlation_score: float
    correlation_strength: str
    sequence_count: int
    sequences: List[ActivitySequenceResponse]
    days_back: int
    insufficient_evidence: bool
    algorithm_version: str
    note: str
    # xcca-v2: per-side appearance caps — when hit, the score covers a
    # truncated window and the UI must not present it as exhaustive.
    truncated: bool = False


# =====================================================
# API Endpoints
# =====================================================


def _feature_disabled(setting_key: str, label: str) -> "HTTPException":
    """The 403 raised when a feature's declared flag is off.

    These flags were declared in config.py with descriptions asserting they
    enable/disable the feature, rendered as editable switches on the settings
    page, and read by nothing — so turning one off changed nothing at all.

    Callers read the attribute directly rather than passing a key to a
    getattr() in here: a dynamic lookup is invisible to the source scan in
    tests/test_runtime_editability.py, which is what proves each registered
    setting has a real consumer. A `getattr(settings, key, True)` would also
    have been a second declaration of the default.
    """
    return HTTPException(status_code=403, detail=f"{label} is disabled ({setting_key})")


@router.get(
    "/api/identities/{identity_id}/related",
    tags=["Intelligence"],
    summary="Get Related Identities",
    description="""
    Find people who frequently appear together with this identity.
    
    **Use Cases:**
    - Identify associates/companions
    - Detect group behavior patterns
    - Security: find people traveling together
    
    **Relationship Strength:**
    - Strong: 50%+ co-appearance or 20+ times together
    - Moderate: 25%+ co-appearance or 10+ times
    - Weak: Below moderate threshold
    """
)
async def get_related_identities(
    identity_id: str,
    min_co_appearances: int = Query(default=None, ge=1, description="Minimum co-appearances (default from config)"),
    time_window_minutes: int = Query(default=None, ge=1, le=60, description="Time window for co-appearance (default from config)"),
    limit: int = Query(default=20, ge=1, le=100, description="Maximum results"),
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_admin())
):
    """Get identities that frequently appear with this identity."""
    if not settings.RELATED_IDENTITIES_ENABLED:
        raise _feature_disabled("RELATED_IDENTITIES_ENABLED", "Related-identity analysis")
    started = time.monotonic()
    await _get_identity_or_404(db, identity_id)
    try:
        related = await intelligence_service.get_related_identities(
            db=db,
            identity_id=identity_id,
            min_co_appearances=min_co_appearances,
            time_window_minutes=time_window_minutes,
            limit=limit
        )

        items = [
            {
                "identity_id": r.identity_id,
                "display_name": r.display_name,
                "identity_type": r.identity_type,
                "co_appearance_count": r.co_appearance_count,
                "co_appearance_percentage": r.co_appearance_percentage,
                "relationship_strength": r.relationship_strength,
                "common_pipelines": r.common_pipelines,
                "first_co_appearance": _iso_z(r.first_co_appearance),
                "last_co_appearance": _iso_z(r.last_co_appearance),
                "best_snapshot_path": r.best_snapshot_path,
                "snapshot_url": path_to_url(r.best_snapshot_path)
            }
            for r in related
        ]
        _audit("related_identities", current_user, identity_id,
               duration_ms=int((time.monotonic() - started) * 1000),
               row_count=len(items))
        # Envelope: items + the authoritative threshold policy so the UI
        # never hard-codes divergent strength rules.
        return {"items": items, "thresholds": RELATIONSHIP_THRESHOLDS}

    except HTTPException:
        raise
    except Exception as e:
        _audit("related_identities", current_user, identity_id, result="error")
        raise _safe_500("related identity analysis", e)


@router.post(
    "/api/identities/{identity_id}/related/refresh",
    tags=["Intelligence"],
    summary="Refresh Related Identities",
    description="Recalculate and cache relationship data for this identity."
)
async def refresh_relationships(
    identity_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_admin()),
    _csrf: None = Depends(require_intel_csrf)
):
    """Refresh relationship cache for an identity."""
    await _get_identity_or_404(db, identity_id)
    try:
        await intelligence_service.refresh_relationships(db, identity_id)
        _audit("refresh_relationships", current_user, identity_id)
        return {"success": True, "message": "Relationships refreshed"}
    except HTTPException:
        raise
    except Exception as e:
        raise _safe_500("relationship refresh", e)


@router.get(
    "/api/identities/{identity_id}/temporal-patterns",
    response_model=TemporalPatternResponse,
    tags=["Intelligence"],
    summary="Get Temporal Patterns",
    description="""
    Analyze when this identity typically appears.
    
    **Provides:**
    - Hourly distribution (0-23)
    - Daily distribution (Mon-Sun)
    - Peak hours and days
    - Most common locations
    - Average appearances per day
    
    **Use Cases:**
    - Predict when someone will appear
    - Detect unusual behavior (off-pattern appearances)
    - Optimize security coverage
    """
)
async def get_temporal_patterns(
    identity_id: str,
    days_back: int = Query(default=90, ge=1, le=365, description="Days to analyze"),
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_admin())
):
    """Get temporal patterns for an identity."""
    if not settings.TEMPORAL_PATTERNS_ENABLED:
        raise _feature_disabled("TEMPORAL_PATTERNS_ENABLED", "Temporal pattern analysis")
    started = time.monotonic()
    await _get_identity_or_404(db, identity_id)
    try:
        patterns = await intelligence_service.get_temporal_patterns(
            db=db,
            identity_id=identity_id,
            days_back=days_back
        )

        _audit("temporal_patterns", current_user, identity_id,
               duration_ms=int((time.monotonic() - started) * 1000),
               days_back=days_back)
        return {
            "identity_id": patterns.identity_id,
            "hourly_distribution": patterns.hourly_distribution,
            "daily_distribution": patterns.daily_distribution,
            "peak_hours": patterns.peak_hours,
            "peak_days": patterns.peak_days,
            "most_common_pipelines": patterns.most_common_pipelines,
            "total_appearances": patterns.total_appearances,
            "first_appearance": _iso_z(patterns.first_appearance),
            "last_appearance": _iso_z(patterns.last_appearance),
            "average_appearances_per_day": patterns.average_appearances_per_day
        }

    except HTTPException:
        raise
    except Exception as e:
        _audit("temporal_patterns", current_user, identity_id, result="error")
        raise _safe_500("temporal pattern analysis", e)


@router.get(
    "/api/identities/{identity_id}/cross-camera",
    tags=["Intelligence"],
    summary="Get Cross-Camera Tracking",
    description="""
    Track this identity's movement across cameras over time.
    
    **Returns:**
    - Chronological list of camera appearances
    - Time at each location
    - Total cameras visited
    
    **Use Cases:**
    - Reconstruct someone's path through a facility
    - Investigate incidents by tracking movement
    - Analyze traffic patterns
    """
)
async def get_cross_camera_track(
    identity_id: str,
    date: str = Query(default=None, description="Specific date (YYYY-MM-DD)"),
    days_back: int = Query(default=7, ge=1, le=30, description="Days to analyze if no date"),
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_admin())
):
    """Get cross-camera tracking for an identity."""
    if not settings.CROSS_CAMERA_TRACKING_ENABLED:
        raise _feature_disabled("CROSS_CAMERA_TRACKING_ENABLED", "Cross-camera tracking")
    started = time.monotonic()
    await _get_identity_or_404(db, identity_id)
    try:
        tracks = await intelligence_service.get_cross_camera_track(
            db=db,
            identity_id=identity_id,
            date=date,
            days_back=days_back
        )

        payload = [
            {
                "identity_id": t.identity_id,
                "display_name": t.display_name,
                "date": t.date,
                "movements": [
                    {
                        "pipeline_id": m.pipeline_id,
                        "pipeline_name": m.pipeline_name,
                        "timestamp": _iso_z(m.timestamp),
                        "snapshot_path": m.snapshot_path,
                        "snapshot_url": path_to_url(m.snapshot_path),
                        "duration_at_location": m.duration_at_location,
                        "coordinates": m.coordinates
                    }
                    for m in t.movements
                ],
                "total_cameras": t.total_cameras,
                "first_seen": _iso_z(t.first_seen),
                "last_seen": _iso_z(t.last_seen),
                "total_duration_minutes": t.total_duration_minutes
            }
            for t in tracks
        ]
        _audit("cross_camera_track", current_user, identity_id,
               duration_ms=int((time.monotonic() - started) * 1000),
               days=len(payload), date=date, days_back=days_back)
        # Bounded outcomes only: success (found movement), empty (query ran,
        # no track), error. Never an identity id or camera id as a label.
        _observe_intel("cross_camera",
                       "success" if payload else "empty", started)
        return payload

    except HTTPException:
        raise
    except ValueError:
        # e.g. malformed date parameter — do not echo internals
        raise HTTPException(status_code=400, detail="Invalid date parameter (expected YYYY-MM-DD)")
    except Exception as e:
        _audit("cross_camera_track", current_user, identity_id, result="error")
        _observe_intel("cross_camera", "error", started)
        raise _safe_500("cross-camera tracking", e)


@router.get(
    "/api/identities/{identity_id}/map-data",
    tags=["Map Service"],
    summary="Get Map Data (GeoJSON) for the MapLibre map",
    description="""
    Structured map data for the identity's movements — everything the map
    shows, as GeoJSON, rendered client-side by MapLibre GL JS over the offline
    basemap served by Martin. No HTML, no iframe.

    **Returns:** `identity`, `detections`, `route`, `cameras`, `risk_points`,
    `security_zones`, `patterns`, `threats`, `metadata`. Every key is always
    present; disabled features return empty collections.

    Security-analysis overlays are opt-in and default OFF, exactly as the
    previous map endpoint: a missing client checkbox never enables them.
    """
)
async def get_tracking_map_data(
    identity_id: str,
    date: str = Query(default=None, description="Specific date (YYYY-MM-DD)"),
    days_back: int = Query(default=7, ge=1, le=30, description="Days to analyze if no date"),
    show_routes: bool = Query(default=True, description="Include the movement route"),
    enable_security_features: bool = Query(default=False, description="Security zones + watchlist threats"),
    detect_patterns: bool = Query(default=False, description="Detect suspicious movement patterns"),
    show_risk_heatmap: bool = Query(default=False, description="Include risk heatmap points"),
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_admin())
):
    """Map data as GeoJSON for MapLibre."""
    started = time.monotonic()
    await _get_identity_or_404(db, identity_id)
    try:
        tracks = await intelligence_service.get_cross_camera_track(
            db=db, identity_id=identity_id, date=date, days_back=days_back)

        tracks_dict = [
            {
                "identity_id": t.identity_id,
                "display_name": t.display_name,
                "date": t.date,
                "movements": [
                    {
                        "pipeline_id": m.pipeline_id,
                        "pipeline_name": m.pipeline_name,
                        "timestamp": _iso_z(m.timestamp),
                        "snapshot_path": m.snapshot_path,
                        "snapshot_url": path_to_url(m.snapshot_path),
                        "duration_at_location": m.duration_at_location,
                        "coordinates": m.coordinates,
                    }
                    for m in t.movements
                ],
                "total_cameras": t.total_cameras,
                "first_seen": _iso_z(t.first_seen),
                "last_seen": _iso_z(t.last_seen),
                "total_duration_minutes": t.total_duration_minutes,
            }
            for t in (tracks or [])
        ]

        # Same authorized inputs the HTML map endpoint assembled; the data
        # module only derives from them and can return nothing more.
        watchlist_matches = None
        security_zones = None
        if enable_security_features:
            watchlist_matches, security_zones = await _security_inputs(db, identity_id, tracks_dict)

        from backend.core.map_data_service import build_map_data
        payload = build_map_data(
            identity_id=identity_id, tracks=tracks_dict,
            watchlist_matches=watchlist_matches, security_zones=security_zones,
            include_routes=show_routes, detect_patterns=detect_patterns,
            include_risk=show_risk_heatmap, include_security=enable_security_features,
        )

        _audit("tracking_map_data", current_user, identity_id,
               duration_ms=int((time.monotonic() - started) * 1000),
               days_back=days_back, date=date, security_features=enable_security_features)
        _observe_intel("map_data", "success", started)
        resp = JSONResponse(content=payload, status_code=200)
        # Personalized, sensitive geographic content — never shared caches.
        resp.headers["Cache-Control"] = "private, no-store"
        return resp
    except HTTPException:
        raise
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid date parameter (expected YYYY-MM-DD)")
    except Exception as e:
        _observe_intel("map_data", "error", started)
        raise _safe_500("map data generation", e)


@router.get(
    "/api/maps/availability",
    tags=["Map Service"],
    summary="Which basemap styles are actually usable",
    description="""
    Per-style availability derived from the installed offline datasets —
    Martin's catalog plus a representative tile — cached and refreshed under
    supervision, so this call is cheap.

    A style whose dataset is not installed reports
    `OFFLINE_MAP_DATASET_UNAVAILABLE`. There is no fallback: the client must
    disable that style, never substitute another.
    """
)
async def get_map_availability(current_user: dict = Depends(get_current_user)):
    from backend.core import map_availability
    snap = await map_availability.get_or_refresh()
    resp = JSONResponse(content=snap.public(), status_code=200)
    resp.headers["Cache-Control"] = "private, max-age=30"
    return resp


# Single-flight: verification decodes tiles and hashes whole archives. Two
# concurrent runs would double that work and race on the ledger file, and the
# second caller learns nothing the first will not report.
_map_verify_lock = asyncio.Lock()


@router.post(
    "/api/maps/verify",
    tags=["Map Service"],
    summary="Re-measure the content of every installed map dataset",
    description="""
    Decodes a deterministic sample of tiles from each installed archive,
    recomputes its SHA-256, rewrites the content ledger and refreshes
    availability.

    This is the only way to make a dataset usable again after it has been
    replaced on disk: a verdict is bound to the exact bytes it was taken from,
    so new bytes are unverified by definition and report
    `CONTENT_NOT_VERIFIED` until they have been measured.

    Expensive and blocking-by-nature — it runs on a worker thread. Admin only.
    """
)
async def verify_map_datasets(
    current_user: dict = Depends(require_admin()),
    _csrf: None = Depends(require_intel_csrf)
):
    from backend.core import map_availability
    if _map_verify_lock.locked():
        raise HTTPException(status_code=409,
                            detail="a map content verification is already running")
    async with _map_verify_lock:
        outcome = await map_availability.verify_and_refresh(verifier="api")
    failed = {sid: v for sid, v in outcome["verified"].items() if not v.get("pass")}
    logger.info("[MAP] content verification by %s: %d archive(s), %d rejected",
                current_user.get("username"), len(outcome["verified"]), len(failed))
    return JSONResponse(content=outcome, status_code=200)


async def _security_inputs(db: AsyncSession, identity_id: str, tracks_dict: list):
    """Watchlist matches + derived security zones for an identity — the exact
    assembly the HTML map endpoint performed inline, factored so both routes
    hand identical authorized inputs to their renderer/data builder."""
    watchlist_matches = None
    security_zones = None
    try:
        from backend.core.watchlist_service import watchlist_service
        watchlist_matches = await watchlist_service.get_identity_watchlists(db, identity_id)
    except Exception as e:                                             # noqa: BLE001
        logger.warning(f"[MAP] Could not load watchlist matches: {e}")
    try:
        import math
        from sqlalchemy import select
        from db_models import Pipeline
        pipeline_ids = {m.get("pipeline_id") for t in tracks_dict
                        for m in t.get("movements", []) if m.get("pipeline_id")}
        if pipeline_ids:
            result = await db.execute(select(Pipeline).where(Pipeline.pipeline_id.in_(list(pipeline_ids))))
            security_zones = []
            for pipeline in result.scalars().all():
                if pipeline.latitude is None or pipeline.longitude is None:
                    continue
                lat, lng = float(pipeline.latitude), float(pipeline.longitude)
                radius = 0.0009  # ~100 m
                zone_coords = [[lat + radius * math.cos(math.radians(a)),
                                lng + radius * math.sin(math.radians(a))] for a in range(0, 360, 30)]
                pname = pipeline.location_name if getattr(pipeline, "location_name", None) else pipeline.pipeline_id
                low = str(pname).lower()
                if "restricted" in low or "secure" in low:
                    ztype, risk = "restricted", 8
                elif "entrance" in low or "exit" in low:
                    ztype, risk = "high_security", 7
                else:
                    ztype, risk = "monitored", 5
                security_zones.append({
                    "name": pname or f"Zone {pipeline.pipeline_id[:8]}",
                    "coordinates": zone_coords, "zone_type": ztype, "risk_level": risk,
                    "description": f"Security zone around {pname or 'pipeline'}",
                })
    except Exception as e:                                             # noqa: BLE001
        logger.warning(f"[MAP] Could not generate security zones: {e}")
        security_zones = None
    return watchlist_matches, security_zones


@router.get(
    "/api/identities/{identity_id}/timeline",
    response_model=MovementTimelineResponse,
    tags=["Intelligence"],
    summary="Get Movement Timeline",
    description="Get a simplified recent movement timeline for dashboard display."
)
async def get_movement_timeline(
    identity_id: str,
    hours_back: int = Query(default=24, ge=1, le=168, description="Hours to look back"),
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_admin())
):
    """Get movement timeline for an identity."""
    await _get_identity_or_404(db, identity_id)
    try:
        timeline = await intelligence_service.get_movement_timeline(
            db=db,
            identity_id=identity_id,
            hours_back=hours_back
        )
        _audit("movement_timeline", current_user, identity_id, hours_back=hours_back)
        return timeline

    except HTTPException:
        raise
    except Exception as e:
        raise _safe_500("movement timeline", e)


@router.get(
    "/api/identities/{identity_id}/analyze",
    tags=["Intelligence"],
    summary="Complete Identity Analysis",
    description="""
    Perform comprehensive intelligence analysis on an identity.
    
    **Includes:**
    - Related identities (co-appearance analysis)
    - Temporal patterns (hourly/daily distributions)
    - Cross-camera tracking (recent movements)
    - Movement timeline
    
    This is a combined endpoint for full intelligence gathering.
    """
)
async def analyze_identity(
    identity_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_admin())
,
    _rl: None = Depends(rate_limited("identity_analysis", heavy=True))):
    """Get complete analysis for an identity with per-section statuses.

    Each section is computed independently: one failing analysis never
    hides the others, and 'tracking available' is only claimed when
    movement data actually exists.
    """
    started = time.monotonic()
    await _get_identity_or_404(db, identity_id)

    result: Dict = {"identity_id": identity_id, "analyzed_at": _iso_z(datetime.utcnow())}
    sections: Dict = {}

    try:
        related = await intelligence_service.get_related_identities(db, identity_id)
        result["related_identities"] = [
            {
                "identity_id": r.identity_id,
                "display_name": r.display_name,
                "type": r.identity_type,
                "co_appearances": r.co_appearance_count,
                "percentage": r.co_appearance_percentage,
                "strength": r.relationship_strength,
                "common_locations": r.common_pipelines,
            }
            for r in related
        ]
        sections["related"] = {"status": "ready", "count": len(related)}
    except Exception as e:
        logger.error("[INTELLIGENCE] analyze/related failed: %s", e, exc_info=True)
        sections["related"] = {"status": "error", "reason_code": "ANALYSIS_FAILED"}

    try:
        patterns = await intelligence_service.get_temporal_patterns(db, identity_id)
        result["temporal_patterns"] = {
            "hourly_distribution": patterns.hourly_distribution,
            "daily_distribution": patterns.daily_distribution,
            "peak_hours": patterns.peak_hours,
            "peak_days": patterns.peak_days,
            "most_common_pipelines": patterns.most_common_pipelines,
            "total_appearances": patterns.total_appearances,
            "average_appearances_per_day": patterns.average_appearances_per_day,
        }
        sections["temporal"] = {
            "status": "ready",
            "total_appearances": patterns.total_appearances,
            "peak_hours": patterns.peak_hours,
        }
    except Exception as e:
        logger.error("[INTELLIGENCE] analyze/temporal failed: %s", e, exc_info=True)
        sections["temporal"] = {"status": "error", "reason_code": "ANALYSIS_FAILED"}

    try:
        tracks = await intelligence_service.get_cross_camera_track(db, identity_id, days_back=7)
        movement_count = sum(len(t.movements) for t in tracks)
        has_coordinates = any(
            m.coordinates for t in tracks for m in t.movements
        )
        result["cross_camera_tracks"] = [
            {
                "date": t.date,
                "cameras_visited": t.total_cameras,
                "first_seen": _iso_z(t.first_seen),
                "last_seen": _iso_z(t.last_seen),
                "duration_minutes": t.total_duration_minutes,
            }
            for t in tracks
        ]
        if movement_count == 0:
            sections["tracking"] = {"status": "unavailable", "reason_code": "NO_MOVEMENT_DATA"}
        elif not has_coordinates:
            sections["tracking"] = {
                "status": "partial", "reason_code": "NO_COORDINATES",
                "movement_count": movement_count,
                "days_with_activity": len(tracks),
            }
        else:
            sections["tracking"] = {
                "status": "ready",
                "movement_count": movement_count,
                "days_with_activity": len(tracks),
            }
    except Exception as e:
        logger.error("[INTELLIGENCE] analyze/tracking failed: %s", e, exc_info=True)
        sections["tracking"] = {"status": "error", "reason_code": "ANALYSIS_FAILED"}

    result["sections"] = sections
    _audit("complete_analysis", current_user, identity_id,
           duration_ms=int((time.monotonic() - started) * 1000),
           related_status=sections["related"]["status"],
           temporal_status=sections["temporal"]["status"],
           tracking_status=sections["tracking"]["status"])
    return result


# =====================================================
# Security Intelligence Endpoints
# =====================================================

@router.get(
    "/api/security/network",
    tags=["Security Intelligence"],
    summary="Social Network Analysis",
    description="""
    Build a social network graph showing connections between identities.
    
    **Use Cases:**
    - Visualize relationship networks
    - Identify key hubs and influencers
    - Detect clusters and groups
    - Security: map suspect networks
    
    **Returns:**
    - Nodes (identities) with risk scores
    - Edges (connections) with strength
    - Clusters (connected groups)
    - Central nodes (most connected)
    """
)
async def get_social_network(
    identity_ids: Optional[str] = Query(default=None, description="Comma-separated identity IDs to analyze (empty = top-risk scope)"),
    min_connections: int = Query(default=1, ge=0, description="Minimum connections to include"),
    days_back: int = Query(default=90, ge=1, le=365, description="Days to analyze"),
    max_nodes: int = Query(default=NETWORK_DEFAULT_MAX_NODES, ge=1, le=NETWORK_MAX_NODES,
                           description="Maximum nodes returned (server-enforced ceiling)"),
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_admin())
,
    _rl: None = Depends(rate_limited("network_analysis", heavy=True))):
    """Get social network analysis — always bounded.

    Without explicit identity_ids the response is the TOP-RISK slice of the
    graph, never the unbounded full network. The envelope reports scope,
    truncation and totals so the UI can be honest about coverage.
    """
    started = time.monotonic()
    try:
        identity_list = None
        if identity_ids:
            identity_list = [i.strip() for i in identity_ids.split(',') if i.strip()][:50]
        scope = ("ego" if identity_list and len(identity_list) == 1
                 else "selected" if identity_list else "top_risk")

        network = await _bounded_intel_call(
            "network",
            security_intelligence_service.build_social_network(
                db=db,
                identity_ids=identity_list,
                min_connections=min_connections,
                days_back=days_back
            ))

        all_nodes = list(network.nodes)
        total_nodes = len(all_nodes)
        limit_nodes = min(max_nodes, NETWORK_MAX_NODES)
        truncated = total_nodes > limit_nodes
        if truncated:
            # Keep the highest-signal nodes: risk first, then connectivity
            all_nodes.sort(
                key=lambda n: (n.risk_score or 0, n.connections_count or 0),
                reverse=True)
            all_nodes = all_nodes[:limit_nodes]
        kept_ids = {n.identity_id for n in all_nodes}
        kept_edges = [e for e in network.edges
                      if e.source_id in kept_ids and e.target_id in kept_ids]
        if len(kept_edges) > NETWORK_MAX_EDGES:
            kept_edges.sort(key=lambda e: (e.strength or 0), reverse=True)
            kept_edges = kept_edges[:NETWORK_MAX_EDGES]
            truncated = True

        payload = {
            "nodes": [
                {
                    "identity_id": n.identity_id,
                    "display_name": n.display_name,
                    "identity_type": n.identity_type,
                    "appearances_count": n.appearances_count,
                    "risk_score": n.risk_score,
                    "connections_count": n.connections_count,
                    "snapshot_url": n.snapshot_url
                }
                for n in all_nodes
            ],
            "edges": [
                {
                    "source_id": e.source_id,
                    "target_id": e.target_id,
                    "strength": e.strength,
                    "co_appearances": e.co_appearances,
                    "co_appearance_percentage": e.co_appearance_percentage,
                    "first_seen_together": _iso_z(e.first_seen_together),
                    "last_seen_together": _iso_z(e.last_seen_together),
                    "common_locations": e.common_locations,
                    "relationship_type": e.relationship_type
                }
                for e in kept_edges
            ],
            "clusters": [
                [i for i in cluster if i in kept_ids]
                for cluster in (network.clusters or [])
                if any(i in kept_ids for i in cluster)
            ],
            "central_nodes": [i for i in (network.central_nodes or []) if i in kept_ids],
            # isolated = REQUESTED ids with no edges. By construction they are
            # never in kept_ids (nodes exist only for edge endpoints), so
            # filtering by kept_ids provably always emitted [] and the whole
            # feature was dead on arrival. Requested ids are capped at 50.
            "isolated_nodes": list(network.isolated_nodes or [])[:50],
            "scope": scope,
            "truncated": truncated,
            "total_nodes": total_nodes,
            "returned_nodes": len(all_nodes),
            "max_nodes": limit_nodes,
            # Node risk rubric provenance — three risk rubrics coexist on the
            # security page; each response labels which one produced its score.
            "risk_score_version": NETWORK_RISK_VERSION,
        }
        _audit("social_network", current_user,
               duration_ms=int((time.monotonic() - started) * 1000),
               scope=scope, returned_nodes=len(all_nodes), total_nodes=total_nodes,
               edges=len(kept_edges), days_back=days_back)
        return payload

    except HTTPException:
        _audit("social_network", current_user, result="error")
        raise
    except Exception as e:
        _audit("social_network", current_user, result="error")
        raise _safe_500("social network analysis", e)


@router.get(
    "/api/security/patterns",
    tags=["Security Intelligence"],
    summary="Detect Suspicious Patterns",
    description="""
    Detect suspicious behavioral patterns across all identities.
    
    **Patterns Detected:**
    - Group activity (multiple people together, recurring groups score higher)
    - Unusual timing (activity in the configured off-hours window)
    - Rapid movement (implied speed between cameras above threshold)

    **Use Cases:**
    - Security: detect coordinated activities
    - Investigation: identify suspicious groups
    - Monitoring: flag unusual behaviors
    """
)
async def get_suspicious_patterns(
    days_back: int = Query(default=30, ge=1, le=90, description="Days to analyze"),
    min_group_size: int = Query(default=3, ge=2, le=20, description="Minimum group size for detection"),
    pipeline_id: Optional[str] = Query(default=None, max_length=255,
                                       description="Restrict the scan to one camera/pipeline"),
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_admin())
,
    _rl: None = Depends(rate_limited("pattern_detection", heavy=True))):
    """Detect suspicious patterns — enveloped so truncation is visible."""
    started = time.monotonic()
    try:
        pipeline_id = (pipeline_id or "").strip() or None
        if pipeline_id:
            exists = (await db.execute(
                select(Pipeline.pipeline_id).where(Pipeline.pipeline_id == pipeline_id))).scalar()
            if not exists:
                raise HTTPException(status_code=404, detail="Unknown pipeline")
        report = await _bounded_intel_call(
            "patterns",
            security_intelligence_service.detect_suspicious_patterns(
                db=db,
                days_back=days_back,
                min_group_size=min_group_size,
                pipeline_id=pipeline_id,
            ))

        items = [
            {
                "pattern_type": p.pattern_type,
                "description": p.description,
                "identities_involved": p.identities_involved,
                "severity": p.severity,
                "confidence": p.confidence,
                "first_detected": _iso_z(p.first_detected),
                "evidence": p.evidence,
                "locations": p.locations,
                "time_range": [
                    _iso_z(p.time_range[0]),
                    _iso_z(p.time_range[1])
                ]
            }
            for p in report.patterns
        ]
        _audit("suspicious_patterns", current_user,
               duration_ms=int((time.monotonic() - started) * 1000),
               days_back=days_back, row_count=len(items),
               truncated=report.truncated, scanned_rows=report.scanned_rows)
        return {
            "items": items,
            "total": len(items),
            "truncated": report.truncated,
            "scanned_rows": report.scanned_rows,
            "analysis_window": {
                "start": _iso_z(report.window_start),
                "end": _iso_z(report.window_end),
                "days_back": days_back,
            },
            "pipeline_id": report.pipeline_id,
            "scope_note": report.scope_note,
            "algorithm_version": report.algorithm_version,
        }

    except HTTPException:
        _audit("suspicious_patterns", current_user, result="error")
        raise
    except Exception as e:
        _audit("suspicious_patterns", current_user, result="error")
        raise _safe_500("pattern detection", e)


@router.get(
    "/api/security/anomalies/{identity_id}",
    tags=["Security Intelligence"],
    summary="Detect Behavioral Anomalies",
    description="""
    Detect behavioral anomalies for a specific identity.
    
    **Anomalies Detected:**
    - Off-schedule activity (circular-hour deviation from the identity's baseline)
    - New location (camera never seen in the baseline window)

    **Use Cases:**
    - Security: flag suspicious behavior changes
    - Investigation: identify deviations from normal patterns
    """
)
async def get_anomalies(
    identity_id: str,
    days_back: int = Query(default=90, ge=1, le=365, description="Days to analyze"),
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_admin())
,
    _rl: None = Depends(rate_limited("anomaly_analysis", heavy=True))):
    """Detect anomalies — enveloped so 'no anomalies' and 'not enough
    history to judge' are distinguishable states, not both empty lists."""
    await _get_identity_or_404(db, identity_id)
    started = time.monotonic()
    try:
        report = await _bounded_intel_call(
            "anomalies",
            security_intelligence_service.detect_anomalies(
                db=db,
                identity_id=identity_id,
                days_back=days_back
            ))

        items = [
            {
                "identity_id": a.identity_id,
                "anomaly_type": a.anomaly_type,
                "description": a.description,
                "severity": a.severity,
                "detected_at": _iso_z(a.detected_at),
                "baseline": a.baseline,
                "deviation": a.deviation,
                "risk_score": a.risk_score
            }
            for a in report.anomalies
        ]
        _audit("anomaly_detection", current_user, identity_id,
               duration_ms=int((time.monotonic() - started) * 1000),
               days_back=days_back, row_count=len(items),
               baseline_sufficient=report.baseline_sufficient,
               baseline_samples=report.baseline_samples)
        return {
            "items": items,
            "total": len(items),
            "truncated": report.truncated,
            "baseline": {
                "sufficient": report.baseline_sufficient,
                "samples": report.baseline_samples,
                "window_start": _iso_z(report.baseline_start),
                "window_end": _iso_z(report.baseline_end),
            },
            "recent_count": report.recent_count,
            "algorithm_version": report.algorithm_version,
            # anomaly-context-v3: timezone + day-bucket configuration and the
            # per-bucket baseline statistics the evaluation used.
            "context": report.context,
        }

    except HTTPException:
        _audit("anomaly_detection", current_user, identity_id, result="error")
        raise
    except Exception as e:
        _audit("anomaly_detection", current_user, identity_id, result="error")
        raise _safe_500("anomaly detection", e)


@router.get(
    "/api/security/threat/{identity_id}",
    tags=["Security Intelligence"],
    summary="Threat Assessment",
    description="""
    Perform comprehensive threat assessment for an identity.
    
    **Risk Factors:**
    - Identity type (unknown = higher risk)
    - Connection count (hub = higher risk)
    - Behavioral anomalies
    - Suspicious patterns
    - Recent activity levels
    
    **Returns:**
    - Overall risk score (0-100)
    - Threat level (critical/high/medium/low/minimal)
    - Risk factors with details
    - Recommendations
    """
)
async def get_threat_assessment(
    identity_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_admin())
,
    _rl: None = Depends(rate_limited("threat_assessment", heavy=True))):
    """Get threat assessment for an identity.

    DEPRECATED SIDE EFFECT: this GET also persists the assessment (kept
    temporarily for backward compatibility — idempotent inside the dedup
    window, and a persistence failure never breaks the read). The canonical
    creation endpoint is POST /api/security/assessments; new clients should
    use it and treat this GET as read-only in a future release.
    """
    await _get_identity_or_404(db, identity_id)
    started = time.monotonic()
    try:
        from backend.ml.decision_service import decision_service
        outcome = await _bounded_intel_call(
            "threat",
            decision_service.decide(db, identity_id))
        assessment = outcome.assessment

        # Persist EVERY generated assessment (idempotent inside the dedup
        # window — repeated views collapse onto one row). A persistence
        # failure must not take down the read: the response then carries
        # persisted=false and the failure is logged + counted.
        assessment_id = None
        persisted = False
        deduplicated = None
        try:
            from backend.core.assessment_service import assessment_service
            from backend.routes.risk_assessments import _threshold_provenance
            stored = await assessment_service.persist_identity_assessment(
                db, identity_id=identity_id, assessment=assessment,
                threshold_version=await _threshold_provenance(db),
                decision_mode=outcome.actual_mode_used)
            assessment_id = stored["id"]
            persisted = True
            deduplicated = stored.get("deduplicated")
        except Exception:
            logger.warning("[INTELLIGENCE] assessment persistence failed identity=%s",
                           identity_id, exc_info=True)

        # SHADOW: bounded parallel anomaly evaluation AFTER the live result
        # is fully determined — it can only ever write comparison rows, never
        # touch the response (swallow-all inside).
        if outcome.shadow_planned:
            from backend.ml.shadow_service import shadow_service
            await shadow_service.run_shadow(
                identity_id=identity_id,
                rule_score=assessment.overall_risk_score,
                rule_severity=assessment.severity or assessment.threat_level,
                assessment_id=assessment_id,
                event_time=getattr(assessment, "last_assessed", None))

        _audit("threat_assessment", current_user, identity_id,
               duration_ms=int((time.monotonic() - started) * 1000),
               threat_level=assessment.threat_level,
               risk_score=assessment.overall_risk_score,
               assessment_id=assessment_id)
        payload = {
            "identity_id": assessment.identity_id,
            "display_name": assessment.display_name,
            "overall_risk_score": assessment.overall_risk_score,
            "risk_factors": assessment.risk_factors,
            "threat_level": assessment.threat_level,
            "severity": assessment.severity or assessment.threat_level,
            "confidence": assessment.confidence,
            "recommendations": assessment.recommendations,
            "last_assessed": _iso_z(assessment.last_assessed),
            "algorithm_version": assessment.algorithm_version,
            "assessment_id": assessment_id,
            "persisted": persisted,
            "deduplicated": deduplicated,
            "engine": assessment.engine,
            # ML first release: which mode handled this decision. In every
            # currently-possible value the LIVE result above is the rules
            # result; gated modes record their exact unmet reasons.
            "decision": outcome.decision_record,
        }
        # Honest labelling: this number is a weighted heuristic, never a
        # probability — 80 does not mean an 80% chance of anything.
        if assessment.engine:
            for key in ("score_type", "is_probability", "calibration_status", "limitations"):
                payload[key] = assessment.engine.get(key)
        else:
            payload.update({"score_type": "heuristic", "is_probability": False,
                            "calibration_status": "uncalibrated", "limitations": []})
        return payload

    except ValueError:
        raise HTTPException(status_code=404, detail="Identity not found")
    except HTTPException:
        _audit("threat_assessment", current_user, identity_id, result="error")
        raise
    except Exception as e:
        _audit("threat_assessment", current_user, identity_id, result="error")
        raise _safe_500("threat assessment", e)


@router.post(
    "/api/intelligence/relationships/calculate-all",
    tags=["Intelligence"],
    summary="Calculate All Relationships",
    description="""
    Trigger background task to calculate and cache relationships for all identities.
    
    This task:
    - Processes all active identities
    - Calculates co-appearance relationships
    - Populates the identity_relationships cache table
    - Improves performance of social network analysis
    
    **Notifications:**
    - You will be notified when the task starts
    - You will be notified when the task completes
    
    **Duration:**
    - Typically 5-30 minutes depending on number of identities
    """
)
async def calculate_all_relationships_endpoint(
    background_tasks: BackgroundTasks,
    request: Request,
    current_user: dict = Depends(require_admin()),
    _csrf: None = Depends(require_intel_csrf)
,
    _rl: None = Depends(rate_limited("relationship_recalc", heavy=True))):
    """Schedule the calculate-all background job (202 + job_id).

    Single-flight: while one run is active, further requests get
    409 Conflict with the running job's id. The heavy work never runs
    inside this HTTP request.
    """
    job_id = f"relationships-{uuid_mod.uuid4().hex[:8]}"
    running = _try_acquire_relationship_job(job_id)
    if running is not None:
        raise HTTPException(
            status_code=409,
            detail={
                "error_code": "JOB_ALREADY_RUNNING",
                "message": "A relationship calculation is already running.",
                "job_id": running,
            },
        )

    # Cross-worker layer: identical 409 semantics when ANOTHER worker holds
    # the job (bounded TTL matches the in-process stale-guard, so a crashed
    # worker's lock self-expires).
    dlock = DistributedLock("relationship-job",
                            ttl_seconds=_RELATIONSHIP_JOB_MAX_AGE_SECONDS)
    if not await dlock.acquire(holder_label=job_id):
        _release_relationship_job(job_id)
        raise HTTPException(
            status_code=409,
            detail={
                "error_code": "JOB_ALREADY_RUNNING",
                "message": "A relationship calculation is already running (another worker).",
                "job_id": dlock.holder_hint or "unknown",
            },
        )

    try:
        from backend.core.task_history import task_history_manager
        task_id = await task_history_manager.create_job(
            job_id=job_id,
            task_type="relationship_calculation",
            task_name="Calculate Identity Relationships",
            description="Calculate co-appearance relationships for all identities",
        )

        async def _run_and_release():
            try:
                await calculate_all_relationships(job_id=job_id)
            finally:
                _release_relationship_job(job_id)
                await dlock.release()

        background_tasks.add_task(_run_and_release)

        _audit("relationships_calculate_all", current_user, job_id=job_id)
        logger.info("[INTELLIGENCE] relationship job scheduled job_id=%s user_id=%s",
                    job_id, (current_user or {}).get("id"))

        return JSONResponse(
            status_code=202,
            content={
                "accepted": True,
                "job_id": job_id,
                "task_id": task_id,
                "status": "scheduled",
                "message": "Relationship calculation scheduled. Monitor progress in Background Tasks.",
                "task_type": "relationship_calculation",
            },
        )
    except HTTPException:
        _release_relationship_job(job_id)
        await dlock.release()
        raise
    except Exception as e:
        _release_relationship_job(job_id)
        await dlock.release()
        raise _safe_500("relationship calculation scheduling", e)


@router.get(
    "/api/intelligence/relationships/jobs/{job_id}",
    tags=["Intelligence"],
    summary="Get Relationship Job Status",
    description="Poll status/progress of a calculate-all relationships job."
)
async def get_relationship_job(
    job_id: str,
    current_user: dict = Depends(require_admin())
):
    """Return task-history record for a relationship job."""
    from backend.core.task_history import task_history_manager
    task = await task_history_manager.get_task_by_job_id(job_id)
    if not task or task.get("task_type") != "relationship_calculation":
        raise HTTPException(status_code=404, detail="Job not found")
    resp = JSONResponse(content=task)
    resp.headers["Cache-Control"] = "no-store"
    return resp


# =====================================================
# Advanced SNA Enhancement Endpoints
# =====================================================

@router.post(
    "/api/intelligence/thresholds/learn",
    response_model=ThresholdLearningResponse,
    tags=["Intelligence - Advanced Features"],
    summary="Learn Optimal Thresholds",
    description="""
    Learn optimal distance and time thresholds for all camera pairs based on historical data.
    
    **What It Does:**
    - Analyzes historical cross-camera movements
    - Learns optimal time windows per camera pair
    - Learns optimal distance thresholds per camera pair
    - Adapts to actual travel patterns
    
    **Use Cases:**
    - Initial setup: Learn thresholds for your camera network
    - After adding cameras: Update thresholds for new pairs
    - Periodic refresh: Re-learn as patterns change (monthly recommended)
    
    **Requirements:**
    - Pipelines must have coordinates (latitude/longitude) set
    - Need at least 10 cross-camera movements per pair
    - Historical data: 90+ days recommended
    
    **Returns:**
    - Learned thresholds for each camera pair
    - Confidence scores (0.0 to 1.0)
    - Sample counts (number of movements analyzed)
    
    **Duration:** 1-5 minutes (depending on number of cameras)
    """,
    responses={
        200: {
            "description": "Thresholds learned successfully",
            "content": {
                "application/json": {
                    "example": {
                        "status": "success",
                        "learned_pairs": 3,
                        "thresholds": [
                            {
                                "camera_1": "camera_1",
                                "camera_2": "camera_2",
                                "optimal_time_window_minutes": 5.2,
                                "optimal_distance_meters": 240.0,
                                "actual_distance_meters": 200.0,
                                "confidence": 0.85,
                                "sample_count": 42
                            }
                        ]
                    }
                }
            }
        },
        500: {"description": "Server error"}
    }
)
async def learn_thresholds(
    pipeline_ids: Optional[str] = Query(
        default=None,
        description="Comma-separated pipeline IDs (e.g., 'camera_1,camera_2'). Leave empty to learn for all active pipelines.",
        example="camera_1,camera_2,camera_3"
    ),
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_admin()),
    _csrf: None = Depends(require_intel_csrf)
,
    _rl: None = Depends(rate_limited("threshold_learning", heavy=True))):
    """DEPRECATED synchronous variant — prefer POST /api/intelligence/thresholds/jobs.

    Holds the SAME single-flight guard as the job path: this endpoint used to
    bypass it, so a sync call could run concurrently with a scheduled job.
    """
    sync_job_id = f"threshold-sync-{uuid_mod.uuid4().hex[:8]}"
    running = _try_acquire_threshold_job(sync_job_id)
    if running is not None:
        raise HTTPException(
            status_code=409,
            detail={
                "error_code": "JOB_ALREADY_RUNNING",
                "message": "A threshold learning job is already running.",
                "job_id": running,
            },
        )
    sync_dlock = DistributedLock("threshold-job",
                                 ttl_seconds=_THRESHOLD_JOB_MAX_AGE_SECONDS)
    if not await sync_dlock.acquire(holder_label=sync_job_id):
        _release_threshold_job(sync_job_id)
        raise HTTPException(
            status_code=409,
            detail={
                "error_code": "JOB_ALREADY_RUNNING",
                "message": "A threshold learning job is already running (another worker).",
                "job_id": sync_dlock.holder_hint or "unknown",
            },
        )
    try:
        if pipeline_ids:
            pipeline_list = [pid.strip() for pid in pipeline_ids.split(',')]
        else:
            # Get all active pipelines
            from db_models import Pipeline
            from sqlalchemy import select
            query = select(Pipeline).where(Pipeline.is_active == 1)
            result = await db.execute(query)
            pipelines = result.scalars().all()
            pipeline_list = [p.pipeline_id for p in pipelines]

        learned = await threshold_learner.learn_all_camera_pairs(db, pipeline_list)

        return ThresholdLearningResponse(
            status="success",
            learned_pairs=len(learned),
            thresholds=[
                ThresholdData(
                    camera_1=pair[0],
                    camera_2=pair[1],
                    optimal_time_window_minutes=data['optimal_time_window_minutes'],
                    optimal_distance_meters=data['optimal_distance_meters'],
                    actual_distance_meters=data['actual_distance_meters'],
                    confidence=data['confidence'],
                    sample_count=data['sample_count'],
                    p95_minutes=data.get('p95_minutes'),
                    spread_minutes=data.get('spread_minutes')
                )
                for pair, data in learned.items()
            ]
        )

    except HTTPException:
        raise
    except Exception as e:
        raise _safe_500("threshold learning", e)
    finally:
        _release_threshold_job(sync_job_id)
        await sync_dlock.release()


async def _persist_threshold_candidates(db, learned: dict) -> int:
    """Persist learning output as CANDIDATE learned_thresholds rows —
    global + per-pipeline scopes, activation strictly manual (an admin
    reviews and activates via /api/security/learned-thresholds)."""
    from backend.core.threshold_store import (
        threshold_store, SIGNAL_DISTANCE, SIGNAL_TIME_WINDOW)
    if not learned:
        return 0
    per_pipeline: Dict[str, Dict[str, list]] = {}
    all_windows, all_distances, total_samples = [], [], 0
    for (cam1, cam2), data in learned.items():
        window = float(data.get("optimal_time_window_minutes") or 0)
        distance = float(data.get("optimal_distance_meters") or 0)
        samples = int(data.get("sample_count") or 0)
        all_windows.append(window)
        all_distances.append(distance)
        total_samples += samples
        for cam in (cam1, cam2):
            bucket = per_pipeline.setdefault(cam, {"windows": [], "distances": [], "samples": 0, "pairs": []})
            bucket["windows"].append(window)
            bucket["distances"].append(distance)
            bucket["samples"] += samples
            bucket["pairs"].append({"pair": [cam1, cam2], "window": window,
                                    "distance": distance, "samples": samples})
    written = 0
    # Global candidates: the max over learned routes (covers the slowest one).
    await threshold_store.record_candidate(
        db, scope_type="global", scope_id="", signal_name=SIGNAL_TIME_WINDOW,
        value=max(all_windows), sample_count=total_samples,
        extras={"aggregation": "max_over_pairs", "pairs": len(learned)})
    await threshold_store.record_candidate(
        db, scope_type="global", scope_id="", signal_name=SIGNAL_DISTANCE,
        value=max(all_distances), sample_count=total_samples,
        extras={"aggregation": "max_over_pairs", "pairs": len(learned)})
    written += 2
    for cam, bucket in per_pipeline.items():
        await threshold_store.record_candidate(
            db, scope_type="pipeline", scope_id=cam, signal_name=SIGNAL_TIME_WINDOW,
            value=max(bucket["windows"]), sample_count=bucket["samples"],
            extras={"aggregation": "max_over_pairs", "pairs": bucket["pairs"][:20]})
        written += 1
    await db.commit()
    return written


async def _run_threshold_job(job_id: str, pipeline_list: Optional[List[str]],
                             dlock: Optional[DistributedLock] = None):
    """Background worker for threshold learning — never inside an HTTP request."""
    from backend.core.task_history import task_history_manager
    from db_connection import db_manager
    started = time.monotonic()
    await task_history_manager.mark_running(job_id)
    candidates_written = 0
    try:
        async with db_manager.get_session() as db:
            if not pipeline_list:
                result = await db.execute(select(Pipeline).where(Pipeline.is_active == 1))
                pipeline_list = [p.pipeline_id for p in result.scalars().all()]
            learned = await threshold_learner.learn_all_camera_pairs(db, pipeline_list)
            try:
                candidates_written = await _persist_threshold_candidates(db, learned)
            except Exception:
                logger.warning("[INTELLIGENCE] threshold candidate persistence failed "
                               "job_id=%s (results still reported)", job_id, exc_info=True)

        thresholds = [
            {
                "camera_1": pair[0],
                "camera_2": pair[1],
                "optimal_time_window_minutes": data.get("optimal_time_window_minutes"),
                "optimal_distance_meters": data.get("optimal_distance_meters"),
                "actual_distance_meters": data.get("actual_distance_meters"),
                "confidence": data.get("confidence"),
                "sample_count": data.get("sample_count"),
                "p95_minutes": data.get("p95_minutes"),
                "spread_minutes": data.get("spread_minutes"),
            }
            for pair, data in learned.items()
        ]
        result_payload = {
            "learned_pairs": len(thresholds),
            "thresholds": thresholds[:200],
            "algorithm_version": THRESHOLD_ALGORITHM_VERSION,
            "calculated_at": _iso_z(datetime.utcnow()),
            "pipelines_scoped": len(pipeline_list or []),
            "candidates_written": candidates_written,
            "activation_note": ("Learned values are CANDIDATES — nothing is "
                                "consumed until activated via "
                                "/api/security/learned-thresholds."),
        }
        await task_history_manager.finish_job(job_id, success=True, result=result_payload)
        logger.info("[INTELLIGENCE] threshold job completed job_id=%s learned_pairs=%s "
                    "candidates=%s duration_ms=%s",
                    job_id, len(thresholds), candidates_written,
                    int((time.monotonic() - started) * 1000))
    except Exception as e:
        logger.error("[INTELLIGENCE] threshold job failed job_id=%s error=%s", job_id, e, exc_info=True)
        await task_history_manager.finish_job(
            job_id, success=False,
            error_code="THRESHOLD_JOB_FAILED", error_message=str(e)[:500])
    finally:
        _release_threshold_job(job_id)
        if dlock is not None:
            await dlock.release()


@router.post(
    "/api/intelligence/thresholds/jobs",
    tags=["Intelligence - Advanced Features"],
    summary="Schedule Threshold Learning Job",
    description="Schedule threshold learning as a background job (202 + job_id; 409 while one is running)."
)
async def create_threshold_job(
    request: Request,
    pipeline_ids: Optional[str] = Query(default=None, description="Comma-separated pipeline IDs; empty = all active"),
    background_tasks: BackgroundTasks = None,
    current_user: dict = Depends(require_admin()),
    _csrf: None = Depends(require_intel_csrf)
,
    _rl: None = Depends(rate_limited("threshold_learning", heavy=True))):
    """Schedule threshold learning in the background — single-flight
    (in-process guard + cross-worker distributed lock)."""
    job_id = f"threshold-{uuid_mod.uuid4().hex[:8]}"
    running = _try_acquire_threshold_job(job_id)
    if running is not None:
        raise HTTPException(
            status_code=409,
            detail={
                "error_code": "JOB_ALREADY_RUNNING",
                "message": "A threshold learning job is already running.",
                "job_id": running,
            },
        )
    dlock = DistributedLock("threshold-job", ttl_seconds=_THRESHOLD_JOB_MAX_AGE_SECONDS)
    if not await dlock.acquire(holder_label=job_id):
        _release_threshold_job(job_id)
        raise HTTPException(
            status_code=409,
            detail={
                "error_code": "JOB_ALREADY_RUNNING",
                "message": "A threshold learning job is already running (another worker).",
                "job_id": dlock.holder_hint or "unknown",
            },
        )
    try:
        pipeline_list = None
        if pipeline_ids:
            pipeline_list = [p.strip() for p in pipeline_ids.split(',') if p.strip()][:100]

        from backend.core.task_history import task_history_manager
        task_id = await task_history_manager.create_job(
            job_id=job_id,
            task_type="threshold_learning",
            task_name="Learn Camera-Pair Thresholds",
            description="Learn optimal time/distance thresholds per camera pair",
        )
        background_tasks.add_task(_run_threshold_job, job_id, pipeline_list, dlock)
        _audit("threshold_job_scheduled", current_user, job_id=job_id,
               pipeline_scope=len(pipeline_list) if pipeline_list else "all")
        return JSONResponse(
            status_code=202,
            content={
                "accepted": True,
                "job_id": job_id,
                "task_id": task_id,
                "status": "scheduled",
                "task_type": "threshold_learning",
            },
        )
    except HTTPException:
        _release_threshold_job(job_id)
        await dlock.release()
        raise
    except Exception as e:
        _release_threshold_job(job_id)
        await dlock.release()
        raise _safe_500("threshold job scheduling", e)


@router.get(
    "/api/intelligence/thresholds/jobs/{job_id}",
    tags=["Intelligence - Advanced Features"],
    summary="Get Threshold Job Status",
)
async def get_threshold_job(
    job_id: str,
    current_user: dict = Depends(require_admin())
):
    """Status of one threshold-learning job. While the deprecated synchronous path holds the guard, its id answers with a synthetic running payload instead of 404."""
    from backend.core.task_history import task_history_manager
    task = await task_history_manager.get_task_by_job_id(job_id)
    if not task or task.get("task_type") != "threshold_learning":
        # The DEPRECATED sync endpoint holds the shared guard under a
        # "threshold-sync-*" id with no task-history row. The 409 from the
        # job endpoint (and capabilities) reports that id — a client polling
        # it must see "running", not a 404 pointing at a job that does not
        # exist in Background Tasks.
        if job_id == _threshold_job_running():
            resp = JSONResponse(content={
                "job_id": job_id,
                "task_type": "threshold_learning",
                "status": "running",
                "synthetic": True,
                "detail": "Synchronous threshold learning in progress (no task-history row).",
            })
            resp.headers["Cache-Control"] = "no-store"
            return resp
        raise HTTPException(status_code=404, detail="Job not found")
    resp = JSONResponse(content=task)
    resp.headers["Cache-Control"] = "no-store"
    return resp


@router.get(
    "/api/security/capabilities",
    tags=["Security Intelligence"],
    summary="Get Security Feature Capabilities",
    description="Report ACTUAL backend readiness per feature — never hard-coded 'enabled'."
)
async def get_security_capabilities(
    current_user: dict = Depends(require_admin())
):
    """Honest feature-status report used by the frontend status dialog."""
    import importlib.util

    from backend.core import map_availability
    _map_snapshot = map_availability.cached()
    usable_basemaps = sorted(name for name, ok in
                             (_map_snapshot.public()["styles"].items() if _map_snapshot else [])
                             if ok)
    running_threshold_job = _threshold_job_running() or await peek_holder("threshold-job")
    with _relationship_job_lock:
        running_rel_job = _RELATIONSHIP_JOB["job_id"]
    running_rel_job = running_rel_job or await peek_holder("relationship-job")


    caps = {
        "network_analysis": {
            "enabled": True, "status": "ready",
            "risk_score_version": NETWORK_RISK_VERSION,
        },
        "pattern_detection": {
            "enabled": True, "status": "ready",
            "algorithm_version": PATTERN_ALGORITHM_VERSION,
        },
        "anomaly_detection": {
            "enabled": True, "status": "ready",
            "algorithm_version": ANOMALY_ALGORITHM_VERSION,
        },
        "threat_assessment": {
            "enabled": True, "status": "ready",
            "algorithm_version": THREAT_ALGORITHM_VERSION,
            "score_type": "heuristic",
            "calibration_status": "uncalibrated",
        },
        "assessment_persistence": {
            "enabled": True, "status": "ready",
            "detail": "Assessments persist to threat_assessments with idempotent dedup.",
        },
        "threshold_learning": {
            "enabled": True,
            "status": "job_running" if running_threshold_job else "ready",
            "job_id": running_threshold_job,
            "algorithm_version": THRESHOLD_ALGORITHM_VERSION,
        },
        "trajectory_prediction": {
            "enabled": True,
            "status": "ready",
            "model_version": TRAJECTORY_MODEL_VERSION,
        },
        "activity_correlation": {
            "enabled": True,
            "status": "ready",
            "algorithm_version": CORRELATION_ALGORITHM_VERSION,
        },
        "relationship_calculation": {
            "enabled": True,
            "status": "job_running" if running_rel_job else "ready",
            "job_id": running_rel_job,
        },
        "map_generation": {
            # The map is rendered by MapLibre in the browser over Martin's
            # offline datasets; there is no server-side renderer any more. This
            # reports whether a basemap is actually usable, which is what an
            # operator needs to know — it used to report whether the `folium`
            # package was importable, which said nothing about the map.
            "enabled": bool(usable_basemaps),
            "status": "ready" if usable_basemaps else "no_basemap_installed",
            "styles_available": usable_basemaps,
        },
        "offline_maps": {
            # Offline-ness is now a property of the INSTALLED DATASETS, not of a
            # flag over a raster directory: the pyramid this used to describe was
            # 145,718 copies of OpenStreetMap's "Access blocked" placeholder and
            # is gone. Every basemap is served by Martin from map-data/production.
            "enabled": bool(usable_basemaps),
            "status": "ready" if usable_basemaps else "no_basemap_installed",
            "styles_available": usable_basemaps,
        },
    }
    resp = JSONResponse(content={"capabilities": caps, "checked_at": _iso_z(datetime.utcnow())})
    resp.headers["Cache-Control"] = "no-store"
    return resp


@router.get(
    "/api/intelligence/trajectory/predict",
    response_model=TrajectoryPredictionResponse,
    tags=["Intelligence - Advanced Features"],
    summary="Predict Next Camera",
    description="""
    Predict where a person will appear next based on historical movement patterns.
    
    **What It Does:**
    - Analyzes historical trajectories for the identity
    - Predicts next camera locations with probability scores
    - Estimates arrival times based on historical travel patterns
    - Returns top K most likely destinations
    
    **Use Cases:**
    - **Proactive Relationship Detection**: Predict where person will be, check if associates are there
    - **Better Cross-Camera Matching**: Prioritize matching at predicted cameras
    - **Anomaly Detection**: Flag if person takes unusual path (not in predictions)
    - **Security**: Predict suspicious movements and coordinate responses
    
    **Requirements:**
    - Identity must have at least 3 historical trajectories
    - Identity must have appeared at current camera before
    - Historical data: 90+ days recommended
    
    **Returns:**
    - List of predicted cameras sorted by probability (highest first)
    - Probability scores (0.0 to 1.0)
    - Estimated arrival times
    
    **Performance:** ~50-200ms per prediction
    """,
    responses={
        200: {
            "description": "Trajectory predicted successfully",
            "content": {
                "application/json": {
                    "example": {
                        "identity_id": "123e4567-e89b-12d3-a456-426614174000",
                        "current_camera": "camera_1",
                        "predictions": [
                            {
                                "camera_id": "camera_3",
                                "probability": 0.75,
                                "estimated_time": "2026-01-11T15:30:00Z"
                            },
                            {
                                "camera_id": "camera_2",
                                "probability": 0.20,
                                "estimated_time": "2026-01-11T15:32:00Z"
                            }
                        ]
                    }
                }
            }
        },
        400: {"description": "Invalid parameters"},
        500: {"description": "Server error"}
    }
)
async def predict_next_camera(
    identity_id: str = Query(..., description="Identity UUID to predict trajectory for", example="123e4567-e89b-12d3-a456-426614174000"),
    current_camera: str = Query(..., description="Current camera/pipeline ID where identity is located", example="camera_1"),
    top_k: int = Query(default=3, ge=1, le=10, description="Number of top predictions to return (1-10)", example=3),
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_admin())
,
    _rl: None = Depends(rate_limited("trajectory", heavy=True))):
    """Predict next camera locations for an identity."""
    if not settings.TRAJECTORY_PREDICTION_ENABLED:
        raise _feature_disabled("TRAJECTORY_PREDICTION_ENABLED", "Trajectory prediction")
    await _get_identity_or_404(db, identity_id)
    try:
        predictions = await trajectory_predictor.predict_next_cameras(
            db=db,
            identity_id=identity_id,
            current_camera=current_camera,
            current_time=datetime.utcnow(),
            top_k=top_k
        )

        def _confidence(prob: float) -> str:
            return "high" if prob >= 0.6 else "moderate" if prob >= 0.3 else "low"

        _audit("trajectory_prediction", current_user, identity_id,
               current_camera=current_camera, predictions=len(predictions))
        return TrajectoryPredictionResponse(
            identity_id=identity_id,
            current_camera=current_camera,
            predictions=[
                TrajectoryPredictionItem(
                    camera_id=camera,
                    probability=float(prob),
                    estimated_time=_iso_z(est_time),
                    confidence=_confidence(float(prob))
                )
                for camera, prob, est_time in predictions
            ],
            model_version=TRAJECTORY_MODEL_VERSION,
            insufficient_evidence=len(predictions) == 0,
            note=("Estimated times are statistical projections from historical "
                  "movement, not certainties.")
        )

    except HTTPException:
        raise
    except Exception as e:
        raise _safe_500("trajectory prediction", e)


@router.get(
    "/api/intelligence/correlation/calculate",
    response_model=ActivityCorrelationResponse,
    tags=["Intelligence - Advanced Features"],
    summary="Calculate Activity Correlation (xCCA)",
    description="""
    Calculate correlation between two identities' activities using Cross-Camera Correlation Analysis (xCCA).
    
    **What It Does:**
    - Measures **temporal and spatial association** between activities at different cameras
    - Identifies coordinated movements (people moving together)
    - Scores relationship confidence (0.0 to 1.0)
    - Finds activity sequences (Person A at Camera 1 → Person B at Camera 2)
    
    **Use Cases:**
    - **Higher Confidence Relationship Detection**: Distinguish coincidental vs. coordinated appearances
    - **Coordinated Activity Detection**: Identify groups moving together (security use case)
    - **Security Investigation**: Detect suspicious patterns and coordinated behaviors
    - **Relationship Quality Assessment**: Filter out false positives from coincidental co-appearances
    
    **Correlation Strength:**
    - **Strong** (≥0.7): High confidence relationship, likely coordinated
    - **Moderate** (≥0.4): Medium confidence, some correlation
    - **Weak** (≥0.1): Low confidence, may be coincidental
    - **None** (<0.1): No significant correlation
    
    **Requirements:**
    - Both identities must have appearance data
    - Need at least 3 activity sequences for meaningful correlation
    - Historical data: 90+ days recommended
    
    **Returns:**
    - Correlation score (0.0 to 1.0)
    - Correlation strength (strong/moderate/weak/none)
    - Number of activity sequences detected
    - List of sequences with camera pairs and time differences
    
    **Performance:** ~100-500ms per identity pair
    """,
    responses={
        200: {
            "description": "Correlation calculated successfully",
            "content": {
                "application/json": {
                    "example": {
                        "identity_a": "123e4567-e89b-12d3-a456-426614174000",
                        "identity_b": "456e7890-e89b-12d3-a456-426614174001",
                        "correlation_score": 0.75,
                        "correlation_strength": "strong",
                        "sequence_count": 15,
                        "sequences": [
                            {
                                "from_camera": "camera_1",
                                "to_camera": "camera_2",
                                "time_diff_minutes": 3.5,
                                "from_time": "2026-01-10T10:00:00Z",
                                "to_time": "2026-01-10T10:03:30Z"
                            }
                        ]
                    }
                }
            }
        },
        400: {"description": "Invalid parameters"},
        500: {"description": "Server error"}
    }
)
async def calculate_activity_correlation(
    identity_a: str = Query(..., description="First identity UUID", example="123e4567-e89b-12d3-a456-426614174000"),
    identity_b: str = Query(..., description="Second identity UUID", example="456e7890-e89b-12d3-a456-426614174001"),
    days_back: int = Query(default=90, ge=1, le=365, description="Days of historical data to analyze (1-365)", example=90),
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_admin())
,
    _rl: None = Depends(rate_limited("correlation", heavy=True))):
    """Calculate activity association between two identities.

    Correlation measures temporal/spatial association only — it does not
    prove causation, and the response says so explicitly.
    """
    await _get_identity_or_404(db, identity_a)
    await _get_identity_or_404(db, identity_b)
    try:
        correlation_score, sequences, correlation_meta = await _bounded_intel_call(
            "correlation",
            activity_correlation_analyzer.calculate_correlation(
                db=db,
                identity_a=identity_a,
                identity_b=identity_b,
                days_back=days_back
            ))

        # Determine correlation strength
        if correlation_score >= 0.7:
            strength = "strong"
        elif correlation_score >= 0.4:
            strength = "moderate"
        elif correlation_score >= 0.1:
            strength = "weak"
        else:
            strength = "none"

        _audit("activity_correlation", current_user, identity_a,
               identity_b=identity_b, sequence_count=len(sequences), days_back=days_back)
        return ActivityCorrelationResponse(
            identity_a=identity_a,
            identity_b=identity_b,
            correlation_score=float(correlation_score),
            correlation_strength=strength,
            sequence_count=len(sequences),
            sequences=[
                ActivitySequenceResponse(
                    from_camera=seq['from_camera'],
                    to_camera=seq['to_camera'],
                    time_diff_minutes=seq['time_diff_minutes'],
                    from_time=_iso_z(seq['from_time']),
                    to_time=_iso_z(seq['to_time'])
                )
                for seq in sequences[:20]  # Limit to first 20 sequences
            ],
            days_back=days_back,
            insufficient_evidence=len(sequences) < CORRELATION_MIN_SEQUENCES,
            algorithm_version=CORRELATION_ALGORITHM_VERSION,
            note=CORRELATION_NOTE,
            truncated=bool(correlation_meta.get("truncated", False))
        )

    except HTTPException:
        raise
    except Exception as e:
        raise _safe_500("activity correlation", e)


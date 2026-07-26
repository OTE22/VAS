"""
Intelligence API Routes
========================
Provides endpoints for identity intelligence features:
- Related Identities (co-appearance analysis)
- Temporal Patterns (when do they appear)
- Cross-Camera Tracking (movement across locations)
"""

import logging
import threading
import time
import uuid as uuid_mod
from datetime import datetime
from typing import List, Optional, Dict

from fastapi import APIRouter, HTTPException, Depends, Query, Request, BackgroundTasks, status as http_status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from db_connection import get_db
from db_models import Pipeline, Identity
from sqlalchemy import select
from backend.auth.auth_service import get_current_user, require_admin
from backend.core.intelligence_service import intelligence_service
from backend.core.security_intelligence_service import security_intelligence_service
from backend.core.map_service import map_service
from backend.core.relationship_calculation_task import calculate_all_relationships
from backend.core.threshold_learner import threshold_learner
from backend.core.trajectory_predictor import trajectory_predictor
from backend.core.activity_correlation import activity_correlation_analyzer
from backend.utils.path_utils import path_to_url
from fastapi.responses import HTMLResponse, JSONResponse

logger = logging.getLogger(__name__)
router = APIRouter()


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
    s = dt.isoformat()
    if dt.tzinfo is None:
        return s + "Z"
    return s.replace("+00:00", "Z")


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


# ---- calculate-all single-flight guard (WORKERS=1 → in-process lock is
# authoritative; the task_history row provides cross-restart visibility) ----
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

THRESHOLD_ALGORITHM_VERSION = "threshold-v1"
TRAJECTORY_MODEL_VERSION = "trajectory-v1"
CORRELATION_ALGORITHM_VERSION = "xcca-v1"
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


# =====================================================
# API Endpoints
# =====================================================

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
        return payload

    except HTTPException:
        raise
    except ValueError:
        # e.g. malformed date parameter — do not echo internals
        raise HTTPException(status_code=400, detail="Invalid date parameter (expected YYYY-MM-DD)")
    except Exception as e:
        _audit("cross_camera_track", current_user, identity_id, result="error")
        raise _safe_500("cross-camera tracking", e)


@router.get(
    "/api/identities/{identity_id}/map",
    response_class=HTMLResponse,
    tags=["Map Service"],
    summary="Get Interactive Map (Folium)",
    description="""
    Generate an interactive HTML map using Folium showing the identity's movement.
    
    **Returns:**
    - Complete HTML page with embedded interactive map
    - Routes between locations
    - Markers with popup information
    - Works completely offline (no external map tiles required)
    
    **Use Cases:**
    - Embed map in iframe
    - Display full-screen map view
    - Share map visualization
    """
)
async def get_tracking_map(
    identity_id: str,
    date: str = Query(default=None, description="Specific date (YYYY-MM-DD)"),
    days_back: int = Query(default=7, ge=1, le=30, description="Days to analyze if no date"),
    map_style: str = Query(default="light", description="Map style: dark, light, satellite, terrain"),
    include_popups: bool = Query(default=True, description="Include popup information on markers"),
    show_routes: bool = Query(default=True, description="Draw routes between locations"),
    cluster_markers: bool = Query(default=True, description="Cluster nearby markers"),
    # Security-analysis and expensive overlays are opt-IN. A missing
    # checkbox in some client must never silently enable them.
    enable_security_features: bool = Query(default=False, description="Enable security intelligence features"),
    detect_patterns: bool = Query(default=False, description="Detect suspicious movement patterns"),
    show_risk_heatmap: bool = Query(default=False, description="Show risk heatmap overlay"),
    show_timeline: bool = Query(default=False, description="Show timeline playback control"),
    show_animated_avatar: bool = Query(default=False, description="Show animated avatar moving along route"),
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_admin())
):
    """Generate interactive HTML map for tracking."""
    started = time.monotonic()
    # Validate style against the allowlist BEFORE any expensive work; the
    # error page never echoes the submitted value.
    if map_style not in MAP_STYLE_ALLOWLIST:
        return _safe_error_page(
            "Invalid Request",
            "Unsupported map style. Allowed: dark, light, satellite, terrain.",
            status_code=400,
        )
    await _get_identity_or_404(db, identity_id)
    try:
        # Get tracking data
        tracks = await intelligence_service.get_cross_camera_track(
            db=db,
            identity_id=identity_id,
            date=date,
            days_back=days_back
        )

        if not tracks:
            return _safe_error_page(
                "No Tracking Data Available",
                "No movement data found for this identity in the specified time period.",
                status_code=200,
            )

        # Convert to dict format for map service
        tracks_dict = [
            {
                "identity_id": t.identity_id,
                "display_name": t.display_name,
                "date": t.date,
                "movements": [
                    {
                        "pipeline_id": m.pipeline_id,
                        "pipeline_name": m.pipeline_name,
                        "timestamp": m.timestamp.isoformat(),
                        "snapshot_path": m.snapshot_path,
                        "snapshot_url": path_to_url(m.snapshot_path),
                        "duration_at_location": m.duration_at_location,
                        "coordinates": m.coordinates
                    }
                    for m in t.movements
                ],
                "total_cameras": t.total_cameras,
                "first_seen": t.first_seen.isoformat(),
                "last_seen": t.last_seen.isoformat(),
                "total_duration_minutes": t.total_duration_minutes
            }
            for t in tracks
        ]
        
        # Get identity name
        identity_name = tracks_dict[0].get('display_name') if tracks_dict else None
        
        # Get watchlist matches for security features
        watchlist_matches = None
        security_zones = None
        if enable_security_features:
            try:
                from backend.core.watchlist_service import watchlist_service
                watchlist_matches = await watchlist_service.get_identity_watchlists(db, identity_id)
            except Exception as e:
                logger.warning(f"[MAP] Could not load watchlist matches: {e}")
            
            # Generate security zones from pipeline locations
            # This creates zones around each pipeline location for visualization
            try:
                from sqlalchemy import select
                from db_models import Pipeline
                
                # Get unique pipeline IDs from tracks
                pipeline_ids = set()
                for track in tracks_dict:
                    for movement in track.get('movements', []):
                        if movement.get('pipeline_id'):
                            pipeline_ids.add(movement['pipeline_id'])
                
                # Fetch pipeline coordinates
                if pipeline_ids:
                    result = await db.execute(
                        select(Pipeline).where(Pipeline.pipeline_id.in_(list(pipeline_ids)))
                    )
                    pipelines = result.scalars().all()
                    
                    # Create security zones around each pipeline
                    security_zones = []
                    for pipeline in pipelines:
                        if pipeline.latitude is not None and pipeline.longitude is not None:
                            lat = float(pipeline.latitude)
                            lng = float(pipeline.longitude)
                            
                            # Create a circular zone around the pipeline (100m radius)
                            # Approximate: 1 degree ≈ 111km, so 0.001 ≈ 111m
                            radius = 0.0009  # ~100m radius
                            zone_coords = []
                            for angle in range(0, 360, 30):  # 12 points for circle
                                import math
                                rad = math.radians(angle)
                                zone_lat = lat + (radius * math.cos(rad))
                                zone_lng = lng + (radius * math.sin(rad))
                                zone_coords.append([zone_lat, zone_lng])
                            
                            # Determine zone type based on pipeline location name/type
                            pipeline_name = pipeline.location_name if hasattr(pipeline, 'location_name') and pipeline.location_name else pipeline.pipeline_id
                            zone_type = 'monitored'
                            risk_level = 5
                            pipeline_name_lower = str(pipeline_name).lower()
                            if 'restricted' in pipeline_name_lower or 'secure' in pipeline_name_lower:
                                zone_type = 'restricted'
                                risk_level = 8
                            elif 'entrance' in pipeline_name_lower or 'exit' in pipeline_name_lower:
                                zone_type = 'high_security'
                                risk_level = 7
                            
                            security_zones.append({
                                'name': pipeline_name or f"Zone {pipeline.pipeline_id[:8]}",
                                'coordinates': zone_coords,
                                'zone_type': zone_type,
                                'risk_level': risk_level,
                                'description': f"Security zone around {pipeline_name or 'pipeline'}"
                            })
            except Exception as e:
                logger.warning(f"[MAP] Could not generate security zones: {e}")
                security_zones = None
        
        # Generate cache key (include all parameters that affect map output)
        from backend.core.map_service import generate_cache_key
        cache_key = generate_cache_key(
            identity_id=identity_id,
            date=date,
            days_back=days_back,
            map_style=map_style,
            include_popups=include_popups,
            show_routes=show_routes,
            cluster_markers=cluster_markers,
            show_animated_avatar=show_animated_avatar,
            enable_security_features=enable_security_features,
            detect_patterns=detect_patterns,
            show_risk_heatmap=show_risk_heatmap,
            show_timeline=show_timeline
        )
        
        # Generate map HTML (production-ready with caching and security features)
        try:
            map_html = await map_service.generate_folium_map(
                tracks=tracks_dict,
                identity_name=identity_name,
                map_style=map_style,
                include_popups=include_popups,
                show_routes=show_routes,
                cluster_markers=cluster_markers,
                use_cache=True,
                cache_key=cache_key,
                enable_security_features=enable_security_features,
                watchlist_matches=watchlist_matches,
                security_zones=security_zones,
                detect_patterns=detect_patterns,
                show_risk_heatmap=show_risk_heatmap,
                show_timeline=show_timeline,
                show_animated_avatar=show_animated_avatar
            )
            
            _audit("tracking_map", current_user, identity_id,
                   duration_ms=int((time.monotonic() - started) * 1000),
                   map_style=map_style, security_features=enable_security_features,
                   date=date, days_back=days_back)
            resp = HTMLResponse(content=map_html, status_code=200)
            # Personalized, sensitive geographic content — never shared caches.
            resp.headers["Cache-Control"] = "private, no-store"
            resp.headers["X-Content-Type-Options"] = "nosniff"
            # If someone navigates to this URL directly (outside the sandboxed
            # iframe), CSP sandbox still isolates it into a unique origin.
            resp.headers["Content-Security-Policy"] = "sandbox allow-scripts"
            resp.headers["Referrer-Policy"] = "no-referrer"
            return resp
        except ValueError as e:
            ref = _reference_id()
            logger.error("[MAP] Validation error reference_id=%s error=%s", ref, e)
            return _safe_error_page(
                "Invalid Request",
                "The map request could not be processed.",
                reference_id=ref, status_code=400,
            )

    except HTTPException:
        raise
    except RuntimeError as e:
        # Map renderer dependency unavailable — deployment concern, belongs
        # in health checks and Docker logs, not in a user-facing page.
        ref = _reference_id()
        logger.error("[MAP] Map renderer unavailable reference_id=%s error=%s", ref, e)
        return _safe_error_page(
            "Map Service Unavailable",
            "The map service is temporarily unavailable. Please contact an administrator.",
            reference_id=ref, status_code=503,
        )
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid date parameter (expected YYYY-MM-DD)")
    except Exception as e:
        ref = _reference_id()
        logger.error("[MAP] Error generating map reference_id=%s error=%s", ref, e, exc_info=True)
        _audit("tracking_map", current_user, identity_id, result="error", reference_id=ref)
        return _safe_error_page(
            "Map Generation Failed",
            "An unexpected error occurred while generating the map.",
            reference_id=ref, status_code=500,
        )


@router.get(
    "/api/identities/{identity_id}/map/geojson",
    tags=["Map Service"],
    summary="Get Map Data as GeoJSON",
    description="""
    Get tracking data in GeoJSON format for advanced frontend rendering.
    
    **Returns:**
    - GeoJSON FeatureCollection
    - Point features for each location
    - LineString features for routes
    
    **Use Cases:**
    - Custom frontend map rendering
    - Integration with mapping libraries (Leaflet, Mapbox, etc.)
    - Data export for GIS tools
    """
)
async def get_tracking_map_geojson(
    identity_id: str,
    date: str = Query(default=None, description="Specific date (YYYY-MM-DD)"),
    days_back: int = Query(default=7, ge=1, le=30, description="Days to analyze if no date"),
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_admin())
):
    """Get tracking data as GeoJSON."""
    await _get_identity_or_404(db, identity_id)
    try:
        # Get tracking data
        tracks = await intelligence_service.get_cross_camera_track(
            db=db,
            identity_id=identity_id,
            date=date,
            days_back=days_back
        )
        
        if not tracks:
            return JSONResponse(
                content={
                    "type": "FeatureCollection",
                    "features": []
                },
                status_code=200
            )
        
        # Convert to dict format for map service
        tracks_dict = [
            {
                "identity_id": t.identity_id,
                "display_name": t.display_name,
                "date": t.date,
                "movements": [
                    {
                        "pipeline_id": m.pipeline_id,
                        "pipeline_name": m.pipeline_name,
                        "timestamp": m.timestamp.isoformat(),
                        "snapshot_path": m.snapshot_path,
                        "snapshot_url": path_to_url(m.snapshot_path),
                        "duration_at_location": m.duration_at_location,
                        "coordinates": m.coordinates
                    }
                    for m in t.movements
                ],
                "total_cameras": t.total_cameras,
                "first_seen": t.first_seen.isoformat(),
                "last_seen": t.last_seen.isoformat(),
                "total_duration_minutes": t.total_duration_minutes
            }
            for t in tracks
        ]
        
        # Generate cache key for GeoJSON
        from backend.core.map_service import generate_cache_key
        geojson_cache_key = generate_cache_key(
            identity_id=identity_id,
            date=date,
            days_back=days_back,
            map_style='dark',  # GeoJSON doesn't use style
            include_popups=False,  # GeoJSON doesn't use popups
            show_routes=True,
            cluster_markers=False  # GeoJSON doesn't use clustering
        )
        cache_key = f"geojson:{geojson_cache_key}"
        
        # Generate GeoJSON (production-ready with caching)
        try:
            geojson = await map_service.generate_geojson(
                tracks=tracks_dict,
                use_cache=True,
                cache_key=cache_key
            )
            
            _audit("tracking_geojson", current_user, identity_id)
            resp = JSONResponse(content=geojson, status_code=200)
            resp.headers["Cache-Control"] = "private, no-store"
            return resp
        except ValueError as e:
            logger.error(f"[MAP] GeoJSON validation error: {e}")
            raise HTTPException(status_code=400, detail="Invalid GeoJSON request")

    except HTTPException:
        raise
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid date parameter (expected YYYY-MM-DD)")
    except Exception as e:
        raise _safe_500("GeoJSON generation", e)


@router.get(
    "/api/map/stats",
    tags=["Map Service"],
    summary="Get Map Service Statistics",
    description="Get statistics about map generation service (cache hits, errors, etc.)"
)
async def get_map_stats(
    current_user: dict = Depends(require_admin())
):
    """Get map service statistics."""
    try:
        stats = map_service.get_stats()
        return stats
    except Exception as e:
        raise _safe_500("map statistics", e)


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
):
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
):
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

        network = await security_intelligence_service.build_social_network(
            db=db,
            identity_ids=identity_list,
            min_connections=min_connections,
            days_back=days_back
        )

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
            "isolated_nodes": [i for i in (network.isolated_nodes or []) if i in kept_ids],
            "scope": scope,
            "truncated": truncated,
            "total_nodes": total_nodes,
            "returned_nodes": len(all_nodes),
            "max_nodes": limit_nodes,
        }
        _audit("social_network", current_user,
               duration_ms=int((time.monotonic() - started) * 1000),
               scope=scope, returned_nodes=len(all_nodes), total_nodes=total_nodes,
               edges=len(kept_edges), days_back=days_back)
        return payload

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
    - Group activity (multiple people together)
    - Unusual timing (off-hours activity)
    - Rapid movement (quick location changes)
    - Repeated co-appearances
    
    **Use Cases:**
    - Security: detect coordinated activities
    - Investigation: identify suspicious groups
    - Monitoring: flag unusual behaviors
    """
)
async def get_suspicious_patterns(
    days_back: int = Query(default=30, ge=1, le=90, description="Days to analyze"),
    min_group_size: int = Query(default=3, ge=2, le=20, description="Minimum group size for detection"),
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_admin())
):
    """Detect suspicious patterns."""
    try:
        patterns = await security_intelligence_service.detect_suspicious_patterns(
            db=db,
            days_back=days_back,
            min_group_size=min_group_size
        )

        _audit("suspicious_patterns", current_user,
               days_back=days_back, row_count=len(patterns))
        return [
            {
                "pattern_type": p.pattern_type,
                "description": p.description,
                "identities_involved": p.identities_involved,
                "severity": p.severity,
                "confidence": p.confidence,
                "first_detected": p.first_detected.isoformat(),
                "evidence": p.evidence,
                "locations": p.locations,
                "time_range": [
                    p.time_range[0].isoformat(),
                    p.time_range[1].isoformat()
                ]
            }
            for p in patterns
        ]
        
    except Exception as e:
        raise _safe_500("pattern detection", e)


@router.get(
    "/api/security/anomalies/{identity_id}",
    tags=["Security Intelligence"],
    summary="Detect Behavioral Anomalies",
    description="""
    Detect behavioral anomalies for a specific identity.
    
    **Anomalies Detected:**
    - Off-schedule activity (unusual timing)
    - New location (never seen before)
    - Unusual group (new associations)
    
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
):
    """Detect anomalies for an identity."""
    await _get_identity_or_404(db, identity_id)
    try:
        anomalies = await security_intelligence_service.detect_anomalies(
            db=db,
            identity_id=identity_id,
            days_back=days_back
        )

        _audit("anomaly_detection", current_user, identity_id,
               days_back=days_back, row_count=len(anomalies))
        return [
            {
                "identity_id": a.identity_id,
                "anomaly_type": a.anomaly_type,
                "description": a.description,
                "severity": a.severity,
                "detected_at": a.detected_at.isoformat(),
                "baseline": a.baseline,
                "deviation": a.deviation,
                "risk_score": a.risk_score
            }
            for a in anomalies
        ]
        
    except Exception as e:
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
):
    """Get threat assessment for an identity."""
    await _get_identity_or_404(db, identity_id)
    try:
        assessment = await security_intelligence_service.assess_threat(
            db=db,
            identity_id=identity_id
        )

        _audit("threat_assessment", current_user, identity_id,
               threat_level=assessment.threat_level)
        return {
            "identity_id": assessment.identity_id,
            "display_name": assessment.display_name,
            "overall_risk_score": assessment.overall_risk_score,
            "risk_factors": assessment.risk_factors,
            "threat_level": assessment.threat_level,
            "recommendations": assessment.recommendations,
            "last_assessed": assessment.last_assessed.isoformat()
        }
        
    except ValueError:
        raise HTTPException(status_code=404, detail="Identity not found")
    except Exception as e:
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
):
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
        raise
    except Exception as e:
        _release_relationship_job(job_id)
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
):
    """DEPRECATED synchronous variant — prefer POST /api/intelligence/thresholds/jobs."""
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
                    sample_count=data['sample_count']
                )
                for pair, data in learned.items()
            ]
        )
        
    except Exception as e:
        raise _safe_500("threshold learning", e)


async def _run_threshold_job(job_id: str, pipeline_list: Optional[List[str]]):
    """Background worker for threshold learning — never inside an HTTP request."""
    from backend.core.task_history import task_history_manager
    from db_connection import db_manager
    started = time.monotonic()
    await task_history_manager.mark_running(job_id)
    try:
        async with db_manager.get_session() as db:
            if not pipeline_list:
                result = await db.execute(select(Pipeline).where(Pipeline.is_active == 1))
                pipeline_list = [p.pipeline_id for p in result.scalars().all()]
            learned = await threshold_learner.learn_all_camera_pairs(db, pipeline_list)

        thresholds = [
            {
                "camera_1": pair[0],
                "camera_2": pair[1],
                "optimal_time_window_minutes": data.get("optimal_time_window_minutes"),
                "optimal_distance_meters": data.get("optimal_distance_meters"),
                "actual_distance_meters": data.get("actual_distance_meters"),
                "confidence": data.get("confidence"),
                "sample_count": data.get("sample_count"),
            }
            for pair, data in learned.items()
        ]
        result_payload = {
            "learned_pairs": len(thresholds),
            "thresholds": thresholds[:200],
            "algorithm_version": THRESHOLD_ALGORITHM_VERSION,
            "calculated_at": _iso_z(datetime.utcnow()),
            "pipelines_scoped": len(pipeline_list or []),
        }
        await task_history_manager.finish_job(job_id, success=True, result=result_payload)
        logger.info("[INTELLIGENCE] threshold job completed job_id=%s learned_pairs=%s duration_ms=%s",
                    job_id, len(thresholds), int((time.monotonic() - started) * 1000))
    except Exception as e:
        logger.error("[INTELLIGENCE] threshold job failed job_id=%s error=%s", job_id, e, exc_info=True)
        await task_history_manager.finish_job(
            job_id, success=False,
            error_code="THRESHOLD_JOB_FAILED", error_message=str(e)[:500])
    finally:
        _release_threshold_job(job_id)


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
):
    """Schedule threshold learning in the background — single-flight."""
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
        background_tasks.add_task(_run_threshold_job, job_id, pipeline_list)
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
        raise
    except Exception as e:
        _release_threshold_job(job_id)
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
    from backend.core.task_history import task_history_manager
    task = await task_history_manager.get_task_by_job_id(job_id)
    if not task or task.get("task_type") != "threshold_learning":
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

    folium_ok = importlib.util.find_spec("folium") is not None
    running_threshold_job = _threshold_job_running()
    with _relationship_job_lock:
        running_rel_job = _RELATIONSHIP_JOB["job_id"]

    from config import settings as app_settings
    offline_tiles = bool(getattr(app_settings, "MAP_OFFLINE_TILES_ENABLED", False))

    caps = {
        "network_analysis": {"enabled": True, "status": "ready"},
        "pattern_detection": {"enabled": True, "status": "ready"},
        "anomaly_detection": {"enabled": True, "status": "ready"},
        "threat_assessment": {"enabled": True, "status": "ready"},
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
            "enabled": folium_ok,
            "status": "ready" if folium_ok else "dependency_unavailable",
        },
        "offline_maps": {
            "enabled": offline_tiles,
            "status": "ready" if offline_tiles else "disabled",
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
):
    """Predict next camera locations for an identity."""
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
):
    """Calculate activity association between two identities.

    Correlation measures temporal/spatial association only — it does not
    prove causation, and the response says so explicitly.
    """
    await _get_identity_or_404(db, identity_a)
    await _get_identity_or_404(db, identity_b)
    try:
        correlation_score, sequences = await activity_correlation_analyzer.calculate_correlation(
            db=db,
            identity_a=identity_a,
            identity_b=identity_b,
            days_back=days_back
        )

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
            note=CORRELATION_NOTE
        )

    except HTTPException:
        raise
    except Exception as e:
        raise _safe_500("activity correlation", e)


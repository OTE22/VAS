"""
Advanced Search API Routes
===========================
Production-grade face search with multi-face detection, quality scoring,
watchlist checking, and search history.
"""

import logging
import io
from datetime import datetime
from typing import List, Optional
from uuid import UUID

import cv2
import hashlib
import os
import numpy as np

# Module level: the route body needs this, and the only other import of
# this module is function-local inside get_advanced_search_service().
from backend.core.advanced_search import _match_log
from fastapi import APIRouter, HTTPException, Depends, UploadFile, File, Form, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from db_connection import get_db
from backend.auth.auth_service import get_current_user, require_admin
from backend.core.face_extraction import (FaceExtractionError, error_payload,
                                          extract_single_face)
from config import settings

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Search"])


def require_search_csrf(request: Request):
    """CSRF defense-in-depth for cookie-authenticated mutating requests.

    Every sibling router that mutates state already carries this check
    (watchlists, live_alerts, intelligence, conversations, identities); the
    search routers were the outlier. SameSite=lax on the auth cookie blocks
    cross-site POSTs in modern browsers, but AUTH_COOKIE_SAMESITE is
    env-configurable and these are multipart endpoints, which a cross-site
    form can submit directly. A cross-site page cannot attach a custom header
    without a CORS preflight.

    Bearer-token clients (curl, tests) are exempt: a token cannot be sent
    cross-site by the browser on its own.
    """
    if request.headers.get("authorization"):
        return
    if request.headers.get("x-requested-with", "").lower() != "xmlhttprequest":
        raise HTTPException(
            status_code=403,
            detail="CSRF check failed: X-Requested-With header required",
        )

# Function to get advanced_search_service dynamically (handles initialization timing)
def get_advanced_search_service():
    """Get advanced_search_service instance - handles import timing issues."""
    # Try multiple ways to get the service
    service = None
    
    # Method 1: Direct import
    try:
        from backend.core.advanced_search import advanced_search_service
        service = advanced_search_service
    except (ImportError, AttributeError) as e:
        logger.debug(f"Direct import failed: {e}")
    
    # Method 2: From sys.modules (if already loaded)
    if not service:
        try:
            import sys
            adv_module = sys.modules.get('backend.core.advanced_search')
            if adv_module:
                service = getattr(adv_module, 'advanced_search_service', None)
        except Exception as e:
            logger.debug(f"sys.modules access failed: {e}")
    
    # Method 3: From backend.core module
    if not service:
        try:
            import sys
            core_module = sys.modules.get('backend.core')
            if core_module:
                service = getattr(core_module, 'advanced_search_service', None)
        except Exception as e:
            logger.debug(f"core module access failed: {e}")
    
    if service:
        logger.debug(f"✅ Retrieved advanced_search_service: {type(service).__name__}, initialized={getattr(service, 'is_initialized', False)}")
        return service
    else:
        logger.error("❌ advanced_search_service not available from any source")
        return None


# =====================================================
# Request/Response Models
# =====================================================

class QualityDetails(BaseModel):
    score: float
    issue: Optional[str] = None
    recommendation: Optional[str] = None


class QualityAssessmentResponse(BaseModel):
    overall_score: float
    band: str
    details: dict
    warnings: List[str]
    proceed_recommendation: bool


class WatchlistMatchResponse(BaseModel):
    watchlist_id: str
    list_name: str
    alert_level: str
    color: Optional[str] = None
    icon: Optional[str] = None
    priority: str
    notes: Optional[str] = None
    action_instructions: Optional[str] = None


class FaceMatchResponse(BaseModel):
    identity_id: str
    display_name: Optional[str] = None
    type: str
    similarity: float
    confidence_band: str
    best_snapshot_path: Optional[str] = None  # Original path for reference
    snapshot_url: Optional[str] = None  # Backend provides ready-to-use URL
    last_seen_at: Optional[str] = None
    appearances_count: int = 0
    watchlist_match: Optional[WatchlistMatchResponse] = None


class FaceInImageResponse(BaseModel):
    face_index: int
    bounding_box: dict
    quality_score: float
    quality_details: dict
    quality_warning: Optional[str] = None
    skipped: bool = False
    skip_reason: Optional[str] = None
    matches: List[FaceMatchResponse] = []


class WatchlistAlertResponse(BaseModel):
    face_index: int
    identity_id: str
    identity_name: Optional[str] = None
    watchlist_id: str
    list_name: str
    alert_level: str
    priority: str
    notes: Optional[str] = None
    action_instructions: Optional[str] = None
    similarity: float


class SearchSummary(BaseModel):
    total_faces_detected: int
    faces_searchable: int
    faces_skipped: int
    total_matches: int
    unique_identities_found: int
    known_matches: int
    unknown_matches: int
    watchlist_alerts: int


class MultiSearchResponse(BaseModel):
    search_id: str
    image_info: dict
    faces: List[FaceInImageResponse]
    watchlist_alerts: List[WatchlistAlertResponse]
    processing_time_ms: int
    summary: SearchSummary


class ConfidenceBandConfig(BaseModel):
    very_high_min: float
    high_min: float
    medium_min: float
    low_min: float


# =====================================================
# API Endpoints
# =====================================================

@router.post(
    "/api/search/advanced",
    response_model=MultiSearchResponse,
    summary="Advanced Multi-Face Search",
    description="""
    Search for all faces in an uploaded image with advanced features:
    - Multi-face detection (finds all faces in image)
    - Quality scoring (blur, lighting, angle, size)
    - Watchlist checking (VIP, Threat, POI)
    - Confidence bands (Very High, High, Medium, Low)
    - Search history logging
    
    **Quality Thresholds:**
    - Faces below min_quality are skipped
    - Warning shown for faces below warning threshold
    
    **Scopes:**
    - `known`: Search only identified people
    - `unknown`: Search only unidentified faces
    - `both`: Search everything (recommended)
    """
)
async def advanced_search(
    request: Request,
    image: UploadFile = File(..., description="Image file (JPG, PNG, WEBP)"),
    scope: str = Form(default="both", description="Search scope: known, unknown, or both"),
    # No literal default/ceiling here: a Form(default=..., le=...) is evaluated
    # once at import, so it would freeze SEARCH_DEFAULT_TOP_K / SEARCH_MAX_TOP_K
    # at whatever they were when the module loaded and ignore every later edit.
    # Resolved and bounds-checked below instead.
    top_k: int = Form(default=None, ge=1, description="Max results per face"),
    min_quality: float = Form(default=None, ge=0, le=1, description="Minimum quality (0-1)"),
    check_watchlist: bool = Form(default=True, description="Check against watchlists"),
    exclude_identity_ids: str = Form(default=None, description="Comma-separated identity IDs to exclude"),
    exclude_watchlist_ids: str = Form(default=None, description="Comma-separated watchlist IDs to exclude"),
    date_from: str = Form(default=None, description="Filter: ISO date string"),
    date_to: str = Form(default=None, description="Filter: ISO date string"),
    pipeline_id: str = Form(default=None, description="Filter by pipeline"),
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_admin()),
    _csrf: None = Depends(require_search_csrf)
):
    """
    Perform advanced multi-face search with quality scoring and watchlist checking.
    """
    # Get service instance dynamically
    service = get_advanced_search_service()
    if not service:
        raise HTTPException(
            status_code=503,
            detail="Advanced search service not available. Please check server logs."
        )
    if not service.is_initialized:
        raise HTTPException(
            status_code=503,
            detail="Advanced search service not initialized. Please wait for system startup."
        )
    
    # Result depth: configured default when the caller omits it, configured
    # ceiling when the caller overreaches.
    max_top_k = int(settings.SEARCH_MAX_TOP_K)
    if top_k is None:
        top_k = int(settings.SEARCH_DEFAULT_TOP_K)
    elif top_k > max_top_k:
        raise HTTPException(
            status_code=422,
            detail=f"top_k must not exceed the configured maximum of {max_top_k}")

    # Validate file type
    if not image.content_type or not image.content_type.startswith('image/'):
        raise HTTPException(status_code=400, detail="File must be an image")

    try:
        # The correlation id the HTTP middleware already minted for this
        # request (backend/main.py). Read here rather than generated, so a
        # [UPLOAD_MATCH] trace joins up with the access log and the
        # X-Request-ID header the caller got back.
        request_id = getattr(getattr(request, "state", None), "request_id", None)

        # Read image
        contents = await image.read()
        _match_log("file_validated", request_id=request_id,
                   filename=os.path.basename(image.filename or "") or "-",
                   content_type=image.content_type,
                   file_size=len(contents),
                   checksum_prefix=hashlib.sha256(contents).hexdigest()[:12])
        nparr = np.frombuffer(contents, np.uint8)
        img = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
        
        if img is None:
            raise HTTPException(status_code=400, detail="Could not decode image")
        
        # Parse exclude lists. Excluding identities or watchlists from a search
        # IS the negative-search feature, so NEGATIVE_SEARCH_ENABLED gates it —
        # the flag was declared, described and rendered as a switch that read
        # nothing.
        negative_search_on = bool(settings.NEGATIVE_SEARCH_ENABLED)
        if not negative_search_on and (exclude_identity_ids or exclude_watchlist_ids):
            raise HTTPException(
                status_code=403,
                detail="Negative search (exclusions) is disabled (NEGATIVE_SEARCH_ENABLED)")

        exclude_ids = None
        if exclude_identity_ids:
            exclude_ids = [i.strip() for i in exclude_identity_ids.split(',') if i.strip()]

        exclude_wl_ids = None
        if exclude_watchlist_ids:
            exclude_wl_ids = [i.strip() for i in exclude_watchlist_ids.split(',') if i.strip()]
        
        # Parse date filters
        filters = {}
        if date_from:
            try:
                filters['date_from'] = datetime.fromisoformat(date_from.replace('Z', '+00:00'))
            except ValueError:
                raise HTTPException(status_code=400, detail="Invalid date_from format")
        if date_to:
            try:
                filters['date_to'] = datetime.fromisoformat(date_to.replace('Z', '+00:00'))
            except ValueError:
                raise HTTPException(status_code=400, detail="Invalid date_to format")
        if pipeline_id:
            filters['pipeline_id'] = pipeline_id
        
        # Get client info
        ip_address = request.client.host if request.client else None
        user_agent = request.headers.get('user-agent')
        
        # Perform search
        result = await service.search_multi_face(
            request_id=request_id,
            image=img,
            db=db,
            user_id=current_user['id'],
            scope=scope,
            top_k=top_k,
            min_quality=min_quality,
            check_watchlist=check_watchlist,
            exclude_identity_ids=exclude_ids,
            exclude_watchlist_ids=exclude_wl_ids,
            filters=filters if filters else None,
            ip_address=ip_address,
            user_agent=user_agent,
            image_hash=hashlib.sha256(contents).hexdigest(),
        )
        
        # Convert to response format
        return service.to_dict(result)
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[ADVANCED_SEARCH] Error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Search failed: {str(e)}")


@router.get(
    "/api/search/config",
    summary="Get Search Configuration",
    description="Get current search configuration including quality thresholds and confidence bands."
)
async def get_search_config(
    current_user: dict = Depends(require_admin())
):
    """Get search configuration."""
    return {
        "quality": {
            "min_threshold": settings.SEARCH_MIN_QUALITY_THRESHOLD,
            "warning_threshold": settings.SEARCH_QUALITY_WARNING_THRESHOLD
        },
        "confidence_bands": {
            "VERY_HIGH": {"min": settings.CONFIDENCE_VERY_HIGH_MIN, "max": 1.0, "label": "Almost Certain"},
            "HIGH": {"min": settings.CONFIDENCE_HIGH_MIN, "max": settings.CONFIDENCE_VERY_HIGH_MIN, "label": "Strong Match"},
            "MEDIUM": {"min": settings.CONFIDENCE_MEDIUM_MIN, "max": settings.CONFIDENCE_HIGH_MIN, "label": "Possible Match"},
            "LOW": {"min": settings.CONFIDENCE_LOW_MIN, "max": settings.CONFIDENCE_MEDIUM_MIN, "label": "Weak Match"},
            "VERY_LOW": {"min": 0.0, "max": settings.CONFIDENCE_LOW_MIN, "label": "Unlikely Match"}
        },
        # Result depth, so the page's top_k control carries the server's
        # default and ceiling instead of its own. One block for both surfaces:
        # the single and batch searches ran at different depths from the same
        # control because each had its own literal default.
        "results": {
            "default_top_k": settings.SEARCH_DEFAULT_TOP_K,
            "max_top_k": settings.SEARCH_MAX_TOP_K
        },
        # The bar a match must clear to be shown at all. Distinct from the
        # retrieval floor, which is an internal recall knob.
        "display": {
            "min_similarity": settings.SIMILARITY_THRESHOLD
        },
        "batch": {
            "max_images": settings.BATCH_SEARCH_MAX_IMAGES,
            "timeout_seconds": settings.BATCH_SEARCH_TIMEOUT_SECONDS
        },
        # Upload limits the UI must mirror. Previously the search page
        # hardcoded its own numbers, which drifted from the enforced values
        # (it staged 100 images against a limit of 20 and only found out on
        # the 400 that failed the whole batch).
        "upload": {
            "max_file_size_bytes": settings.MAX_FILE_SIZE,
            "allowed_extensions": sorted(settings.allowed_image_extensions_list)
        },
        "history": {
            "retention_days": settings.SEARCH_HISTORY_RETENTION_DAYS,
            "max_per_user": settings.SEARCH_HISTORY_MAX_PER_USER
        }
    }


@router.post(
    "/api/search/quality-check",
    response_model=QualityAssessmentResponse,
    summary="Check Image Quality",
    description="Assess face image quality without performing search. Useful for validating images before batch operations."
)
async def check_image_quality(
    image: UploadFile = File(..., description="Image file to assess"),
    current_user: dict = Depends(require_admin()),
    _csrf: None = Depends(require_search_csrf)
):
    """Check image quality without searching."""
    from backend.core.face_quality import assess_face_quality
    
    # Get service instance dynamically
    service = get_advanced_search_service()
    if not service:
        raise HTTPException(
            status_code=503,
            detail="Advanced search service not available. Please check server logs."
        )
    if not service.is_initialized:
        raise HTTPException(
            status_code=503,
            detail="Service not initialized"
        )
    
    try:
        # Read image
        contents = await image.read()
        nparr = np.frombuffer(contents, np.uint8)
        img = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
        
        if img is None:
            raise HTTPException(status_code=400, detail="Could not decode image")
        
        # Detect the face through the shared extractor. What this replaces
        # synthesized five keypoints from image geometry when detection failed
        # on a small squarish image, and scored THOSE. Because the invented
        # points are symmetric about the image midpoint by construction,
        # _assess_angle computed yaw 0 and roll 0 every time — a constant
        # perfect score for 25% of the weighted total, for any small upload,
        # including one containing no face at all. The number it reported was
        # not a measurement.
        #
        # on_multiple="best" preserves the previous max_num=1 semantics exactly:
        # the largest face is assessed, group photos included.
        try:
            face = extract_single_face(img, on_multiple="best",
                                       manager=service.model_manager)
        except FaceExtractionError as exc:
            logger.info("[QUALITY_CHECK] refused: %s", exc.code)
            return JSONResponse(status_code=400, content=error_payload(exc))

        # Clamped at both ends: a padded-retry face can have a box reaching past
        # the original frame, and an unclamped slice would yield an empty crop.
        x1, y1, x2, y2 = face.bbox_int(img.shape)
        face_crop = img[y1:y2, x1:x2]

        quality = assess_face_quality(
            face_image=face_crop,
            bbox=(x1, y1, x2, y2),
            landmarks=face.landmarks,
            full_image=img
        )
        
        # Convert numpy types to Python native types for JSON serialization
        def convert_numpy_types(obj):
            """Recursively convert numpy types to Python native types."""
            if isinstance(obj, np.integer):
                return int(obj)
            elif isinstance(obj, np.floating):
                return float(obj)
            elif isinstance(obj, np.ndarray):
                return obj.tolist()
            elif isinstance(obj, dict):
                return {key: convert_numpy_types(value) for key, value in obj.items()}
            elif isinstance(obj, (list, tuple)):
                return [convert_numpy_types(item) for item in obj]
            return obj
        
        # Convert details dictionary
        converted_details = convert_numpy_types(quality.details)
        
        return {
            "overall_score": float(quality.overall_score),
            "band": quality.band,
            "details": converted_details,
            "warnings": quality.warnings,
            "proceed_recommendation": quality.proceed_recommendation
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[QUALITY_CHECK] Error: {e}")
        raise HTTPException(status_code=500, detail=str(e))



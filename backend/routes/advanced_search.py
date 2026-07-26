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
import numpy as np
from fastapi import APIRouter, HTTPException, Depends, UploadFile, File, Form, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from db_connection import get_db
from backend.auth.auth_service import get_current_user, require_admin
from config import settings

logger = logging.getLogger(__name__)

router = APIRouter()

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
    top_k: int = Form(default=10, ge=1, le=100, description="Max results per face"),
    min_quality: float = Form(default=None, ge=0, le=1, description="Minimum quality (0-1)"),
    check_watchlist: bool = Form(default=True, description="Check against watchlists"),
    exclude_identity_ids: str = Form(default=None, description="Comma-separated identity IDs to exclude"),
    exclude_watchlist_ids: str = Form(default=None, description="Comma-separated watchlist IDs to exclude"),
    date_from: str = Form(default=None, description="Filter: ISO date string"),
    date_to: str = Form(default=None, description="Filter: ISO date string"),
    pipeline_id: str = Form(default=None, description="Filter by pipeline"),
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_admin())
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
    
    # Validate file type
    if not image.content_type or not image.content_type.startswith('image/'):
        raise HTTPException(status_code=400, detail="File must be an image")
    
    try:
        # Read image
        contents = await image.read()
        nparr = np.frombuffer(contents, np.uint8)
        img = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
        
        if img is None:
            raise HTTPException(status_code=400, detail="Could not decode image")
        
        # Parse exclude lists
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
            user_agent=user_agent
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
        "batch": {
            "max_images": settings.BATCH_SEARCH_MAX_IMAGES,
            "timeout_seconds": settings.BATCH_SEARCH_TIMEOUT_SECONDS
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
    current_user: dict = Depends(require_admin())
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
        
        # Detect face
        bboxes, kpss = service.model_manager.detector.detect(img, max_num=1)
        
        # If no faces detected, check if this is a small pre-cropped face image
        if (bboxes is None or len(bboxes) == 0) or (kpss is None or len(kpss) == 0):
            h, w = img.shape[:2]
            # Common face crop sizes: 112x112, 128x128, 160x160, etc.
            # If image is small and roughly square, treat as face image
            is_small_image = (h <= 200 and w <= 200) and (abs(h - w) <= 20)
            
            if is_small_image:
                logger.info(f"[QUALITY_CHECK] No face detected, but image is small ({h}x{w}) - treating as pre-cropped face image")
                logger.info(f"[QUALITY_CHECK] Using entire image as face region for quality assessment")
                
                # Create approximate keypoints at image center (for face alignment)
                center_x, center_y = w // 2, h // 2
                kpss = np.array([[
                    [center_x - w * 0.15, center_y - h * 0.1],  # Left eye
                    [center_x + w * 0.15, center_y - h * 0.1],  # Right eye
                    [center_x, center_y],                         # Nose
                    [center_x - w * 0.1, center_y + h * 0.15],  # Left mouth corner
                    [center_x + w * 0.1, center_y + h * 0.15]   # Right mouth corner
                ]], dtype=np.float32)
                bboxes = np.array([[0, 0, w, h, 1.0]], dtype=np.float32)
                logger.info(f"[QUALITY_CHECK] Created approximate keypoints for pre-cropped face image")
            else:
                raise HTTPException(status_code=400, detail="No face detected in image")
        
        # Assess first face
        bbox = bboxes[0]
        kps = kpss[0] if kpss is not None and len(kpss) > 0 else None
        x1, y1, x2, y2 = map(int, bbox[:4])
        face_crop = img[max(0, y1):y2, max(0, x1):x2]
        
        quality = assess_face_quality(
            face_image=face_crop,
            bbox=(x1, y1, x2, y2),
            landmarks=kps,
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



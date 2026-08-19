"""
Batch Search & Export API Routes
=================================
Handle batch search operations and result exports.
"""

import logging
from datetime import datetime, timedelta
from typing import List, Optional

import cv2
import hashlib
import numpy as np
from fastapi import APIRouter, HTTPException, Depends, Query, UploadFile, File, Form, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, and_, delete

from db_connection import get_db
from backend.auth.auth_service import get_current_user, require_admin
from backend.core.batch_search_service import batch_search_service
# The search routers share one CSRF dependency; defined next to the primary
# search route rather than duplicated.
from backend.routes.advanced_search import require_search_csrf
from backend.core.export_service import export_service
from backend.core.rate_limiter import rate_limited
from db_models import SearchHistory
from config import settings
from backend.utils.time_utils import iso_utc, utc_now

logger = logging.getLogger(__name__)
router = APIRouter(tags=["Export"])


# =====================================================
# Response Models
# =====================================================

class BatchImageResultResponse(BaseModel):
    image_index: int
    image_name: str
    status: str
    error_message: Optional[str]
    faces_detected: int
    faces_searched: int
    matches: List[dict]
    quality_scores: List[float]
    processing_time_ms: int


class BatchSearchResponse(BaseModel):
    batch_id: str
    total_images: int
    successful_images: int
    failed_images: int
    total_faces_detected: int
    total_matches: int
    unique_identities: int
    watchlist_alerts: int
    results: List[BatchImageResultResponse]
    processing_time_ms: int


class SearchHistoryResponse(BaseModel):
    id: str
    search_type: str
    scope: Optional[str]
    input_faces_count: Optional[int]
    results_count: Optional[int]
    unique_identities_count: Optional[int]
    watchlist_alerts_count: int
    processing_time_ms: Optional[int]
    created_at: str


# =====================================================
# Batch Search Endpoints
# =====================================================

@router.post(
    "/api/search/batch",
    response_model=BatchSearchResponse,
    summary="Batch Face Search",
    description=f"""
    Search multiple images for faces in a single request.
    
    **Limits:**
    - Maximum {settings.BATCH_SEARCH_MAX_IMAGES} images per batch
    - Timeout: {settings.BATCH_SEARCH_TIMEOUT_SECONDS} seconds
    
    **Use Cases:**
    - Search through folder of images
    - Bulk identity verification
    - Forensic analysis of multiple sources
    
    **Processing:**
    - Images are processed in parallel (max 5 concurrent)
    - Results aggregated with unique identity tracking
    - Watchlist checked across all images
    """
)
async def batch_search(
    request: Request,
    images: List[UploadFile] = File(..., description="Image files to search"),
    scope: str = Form(default="both", description="Search scope: known, unknown, both"),
    # Resolved from SEARCH_DEFAULT_TOP_K/SEARCH_MAX_TOP_K below. The literal
    # default of 5 here ran a batch at half the depth of the single-image
    # search (10), from the same control on the search page.
    top_k: int = Form(default=None, ge=1, description="Results per face"),
    min_quality: float = Form(default=None, ge=0, le=1, description="Minimum quality threshold"),
    check_watchlist: bool = Form(default=True, description="Check against watchlists"),
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_admin()),
    _csrf: None = Depends(require_search_csrf)
,
    _rl: None = Depends(rate_limited("batch_search", heavy=True))):
    """Perform batch face search across multiple images."""
    if not settings.BATCH_SEARCH_ENABLED:
        raise HTTPException(
            status_code=403, detail="Batch search is disabled (BATCH_SEARCH_ENABLED)")

    max_top_k = int(settings.SEARCH_MAX_TOP_K)
    if top_k is None:
        top_k = int(settings.SEARCH_DEFAULT_TOP_K)
    elif top_k > max_top_k:
        raise HTTPException(
            status_code=422,
            detail=f"top_k must not exceed the configured maximum of {max_top_k}")

    if not batch_search_service.is_initialized:
        raise HTTPException(
            status_code=503,
            detail="Batch search service not initialized"
        )
    
    # Check image count
    if len(images) > settings.BATCH_SEARCH_MAX_IMAGES:
        raise HTTPException(
            status_code=400,
            detail=f"Maximum {settings.BATCH_SEARCH_MAX_IMAGES} images allowed per batch"
        )
    
    # Load images
    image_data = []
    for img_file in images:
        if not img_file.content_type or not img_file.content_type.startswith('image/'):
            continue
        
        try:
            contents = await img_file.read()
            nparr = np.frombuffer(contents, np.uint8)
            img = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
            
            if img is not None:
                image_data.append((img_file.filename, img, hashlib.sha256(contents).hexdigest()))
        except Exception as e:
            logger.warning(f"[BATCH_SEARCH] Failed to load {img_file.filename}: {e}")
    
    if not image_data:
        raise HTTPException(
            status_code=400,
            detail="No valid images provided"
        )
    
    # Get client info
    ip_address = request.client.host if request.client else None
    user_agent = request.headers.get('user-agent')
    
    try:
        result = await batch_search_service.search_batch(
            db=db,
            images=image_data,
            user_id=current_user['id'],
            scope=scope,
            top_k=top_k,
            min_quality=min_quality,
            check_watchlist=check_watchlist,
            ip_address=ip_address,
            user_agent=user_agent
        )
        
        return batch_search_service.to_dict(result)
        
    except Exception as e:
        logger.error(f"[BATCH_SEARCH] Error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


# =====================================================
# Export Endpoints
# =====================================================

@router.post(
    "/api/search/export",
    summary="Export Search Results",
    description="""
    Export search results to various formats.
    
    **Formats:**
    - `csv`: Spreadsheet-compatible format
    - `json`: Structured data format
    - `pdf`: Formatted report
    
    Pass the search results from `/api/search/advanced` response.
    """
)
async def export_search_results(
    results: dict,
    format: str = Query(default="csv", description="Export format: csv, json, pdf"),
    include_images: bool = Query(default=False, description="Include images (json only)"),
    current_user: dict = Depends(require_admin()),
    _csrf: None = Depends(require_search_csrf)
,
    _rl: None = Depends(rate_limited("export", heavy=True))):
    """Export search results."""
    if not settings.EXPORT_RESULTS_ENABLED:
        raise HTTPException(
            status_code=403, detail="Result export is disabled (EXPORT_RESULTS_ENABLED)")
    try:
        export = export_service.export_search_results(
            results=results,
            format=format,
            include_images=include_images
        )
        
        return Response(
            content=export.data,
            media_type=export.content_type,
            headers={
                'Content-Disposition': f'attachment; filename="{export.filename}"'
            }
        )
        
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"[EXPORT] Error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post(
    "/api/search/batch/export",
    summary="Export Batch Results",
    description="Export batch search results to CSV or JSON format."
)
async def export_batch_results(
    results: dict,
    format: str = Query(default="csv", description="Export format: csv, json"),
    current_user: dict = Depends(require_admin()),
    _csrf: None = Depends(require_search_csrf)
,
    _rl: None = Depends(rate_limited("export", heavy=True))):
    """Export batch search results."""
    try:
        export = export_service.export_batch_results(
            results=results,
            format=format
        )
        
        return Response(
            content=export.data,
            media_type=export.content_type,
            headers={
                'Content-Disposition': f'attachment; filename="{export.filename}"'
            }
        )
        
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"[EXPORT] Error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# =====================================================
# Search History Endpoints
# =====================================================

@router.get(
    "/api/search/history",
    response_model=List[SearchHistoryResponse],
    summary="Get Search History",
    description="Get search history for the current user."
)
async def get_search_history(
    days_back: int = Query(default=30, ge=1, le=365, description="Days to look back"),
    search_type: str = Query(default=None, description="Filter by type: single, multi, batch"),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_admin())
):
    """Get search history."""
    cutoff = datetime.utcnow() - timedelta(days=days_back)
    
    query = select(SearchHistory).where(
        and_(
            SearchHistory.user_id == current_user['id'],
            SearchHistory.created_at >= cutoff
        )
    )
    
    if search_type:
        from db_models import SearchType
        try:
            type_enum = SearchType(search_type)
            query = query.where(SearchHistory.search_type == type_enum)
        except ValueError:
            pass
    
    query = query.order_by(SearchHistory.created_at.desc())
    query = query.limit(limit).offset(offset)
    
    result = await db.execute(query)
    history = result.scalars().all()
    
    return [
        {
            "id": str(h.id),
            "search_type": h.search_type.value if h.search_type else None,
            "scope": h.scope,
            "input_faces_count": h.input_faces_count,
            "results_count": h.results_count,
            "unique_identities_count": h.unique_identities_count,
            "watchlist_alerts_count": h.watchlist_alerts_count,
            "processing_time_ms": h.processing_time_ms,
            "created_at": iso_utc(h.created_at)
        }
        for h in history
    ]


@router.delete(
    "/api/search/history",
    summary="Clear Search History",
    description="Delete the calling user's own search history."
)
async def clear_search_history(
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_admin()),
    _csrf: None = Depends(require_search_csrf)
):
    """Clear the caller's search history.

    Scoped to `user_id == current_user['id']`, exactly like the GET above: the
    page lists your own searches, so CLEAR removes your own searches and never
    another investigator's.

    The Clear button on /admin/search-history existed with no endpoint behind
    it — it popped a confirmation and then showed "Clear history feature
    requires backend endpoint", so the history stayed.
    """
    result = await db.execute(
        delete(SearchHistory).where(SearchHistory.user_id == current_user['id'])
    )
    await db.commit()
    deleted = result.rowcount or 0
    logger.info(f"[SEARCH_HISTORY] user={current_user['id']} cleared {deleted} row(s)")
    return {"deleted": deleted, "message": f"Cleared {deleted} search history entr"
                                           f"{'y' if deleted == 1 else 'ies'}"}


@router.get(
    "/api/search/history/export",
    summary="Export Search History",
    description="Export search history to CSV or JSON."
)
async def export_search_history(
    format: str = Query(default="csv", description="Export format: csv, json"),
    days_back: int = Query(default=30, ge=1, le=365),
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_admin())
):
    """Export search history."""
    cutoff = datetime.utcnow() - timedelta(days=days_back)
    
    query = select(SearchHistory).where(
        and_(
            SearchHistory.user_id == current_user['id'],
            SearchHistory.created_at >= cutoff
        )
    ).order_by(SearchHistory.created_at.desc())
    
    result = await db.execute(query)
    history = result.scalars().all()
    
    history_data = [
        {
            "id": str(h.id),
            "search_type": h.search_type.value if h.search_type else None,
            "scope": h.scope,
            "input_faces_count": h.input_faces_count,
            "results_count": h.results_count,
            "unique_identities_count": h.unique_identities_count,
            "watchlist_alerts_count": h.watchlist_alerts_count,
            "processing_time_ms": h.processing_time_ms,
            "created_at": iso_utc(h.created_at)
        }
        for h in history
    ]
    
    try:
        export = export_service.export_search_history(
            history=history_data,
            format=format
        )
        
        return Response(
            content=export.data,
            media_type=export.content_type,
            headers={
                'Content-Disposition': f'attachment; filename="{export.filename}"'
            }
        )
        
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"[EXPORT] Error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


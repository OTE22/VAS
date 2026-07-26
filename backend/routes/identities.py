"""
Identity Management Routes
==========================
Admin-only endpoints for managing unknown faces and identities.
"""

import os
import sys
import logging
import uuid
from typing import List, Optional
from datetime import datetime, timedelta
from fastapi import APIRouter, Depends, HTTPException, status, UploadFile, File, Form, Request, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, and_, or_
from sqlalchemy.orm import selectinload
from pydantic import BaseModel

# Add parent directory to path
parent_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if parent_dir not in sys.path:
    sys.path.insert(0, parent_dir)

from db_connection import get_db
from db_models import (
    User, Identity, IdentityAppearance, IdentityEmbedding, Face, Detection,
    IdentityType, IdentityStatus, LabelState
)
from backend.auth.auth_service import get_current_user, require_role
from backend.core import model_manager
from backend.utils.identity_audit import IdentityAuditLogger, get_client_info
from fastapi import Request
from config import settings

# Helper function to find best image from storage/pipeline_id/person_name/ structure
async def _find_best_image_from_storage_for_identity(identity: Identity, storage_dir: str, db: AsyncSession) -> Optional[str]:
    """
    Find the best quality image from storage/pipeline_id/person_name/ structure.
    Searches all pipelines for this identity's name and returns the best quality image.
    """
    try:
        from db_models import Face, Detection, IdentityEmbedding
        
        storage_dir_abs = os.path.abspath(storage_dir)
        person_name = identity.display_name or "unknown"
        safe_name = "".join(c for c in person_name if c.isalnum() or c in ('-', '_')).lower()
        
        logger.info(f"[IDENTITIES] Searching for images for identity {identity.id} ('{person_name}', safe_name: '{safe_name}') in storage: {storage_dir}")
        
        # Get all face records for this identity with quality scores
        logger.debug(f"[IDENTITIES] Querying database for face records with identity_id={identity.id}...")
        result = await db.execute(
            select(
                Face.face_image_path,
                Detection.pipeline_id,
                IdentityEmbedding.quality
            )
            .join(Detection, Face.detection_id == Detection.id)
            .outerjoin(IdentityEmbedding, 
                (IdentityEmbedding.detection_id == Face.detection_id) &
                (IdentityEmbedding.identity_id == identity.id)
            )
            .where(
                Face.identity_id == identity.id,
                Face.face_image_path.isnot(None)
            )
            .order_by(
                IdentityEmbedding.quality.desc().nulls_last(),
                Detection.timestamp.desc()
            )
            .limit(10)  # Check top 10 by quality
        )
        
        rows = result.all()
        logger.info(f"[IDENTITIES] Found {len(rows)} face records in database for identity {identity.id}")
        
        best_path = None
        best_quality = None
        
        for idx, row in enumerate(rows):
            face_path, pipeline_id, quality = row
            logger.debug(f"[IDENTITIES] Checking face record {idx+1}: path={face_path}, pipeline={pipeline_id}, quality={quality}")
            
            if not face_path:
                logger.debug(f"[IDENTITIES] Skipping record {idx+1} - no face_image_path")
                continue
            
            # Check if file exists
            if os.path.exists(face_path):
                logger.debug(f"[IDENTITIES] ✅ File exists: {face_path}")
                # If we have quality scores, prefer highest quality
                if quality is not None:
                    if best_quality is None or quality > best_quality:
                        best_path = face_path
                        best_quality = quality
                        logger.info(f"[IDENTITIES] Updated best path (quality={quality:.3f}): {best_path}")
                elif best_path is None:
                    # If no quality scores, use first found
                    best_path = face_path
                    logger.info(f"[IDENTITIES] Using first found path (no quality score): {best_path}")
            else:
                logger.warning(f"[IDENTITIES] ⚠️ File does not exist: {face_path}")
        
        # If no path found in database, try to find in storage structure directly
        if not best_path:
            logger.info(f"[IDENTITIES] No valid paths found in database. Searching storage directory structure...")
            if not os.path.exists(storage_dir):
                logger.warning(f"[IDENTITIES] ⚠️ Storage directory does not exist: {storage_dir}")
                return None
            
            from db_models import IdentityType
            is_known = identity.type == IdentityType.KNOWN
            
            pipeline_dirs = [d for d in os.listdir(storage_dir) if os.path.isdir(os.path.join(storage_dir, d))]
            logger.info(f"[IDENTITIES] Found {len(pipeline_dirs)} pipeline directories in storage")
            
            # Search all pipeline directories
            for pipeline_dir_name in pipeline_dirs:
                pipeline_path = os.path.join(storage_dir, pipeline_dir_name)
                
                if is_known:
                    # Known identities: storage/pipeline_id/person_name/image.jpg
                    person_dir = os.path.join(pipeline_path, safe_name)
                    logger.debug(f"[IDENTITIES] Checking known identity directory: {person_dir}")
                    
                    if os.path.exists(person_dir) and os.path.isdir(person_dir):
                        logger.info(f"[IDENTITIES] ✅ Found person directory: {person_dir}")
                        image_files = [
                            f for f in os.listdir(person_dir)
                            if f.lower().endswith(('.jpg', '.jpeg', '.png'))
                        ]
                        if image_files:
                            image_files.sort(reverse=True)
                            best_path = os.path.join(person_dir, image_files[0])
                            logger.info(f"[IDENTITIES] ✅ Selected image from storage/{pipeline_dir_name}/{safe_name}/: {best_path}")
                            break
                else:
                    # Unknown identities: storage/pipeline_id/unknown/image.jpg
                    unknown_dir = os.path.join(pipeline_path, "unknown")
                    logger.debug(f"[IDENTITIES] Checking unknown identity directory: {unknown_dir}")
                    
                    if os.path.exists(unknown_dir) and os.path.isdir(unknown_dir):
                        logger.info(f"[IDENTITIES] ✅ Found unknown directory: {unknown_dir}")
                        image_files = [
                            f for f in os.listdir(unknown_dir)
                            if f.lower().endswith(('.jpg', '.jpeg', '.png'))
                        ]
                        if image_files:
                            # Sort by filename (most recent first)
                            image_files.sort(reverse=True)
                            best_path = os.path.join(unknown_dir, image_files[0])
                            logger.info(f"[IDENTITIES] ✅ Selected image from storage/{pipeline_dir_name}/unknown/: {best_path}")
                            break
        
        # Convert to relative path if found
        if best_path:
            logger.info(f"[IDENTITIES] Converting path to relative format: {best_path}")
            if os.path.isabs(best_path):
                best_path_abs = os.path.abspath(best_path)
                if best_path_abs.startswith(storage_dir_abs):
                    relative_path = os.path.relpath(best_path_abs, storage_dir_abs)
                    final_path = 'storage/' + relative_path.replace('\\', '/')
                    logger.info(f"[IDENTITIES] ✅ Converted absolute path to relative: {final_path}")
                    return final_path
                else:
                    logger.warning(f"[IDENTITIES] ⚠️ Path outside storage directory: {best_path_abs}")
            else:
                final_path = 'storage/' + best_path.lstrip('/') if not best_path.startswith('storage/') else best_path
                logger.info(f"[IDENTITIES] ✅ Using relative path: {final_path}")
                return final_path
        else:
            logger.warning(f"[IDENTITIES] ❌ No image found for identity {identity.id} ('{person_name}') in storage")
        
        return None
        
    except Exception as e:
        logger.error(f"[IDENTITIES] ❌ Error finding best image from storage for identity {identity.id}: {e}", exc_info=True)
        return None

# Import identity_service and identity_index (may be None if not initialized)
# Use getattr to access dynamically since they're set during startup
def get_identity_service():
    """Get identity_service instance (may be None if not initialized)"""
    try:
        # Try from backend.core first (set during startup)
        import backend.core
        if hasattr(backend.core, 'identity_service'):
            return backend.core.identity_service
    except (ImportError, AttributeError):
        pass
    
    try:
        # Fallback to direct import
        from backend.core.identity_service import identity_service
        return identity_service
    except (ImportError, AttributeError):
        return None

def get_identity_index():
    """Get identity_index instance (may be None if not initialized)"""
    try:
        # Try from backend.core first (set during startup)
        import backend.core
        if hasattr(backend.core, 'identity_index'):
            return backend.core.identity_index
    except (ImportError, AttributeError):
        pass
    
    try:
        # Fallback to direct import
        from backend.core.identity_index import identity_index
        return identity_index
    except (ImportError, AttributeError):
        return None

# For backward compatibility, try to import directly
try:
    from backend.core.identity_service import identity_service
except (ImportError, AttributeError):
    identity_service = None

try:
    from backend.core.identity_index import identity_index
except (ImportError, AttributeError):
    identity_index = None
import numpy as np
import cv2

logger = logging.getLogger(__name__)

# Create router with tags for API documentation
router = APIRouter(
    prefix="/api",
    tags=["Identity Management"],
    responses={
        401: {"description": "Unauthorized"},
        403: {"description": "Forbidden - Admin access required"},
        404: {"description": "Not found"},
        500: {"description": "Internal server error"}
    }
)


# =====================================================
# Request/Response Models
# =====================================================

class IdentityListItem(BaseModel):
    id: str
    type: str
    display_name: Optional[str]
    status: str
    first_seen_at: str
    last_seen_at: str
    appearances_count: int
    best_snapshot_path: Optional[str]  # Original path for reference
    snapshot_url: Optional[str]  # Backend provides ready-to-use URL
    pipeline_ids: List[str]  # List of pipeline IDs where this identity was seen

    class Config:
        from_attributes = True


class IdentityDetail(IdentityListItem):
    appearances: List[dict]
    embeddings_count: int
    faces_count: int

    class Config:
        from_attributes = True


class PromoteRequest(BaseModel):
    display_name: str
    person_code: Optional[str] = None  # Optional identifier code


class MergeRequest(BaseModel):
    from_identity_id: str
    to_identity_id: str
    notes: Optional[str] = None

class MergeMultipleRequest(BaseModel):
    identity_ids: List[str]  # List of identity IDs to merge
    target_identity_id: Optional[str] = None  # Optional: if provided, merge all others into this one
    notes: Optional[str] = None


class MergePreviewRequest(BaseModel):
    """Request for merge preview - shows what will happen before actual merge"""
    identity_ids: List[str]  # List of identity IDs to preview merge
    target_identity_id: Optional[str] = None  # Optional: if provided, preview merge into this one


class MergePreviewResponse(BaseModel):
    """Response with detailed merge preview information"""
    success: bool
    target_identity: dict
    source_identities: List[dict]
    pipeline_distribution: dict
    type_promotion: dict
    snapshot_selection: dict
    statistics: dict
    warnings: List[str]
    selection_details: Optional[dict] = None


class SearchByImageRequest(BaseModel):
    scope: str = "both"  # "known", "unknown", or "both"
    top_k: int = 10
    date_from: Optional[str] = None
    date_to: Optional[str] = None
    pipeline_id: Optional[str] = None


class SearchResult(BaseModel):
    identity_id: str
    type: str
    display_name: Optional[str]
    similarity: float
    best_snapshot_path: Optional[str]
    last_seen_at: str
    appearances_count: int


class MergeSuggestionResponse(BaseModel):
    id: Optional[int] = None  # None for on-the-fly suggestions, int for database suggestions
    cluster_id: Optional[str]
    identity_ids: List[str]
    identity_count: Optional[int] = None
    confidence: float
    confidence_percent: Optional[float] = None
    status: str
    representative_snapshots: Optional[List[str]] = None
    snapshot_count: Optional[int] = None
    created_at: str
    cluster_type: Optional[str] = None
    is_large_cluster: Optional[bool] = None
    is_cross_camera: Optional[bool] = None  # NEW: Indicates cross-camera match (different cameras)
    pipelines: Optional[List[str]] = None  # NEW: List of pipelines involved
    display_name: Optional[str] = None
    recommendation: Optional[str] = None


# =====================================================
# Health/Status Endpoint
# =====================================================

@router.get("/admin/identities/status", summary="Identity Service Status", description="Check if identity management service is available (Admin only)")
async def get_identity_service_status(
    current_user: User = Depends(require_role(["admin"])),
    db: AsyncSession = Depends(get_db)
):
    """
    Check the status of the identity management service.
    Returns information about service availability and index statistics.
    Includes both FAISS and pgvector backend status.
    """
    from config import settings as config_settings
    
    try:
        # Get configured backend
        vector_backend = getattr(config_settings, 'VECTOR_BACKEND', 'faiss').lower()
        
        status_info = {
            "service_available": identity_service is not None,
            "index_available": identity_index is not None,
            "model_manager_available": model_manager is not None and hasattr(model_manager, 'recognizer'),
            "vector_backend": vector_backend,
        }
        
        # FAISS Index Stats
        if identity_index:
            stats = identity_index.get_stats()
            status_info["faiss_index_stats"] = stats
        else:
            status_info["faiss_index_stats"] = None
        
        # pgvector Stats (if available)
        if vector_backend == 'pgvector':
            try:
                from backend.core.identity_index_pgvector import identity_index_pgvector
                if identity_index_pgvector:
                    pgvector_stats = await identity_index_pgvector.get_stats(db)
                    pgvector_health = await identity_index_pgvector.health_check(db)
                    status_info["pgvector_stats"] = pgvector_stats
                    status_info["pgvector_health"] = pgvector_health
                else:
                    status_info["pgvector_stats"] = {"error": "pgvector index not initialized"}
                    status_info["pgvector_health"] = {"healthy": False, "error": "not initialized"}
            except ImportError:
                status_info["pgvector_stats"] = {"error": "pgvector module not available"}
                status_info["pgvector_health"] = {"healthy": False, "error": "module not available"}
            except Exception as e:
                logger.warning(f"Could not get pgvector stats: {e}")
                status_info["pgvector_stats"] = {"error": str(e)}
                status_info["pgvector_health"] = {"healthy": False, "error": str(e)}
        else:
            status_info["pgvector_stats"] = {"status": "disabled", "reason": f"VECTOR_BACKEND={vector_backend}"}
        
        # Add verification results
        identity_service_local = get_identity_service()
        if identity_service_local and model_manager:
            try:
                from backend.core.identity_loader import IdentityLoader
                identity_loader = IdentityLoader(identity_service_local, model_manager)
                verification = await identity_loader.verify_indexes(db)
                status_info["verification"] = verification
            except Exception as e:
                logger.warning(f"Could not verify indexes: {e}")
                status_info["verification"] = {"error": str(e)}
        
        return {
            "status": "available" if status_info["service_available"] else "unavailable",
            "vector_backend": vector_backend,
            "details": status_info,
            "available_endpoints": [
                "GET /api/admin/unknown - List unknown identities",
                "GET /api/admin/identity/{id} - Get identity details",
                "POST /api/admin/unknown/{id}/promote - Promote unknown to known",
                "POST /api/admin/identities/merge - Merge identities",
                "POST /api/search/by-image - Search by image",
                "GET /api/admin/merge-suggestions - Get merge suggestions",
                "POST /api/admin/merge-suggestions/{id}/approve - Approve merge",
                "POST /api/admin/merge-suggestions/{id}/reject - Reject merge",
                "POST /api/admin/identities/load-known-faces - Load known faces from storage/faces",
                "GET /api/admin/identities/verify-indexes - Verify both indexes"
            ]
        }
    except Exception as e:
        logger.error(f"Error checking identity service status: {e}", exc_info=True)
        return {
            "status": "error",
            "error": str(e)
        }


@router.post("/admin/identities/load-known-faces", summary="Load Known Faces", description="Load known faces from storage/faces directory into Identity system (Admin only)")
async def load_known_faces(
    force_reload: bool = False,
    current_user: User = Depends(require_role(["admin"])),
    db: AsyncSession = Depends(get_db)
):
    """
    Load known faces from storage/faces directory into the Identity system.
    This ensures known faces are in the KNOWN FAISS index.
    """
    if not identity_service or not model_manager:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Identity service or model manager not available"
        )
    
    try:
        from backend.core.identity_loader import IdentityLoader
        from config import settings
        
        identity_loader = IdentityLoader(identity_service, model_manager)
        faces_dir = getattr(settings, 'FACES_DIR', './storage/faces')
        
        loaded, skipped, errors = await identity_loader.load_known_faces_from_directory(
            faces_dir=faces_dir,
            db=db,
            force_reload=force_reload
        )
        
        if loaded > 0:
            await db.commit()
        
        # Save indexes
        if identity_index:
            identity_index.save()
        
        return {
            "success": True,
            "message": f"Loaded {loaded} known faces, {skipped} skipped, {errors} errors",
            "loaded": loaded,
            "skipped": skipped,
            "errors": errors,
            "faces_directory": faces_dir
        }
    except Exception as e:
        logger.error(f"Error loading known faces: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to load known faces: {str(e)}"
        )


@router.get("/admin/identities/debug/{identity_id}", summary="Debug Identity Recognition", description="Debug why a specific identity is or isn't being recognized (Admin only)")
async def debug_identity_recognition(
    identity_id: str,
    current_user: User = Depends(require_role(["admin"])),
    db: AsyncSession = Depends(get_db)
):
    """
    Debug endpoint to check why an identity is or isn't being recognized.
    Returns detailed information about FAISS index state, database state, and potential issues.
    """
    try:
        from backend.core.identity_index import identity_index
        from backend.core.identity_service import identity_service
        
        identity_uuid = uuid.UUID(identity_id)
        identity_id_str = str(identity_uuid)
        
        # Get identity from database
        result = await db.execute(
            select(Identity).where(Identity.id == identity_uuid)
        )
        identity = result.scalar_one_or_none()
        
        if not identity:
            raise HTTPException(status_code=404, detail=f"Identity {identity_id} not found in database")
        
        debug_info = {
            "identity_id": identity_id_str,
            "display_name": identity.display_name,
            "database": {
                "exists": True,
                "type": identity.type.value,
                "status": identity.status.value,
                "display_name": identity.display_name,
                "appearances_count": identity.appearances_count,
                "first_seen_at": identity.first_seen_at.isoformat() if identity.first_seen_at else None,
                "last_seen_at": identity.last_seen_at.isoformat() if identity.last_seen_at else None,
            },
            "faiss": {
                "in_known_index": False,
                "in_unknown_index": False,
                "faiss_ids": [],
                "metadata_entries": [],
                "issues": []
            },
            "embeddings": {
                "database_count": 0,
                "with_faiss_id": 0,
                "without_faiss_id": 0,
                "details": []
            },
            "recognition_status": "unknown",
            "issues": []
        }
        
        # Check FAISS KNOWN index
        if identity_index and identity_index.known_identity_to_faiss:
            known_faiss_ids = identity_index.known_identity_to_faiss.get(identity_id_str, [])
            if known_faiss_ids:
                debug_info["faiss"]["in_known_index"] = True
                debug_info["faiss"]["faiss_ids"] = known_faiss_ids
                
                # Check metadata
                for faiss_id in known_faiss_ids:
                    if faiss_id in identity_index.known_metadata:
                        metadata_identity_id = identity_index.known_metadata[faiss_id]
                        debug_info["faiss"]["metadata_entries"].append({
                            "faiss_id": faiss_id,
                            "identity_id": metadata_identity_id,
                            "matches": metadata_identity_id == identity_id_str
                        })
                        if metadata_identity_id != identity_id_str:
                            debug_info["faiss"]["issues"].append(f"FAISS ID {faiss_id} maps to different identity_id: {metadata_identity_id}")
                    else:
                        debug_info["faiss"]["issues"].append(f"FAISS ID {faiss_id} not in metadata")
        
        # Check FAISS UNKNOWN index
        if identity_index and identity_index.unknown_identity_to_faiss:
            unknown_faiss_ids = identity_index.unknown_identity_to_faiss.get(identity_id_str, [])
            if unknown_faiss_ids:
                debug_info["faiss"]["in_unknown_index"] = True
                debug_info["faiss"]["faiss_ids"].extend(unknown_faiss_ids)
                debug_info["faiss"]["issues"].append(f"Identity is in UNKNOWN index but type={identity.type.value}")
        
        # Check database embeddings
        emb_result = await db.execute(
            select(IdentityEmbedding).where(IdentityEmbedding.identity_id == identity_uuid)
        )
        embeddings = emb_result.scalars().all()
        debug_info["embeddings"]["database_count"] = len(embeddings)
        
        for emb in embeddings:
            emb_info = {
                "id": emb.id,
                "faiss_id": emb.faiss_id,
                "faiss_index_type": emb.faiss_index_type,
                "quality": emb.quality,
                "pipeline_id": emb.pipeline_id,
                "created_at": emb.created_at.isoformat() if emb.created_at else None
            }
            debug_info["embeddings"]["details"].append(emb_info)
            
            if emb.faiss_id is not None:
                debug_info["embeddings"]["with_faiss_id"] += 1
            else:
                debug_info["embeddings"]["without_faiss_id"] += 1
        
        # Determine recognition status
        if identity.type == IdentityType.KNOWN:
            if debug_info["faiss"]["in_known_index"]:
                if len(debug_info["faiss"]["faiss_ids"]) > 0:
                    debug_info["recognition_status"] = "should_be_recognized"
                else:
                    debug_info["recognition_status"] = "in_index_but_no_embeddings"
                    debug_info["issues"].append("Identity is in FAISS but has no faiss_ids")
            else:
                debug_info["recognition_status"] = "not_in_faiss"
                debug_info["issues"].append("Identity is KNOWN but not in FAISS KNOWN index")
        else:
            if debug_info["faiss"]["in_unknown_index"]:
                debug_info["recognition_status"] = "in_unknown_index"
            else:
                debug_info["recognition_status"] = "not_indexed"
                debug_info["issues"].append("Identity is UNKNOWN and not in FAISS")
        
        # Check for common issues
        if identity.type == IdentityType.KNOWN and not debug_info["faiss"]["in_known_index"]:
            debug_info["issues"].append("CRITICAL: KNOWN identity not in FAISS KNOWN index - will not be recognized!")
        
        if debug_info["embeddings"]["without_faiss_id"] > 0:
            debug_info["issues"].append(f"{debug_info['embeddings']['without_faiss_id']} embeddings have no faiss_id - not indexed in FAISS")
        
        if identity.status != IdentityStatus.ACTIVE:
            debug_info["issues"].append(f"Identity status is {identity.status.value} (not ACTIVE) - may be filtered")
        
        return debug_info
        
    except ValueError as e:
        raise HTTPException(status_code=400, detail=f"Invalid identity_id format: {e}")
    except Exception as e:
        logger.error(f"Error debugging identity {identity_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error debugging identity: {str(e)}")


@router.get("/admin/identities/verify-indexes", summary="Verify Indexes", description="Verify both KNOWN and UNKNOWN indexes are working correctly (Admin only)")
async def verify_indexes(
    current_user: User = Depends(require_role(["admin"])),
    db: AsyncSession = Depends(get_db)
):
    """
    Verify both KNOWN and UNKNOWN FAISS indexes match the database.
    Returns detailed verification results.
    """
    identity_service = get_identity_service()
    identity_service = get_identity_service()
    if not identity_service or not model_manager:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Identity service or model manager not available"
        )

    try:
        from backend.core.identity_loader import IdentityLoader
        
        identity_loader = IdentityLoader(identity_service, model_manager)
        verification = await identity_loader.verify_indexes(db)
        
        return {
            "success": True,
            "verification": verification,
            "summary": {
                "known_index_healthy": verification["known_index"]["match"],
                "unknown_index_healthy": verification["unknown_index"]["match"],
                "known_count": verification["known_index"]["faiss_count"],
                "unknown_count": verification["unknown_index"]["faiss_count"],
                "assets_faces_loaded": verification.get("assets_faces", {}).get("loaded_count", 0),
                "assets_faces_total": verification.get("assets_faces", {}).get("file_count", 0)
            }
        }
    except Exception as e:
        logger.error(f"Error verifying indexes: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to verify indexes: {str(e)}"
        )


# =====================================================
# Unknown Identities List
# =====================================================

@router.get("/admin/unknown", response_model=dict, summary="List Unknown Identities", description="Get paginated list of unknown identities with filters (Admin or users with pipeline access)")
async def list_unknown_identities(
    page: int = 1,
    page_size: int = 20,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    pipeline_id: Optional[str] = None,
    status_filter: Optional[str] = None,
    min_appearances: Optional[int] = None,
    show_all: bool = False,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    List unknown identities with pagination and filters.
    - Admin: sees all unknown identities
    - Regular users: only see unknown identities from their accessible pipelines

    Display window: by default only identities last seen within
    UNKNOWN_FACE_DISPLAY_HOURS (setting, 0 = disabled) are listed —
    display-only, mirroring DASHBOARD_FACE_DISPLAY_HOURS for known faces.
    `show_all=true` (the page's Show-all toggle) or an explicit `date_from`
    bypasses the window. Data stays stored until retention deletes it.
    """
    try:
        # REDIS CACHING: Check cache first for faster page loads
        from backend.core.redis_cache import redis_cache_service

        # Display window (read per request → setting applies without restart)
        display_window_hours = float(getattr(settings, 'UNKNOWN_FACE_DISPLAY_HOURS', 24) or 0)
        window_active = (not show_all) and (not date_from) and display_window_hours > 0
        window_cutoff = (datetime.utcnow() - timedelta(hours=display_window_hours)) if window_active else None

        # Build filters dict for cache key (window params MUST be part of the
        # key — the cache TTL is ~30h, far longer than the display window)
        filters = {
            "date_from": date_from,
            "date_to": date_to,
            "pipeline_id": pipeline_id,
            "status_filter": status_filter,
            "min_appearances": min_appearances,
            "show_all": show_all,
            "display_window_hours": display_window_hours if window_active else None,
            # Bucket the cutoff to 5-minute steps so the rolling window doesn't
            # defeat caching entirely while stale entries still age out fast
            "window_bucket": int(window_cutoff.timestamp() // 300) if window_cutoff else None,
        }
        
        # Generate cache key
        cache_key = await redis_cache_service.get_unknown_cache_key(
            user_id=current_user.id,
            page=page,
            page_size=page_size,
            filters=filters
        )
        
        # Try to get from cache
        cached_result = await redis_cache_service.get(cache_key)
        if cached_result:
            logger.info(f"[UNKNOWN_API] [CACHE] ✅ Cache HIT - returning cached data (key: {cache_key})")
            return cached_result
        
        logger.info(f"[UNKNOWN_API] [CACHE] ❌ Cache MISS - querying database (key: {cache_key})")
        
        # Check if identity tables exist (migration may not have run)
        # This endpoint should work even if identity_service is not initialized
        
        # Get user's accessible pipelines (if not admin)
        user_pipelines = None
        if current_user.role != "admin":
            from backend.auth.auth_service import AuthService
            user_pipelines = await AuthService.get_user_pipelines(current_user.id, db)
            if not user_pipelines:
                # User has no pipeline access - return empty result
                empty_result = {
                    "identities": [],
                    "total": 0,
                    "page": page,
                    "page_size": page_size,
                    "total_pages": 1,
                    "stats": {
                        "total_unknown": 0,
                        "total_appearances": 0,
                        "active_cameras": 0
                    }
                }
                # Cache empty result too (use unknown faces TTL)
                cache_ttl = getattr(settings, 'CACHE_TTL_UNKNOWN', 108000)  # Default 30 hours
                await redis_cache_service.set(cache_key, empty_result, ttl=cache_ttl)
                return empty_result
        
        # Build query
        query = select(Identity).where(Identity.type == IdentityType.UNKNOWN)

        # Apply filters
        if status_filter:
            query = query.where(Identity.status == IdentityStatus(status_filter))
        else:
            query = query.where(Identity.status == IdentityStatus.ACTIVE)

        if date_from:
            date_from_dt = datetime.fromisoformat(date_from.replace('Z', '+00:00'))
            query = query.where(Identity.last_seen_at >= date_from_dt)
        elif window_cutoff is not None:
            # UNKNOWN_FACE_DISPLAY_HOURS display window (Show-all toggle bypasses)
            query = query.where(Identity.last_seen_at >= window_cutoff)

        if date_to:
            date_to_dt = datetime.fromisoformat(date_to.replace('Z', '+00:00'))
            query = query.where(Identity.last_seen_at <= date_to_dt)

        if min_appearances:
            query = query.where(Identity.appearances_count >= min_appearances)
        
        # Calculate statistics efficiently using SQL aggregations
        # Get all identities matching filters (for stats calculation)
        stats_query = select(Identity).where(Identity.type == IdentityType.UNKNOWN)
        
        if status_filter:
            stats_query = stats_query.where(Identity.status == IdentityStatus(status_filter))
        else:
            stats_query = stats_query.where(Identity.status == IdentityStatus.ACTIVE)
        
        if date_from:
            date_from_dt = datetime.fromisoformat(date_from.replace('Z', '+00:00'))
            stats_query = stats_query.where(Identity.last_seen_at >= date_from_dt)
        elif window_cutoff is not None:
            stats_query = stats_query.where(Identity.last_seen_at >= window_cutoff)

        if date_to:
            date_to_dt = datetime.fromisoformat(date_to.replace('Z', '+00:00'))
            stats_query = stats_query.where(Identity.last_seen_at <= date_to_dt)
        
        if min_appearances:
            stats_query = stats_query.where(Identity.appearances_count >= min_appearances)
        
        # Get all matching identities for stats (only on first page to avoid performance issues)
        stats_identities = []
        if page == 1:
            stats_result = await db.execute(stats_query)
            stats_identities = stats_result.scalars().all()
        
        # Apply pagination
        offset = (page - 1) * page_size
        query = query.order_by(Identity.last_seen_at.desc()).offset(offset).limit(page_size)
        
        # Execute query
        result = await db.execute(query)
        identities = result.scalars().all()
        
        # Get camera counts and pipeline IDs for each identity
        identity_list = []
        total_appearances = 0
        unique_pipelines = set()
        total_with_pipelines = 0
        
        # Process identities for current page
        for identity in identities:
            # CRITICAL: Double-check that identity is UNKNOWN (safety check)
            # This should never happen if query is correct, but protects against data corruption
            if identity.type != IdentityType.UNKNOWN:
                logger.warning(f"[SECURITY] Filtered out KNOWN identity {identity.id} from unknown list (type: {identity.type})")
                continue
            
            # Get unique pipeline IDs from multiple sources
            pipeline_ids_set = set()
            
            # 1. From IdentityAppearance
            appearance_result = await db.execute(
                select(IdentityAppearance.pipeline_id).where(
                    IdentityAppearance.identity_id == identity.id
                ).distinct()
            )
            for row in appearance_result:
                if row[0]:
                    pipeline_ids_set.add(row[0])
            
            # 2. From IdentityEmbedding (fallback if no appearances)
            if not pipeline_ids_set:
                embedding_result = await db.execute(
                    select(IdentityEmbedding.pipeline_id).where(
                        IdentityEmbedding.identity_id == identity.id
                    ).distinct()
                )
                for row in embedding_result:
                    if row[0]:
                        pipeline_ids_set.add(row[0])
            
            # 3. From Face->Detection (fallback if no embeddings)
            if not pipeline_ids_set:
                face_result = await db.execute(
                    select(Detection.pipeline_id).join(
                        Face, Face.detection_id == Detection.id
                    ).where(
                        Face.identity_id == identity.id
                    ).distinct()
                )
                for row in face_result:
                    if row[0]:
                        pipeline_ids_set.add(row[0])
            
            pipeline_ids = sorted(list(pipeline_ids_set))
            cameras_count = len(pipeline_ids)
            
            # Skip identities with no pipeline IDs (completely remove them)
            if len(pipeline_ids) == 0:
                continue
            
            # Filter by pipeline_id if specified
            if pipeline_id:
                if pipeline_id not in pipeline_ids:
                    continue
            
            # For non-admin users: filter by accessible pipelines
            if current_user.role != "admin":
                if not (pipeline_ids_set & set(user_pipelines)):
                    continue
            
            # Accumulate statistics for current page
            total_appearances += identity.appearances_count
            unique_pipelines.update(pipeline_ids)
            
            # Verify best_snapshot_path exists on disk and convert to relative path for serving
            best_snapshot_path = identity.best_snapshot_path
            logger.debug(f"[IDENTITIES] 📸 Retrieving image for identity {identity.id} ({identity.display_name})")
            logger.debug(f"[IDENTITIES]   Original best_snapshot_path from DB: {best_snapshot_path}")
            
            if best_snapshot_path:
                # Convert absolute path to relative path for static file serving
                storage_dir = getattr(settings, 'STORAGE_DIR', './storage')
                # Normalize storage_dir to handle both absolute and relative paths
                storage_dir_abs = os.path.abspath(storage_dir)
                
                # Check if path is absolute and within storage directory
                if os.path.isabs(best_snapshot_path):
                    best_snapshot_path_abs = os.path.abspath(best_snapshot_path)
                    if best_snapshot_path_abs.startswith(storage_dir_abs):
                        # Convert to relative: /app/storage/pipeline/name/file.jpg -> storage/pipeline/name/file.jpg
                        relative_path = os.path.relpath(best_snapshot_path_abs, storage_dir_abs)
                        best_snapshot_path = 'storage/' + relative_path.replace('\\', '/')
                    elif not os.path.exists(best_snapshot_path):
                        best_snapshot_path = None
                else:
                    # Already relative, check if it's from known_faces or needs storage/ prefix
                    if best_snapshot_path.startswith('known_faces/'):
                        # Known faces are in storage/known_faces/
                        best_snapshot_path = 'storage/' + best_snapshot_path
                    elif not best_snapshot_path.startswith('storage/'):
                        best_snapshot_path = 'storage/' + best_snapshot_path.lstrip('/')
                
                # Verify file exists (if we have a path)
                if best_snapshot_path:
                    # Remove 'storage/' prefix to get actual file path
                    file_path = best_snapshot_path.replace('storage/', '', 1).lstrip('/')
                    full_path = os.path.join(storage_dir, file_path)
                    if not os.path.exists(full_path):
                        # Try to find best snapshot from appearances (sorted by quality)
                        # IdentityAppearance and IdentityEmbedding are already imported at the top
                        # func is already imported at the top
                        
                        # Get appearance with best quality snapshot
                        appearance_result = await db.execute(
                            select(
                                IdentityAppearance.best_snapshot_path,
                                IdentityAppearance.pipeline_id,
                                func.max(IdentityEmbedding.quality).label('max_quality')
                            )
                            .outerjoin(
                                IdentityEmbedding,
                                (IdentityEmbedding.identity_id == IdentityAppearance.identity_id) &
                                (IdentityEmbedding.pipeline_id == IdentityAppearance.pipeline_id)
                            )
                            .where(
                                IdentityAppearance.identity_id == identity.id,
                                IdentityAppearance.best_snapshot_path.isnot(None)
                            )
                            .group_by(IdentityAppearance.best_snapshot_path, IdentityAppearance.pipeline_id)
                            .order_by(func.max(IdentityEmbedding.quality).desc().nulls_last())
                            .limit(1)
                        )
                        appearance_row = appearance_result.first()
                        
                        if appearance_row and appearance_row[0]:
                            appearance_path = appearance_row[0]
                            if os.path.exists(appearance_path):
                                # Convert appearance path to relative
                                if os.path.isabs(appearance_path):
                                    appearance_path_abs = os.path.abspath(appearance_path)
                                    if appearance_path_abs.startswith(storage_dir_abs):
                                        relative_path = os.path.relpath(appearance_path_abs, storage_dir_abs)
                                        best_snapshot_path = 'storage/' + relative_path.replace('\\', '/')
                                    else:
                                        best_snapshot_path = None
                                else:
                                    best_snapshot_path = 'storage/' + appearance_path.lstrip('/') if not appearance_path.startswith('storage/') else appearance_path
                            else:
                                # Try to find best image from storage/pipeline_id/person_name/ structure
                                best_snapshot_path = await _find_best_image_from_storage_for_identity(identity, storage_dir, db)
                        else:
                            # No appearances found, try to find best image from storage/pipeline_id/person_name/ structure
                            logger.debug(f"[IDENTITIES]   🔍 No appearances found, searching storage structure for {identity.display_name}...")
                            best_snapshot_path = await _find_best_image_from_storage_for_identity(identity, storage_dir, db)
            
            # Log final image path for this identity (list view)
            if best_snapshot_path:
                logger.debug(f"[IDENTITIES]   🌐 Final display path for {identity.display_name} (list view): {best_snapshot_path}")
            else:
                logger.debug(f"[IDENTITIES]   ⚠️ No image path for {identity.display_name} (list view) - will show placeholder")
            
            # Backend constructs snapshot URL (all logic in backend)
            snapshot_url = None
            if best_snapshot_path:
                # Convert to URL format with leading slash
                if best_snapshot_path.startswith('storage/'):
                    snapshot_url = f"/{best_snapshot_path}"
                elif not best_snapshot_path.startswith('/'):
                    snapshot_url = f"/storage/{best_snapshot_path}"
                else:
                    snapshot_url = best_snapshot_path
            else:
                # Backend provides fallback URL (SVG data URI)
                snapshot_url = 'data:image/svg+xml,%3Csvg xmlns=%27http://www.w3.org/2000/svg%27 width=%27100%27 height=%27100%27%3E%3Crect fill=%27%23333%27 width=%27100%27 height=%27100%27/%3E%3Ccircle cx=%2750%27 cy=%2735%27 r=%2715%27 fill=%27%23999%27/%3E%3Cpath d=%27M 25 70 Q 25 60 35 60 L 65 60 Q 75 60 75 70 L 75 85 L 25 85 Z%27 fill=%27%23999%27/%3E%3C/svg%3E'
            
            identity_list.append({
                "id": str(identity.id),
                "type": identity.type.value,
                "display_name": identity.display_name,
                "status": identity.status.value,
                "first_seen_at": identity.first_seen_at.isoformat(),
                "last_seen_at": identity.last_seen_at.isoformat(),
                "appearances_count": identity.appearances_count,
                "best_snapshot_path": best_snapshot_path,  # Keep original path for reference
                "snapshot_url": snapshot_url,  # Backend provides ready-to-use URL
                "pipeline_ids": pipeline_ids  # List of pipeline IDs where this identity was seen
            })
        
        # Calculate accurate statistics (only on first page for performance)
        if page == 1 and stats_identities:
            # Count identities that have pipeline IDs
            total_with_pipelines = 0
            total_appearances_all = 0
            unique_pipelines_all = set()
            
            for identity in stats_identities:
                # CRITICAL: Double-check that identity is UNKNOWN (safety check)
                if identity.type != IdentityType.UNKNOWN:
                    logger.warning(f"[SECURITY] Filtered out KNOWN identity {identity.id} from unknown stats (type: {identity.type})")
                    continue
                
                pipeline_ids_set_all = set()
                
                # Check IdentityAppearance
                appearance_result = await db.execute(
                    select(IdentityAppearance.pipeline_id).where(
                        IdentityAppearance.identity_id == identity.id
                    ).distinct()
                )
                for row in appearance_result:
                    if row[0]:
                        pipeline_ids_set_all.add(row[0])
                
                # Check IdentityEmbedding
                if not pipeline_ids_set_all:
                    embedding_result = await db.execute(
                        select(IdentityEmbedding.pipeline_id).where(
                            IdentityEmbedding.identity_id == identity.id
                        ).distinct()
                    )
                    for row in embedding_result:
                        if row[0]:
                            pipeline_ids_set_all.add(row[0])
                
                # Check Face->Detection
                if not pipeline_ids_set_all:
                    face_result = await db.execute(
                        select(Detection.pipeline_id).join(
                            Face, Face.detection_id == Detection.id
                        ).where(
                            Face.identity_id == identity.id
                        ).distinct()
                    )
                    for row in face_result:
                        if row[0]:
                            pipeline_ids_set_all.add(row[0])
                
                # Only count if has pipeline IDs
                if len(pipeline_ids_set_all) > 0:
                    # Apply pipeline_id filter if specified
                    if pipeline_id and pipeline_id not in pipeline_ids_set_all:
                        continue
                    
                    # For non-admin users: filter by accessible pipelines
                    if current_user.role != "admin":
                        if not (pipeline_ids_set_all & set(user_pipelines)):
                            continue
                    
                    total_with_pipelines += 1
                    total_appearances_all += identity.appearances_count
                    unique_pipelines_all.update(pipeline_ids_set_all)
            
            total = total_with_pipelines
            total_appearances = total_appearances_all
            unique_pipelines = unique_pipelines_all
        else:
            # For other pages, approximate from current page data
            # Note: This is approximate - for exact stats, always use page 1
            total = len(identity_list)  # Approximate - will be corrected when user goes to page 1
        
        result = {
            "identities": identity_list,
            "total": total,
            "page": page,
            "page_size": page_size,
            "total_pages": (total + page_size - 1) // page_size if total > 0 else 1,
            # Display-window contract for the frontend Show-all toggle
            "display_window_hours": display_window_hours if window_active else None,
            "show_all": show_all,
            "stats": {
                "total_unknown": total,
                "total_appearances": total_appearances,
                "active_cameras": len(unique_pipelines)
            }
        }
        
        # REDIS CACHING: Cache the result for future requests
        # Use longer TTL for unknown faces (30 hours) since they change less frequently
        cache_ttl = getattr(settings, 'CACHE_TTL_UNKNOWN', 108000)  # Default 30 hours (108000 seconds)
        await redis_cache_service.set(cache_key, result, ttl=cache_ttl)
        logger.info(f"[UNKNOWN_API] [CACHE] 💾 Cached result (TTL: {cache_ttl}s = {cache_ttl/3600:.1f} hours, key: {cache_key})")
        
        return result
    
    except Exception as e:
        logger.error(f"Error listing unknown identities: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to list unknown identities: {str(e)}"
        )


# =====================================================
# Identity Details
# =====================================================

@router.get("/admin/identity/{identity_id}", response_model=IdentityDetail, summary="Get Identity Details", description="Get detailed information about a specific identity including timeline (Admin or users with pipeline access)")
async def get_identity_details(
    identity_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    Get detailed information about an identity including timeline (admin only).
    """
    try:
        identity_uuid = uuid.UUID(identity_id)
        
        # Get identity
        result = await db.execute(
            select(Identity).where(Identity.id == identity_uuid)
        )
        identity = result.scalar_one_or_none()
        
        if not identity:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Identity {identity_id} not found"
            )
        
        # Check access for non-admin users
        if current_user.role != "admin":
            from backend.auth.auth_service import check_identity_access
            has_access = await check_identity_access(identity_id, current_user, db)
            if not has_access:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Access denied to this identity"
                )
            
        
        # Get appearances
        appearances_result = await db.execute(
            select(IdentityAppearance).where(
                IdentityAppearance.identity_id == identity_uuid
            ).order_by(IdentityAppearance.start_time.desc())
        )
        appearances = appearances_result.scalars().all()
        
        # Get embeddings count
        embeddings_result = await db.execute(
            select(func.count(IdentityEmbedding.id)).where(
                IdentityEmbedding.identity_id == identity_uuid
            )
        )
        embeddings_count = embeddings_result.scalar() or 0
        
        # Get faces count
        faces_result = await db.execute(
            select(func.count(Face.id)).where(
                Face.identity_id == identity_uuid
            )
        )
        faces_count = faces_result.scalar() or 0
        
        # Get unique pipeline IDs from multiple sources
        pipeline_ids_set = set()
        
        # 1. From IdentityAppearance
        appearance_result = await db.execute(
            select(IdentityAppearance.pipeline_id).where(
                IdentityAppearance.identity_id == identity_uuid
            ).distinct()
        )
        for row in appearance_result:
            if row[0]:
                pipeline_ids_set.add(row[0])
        
        # 2. From IdentityEmbedding (fallback if no appearances)
        if not pipeline_ids_set:
            embedding_result = await db.execute(
                select(IdentityEmbedding.pipeline_id).where(
                    IdentityEmbedding.identity_id == identity_uuid
                ).distinct()
            )
            for row in embedding_result:
                if row[0]:
                    pipeline_ids_set.add(row[0])
        
        # 3. From Face->Detection (fallback if no embeddings)
        if not pipeline_ids_set:
            face_result = await db.execute(
                select(Detection.pipeline_id).join(
                    Face, Face.detection_id == Detection.id
                ).where(
                    Face.identity_id == identity_uuid
                ).distinct()
            )
            for row in face_result:
                if row[0]:
                    pipeline_ids_set.add(row[0])
        
        pipeline_ids = sorted(list(pipeline_ids_set))
        cameras_count = len(pipeline_ids)
        
        # Get image - prioritize database paths, then search storage
        storage_dir = getattr(settings, 'STORAGE_DIR', './storage')
        from backend.utils.path_utils import path_to_url
        
        best_snapshot_path = None
        
        # Priority 1: Check identity.best_snapshot_path from database
        if identity.best_snapshot_path:
            logger.info(f"[IDENTITIES] 📸 Checking identity.best_snapshot_path from database: {identity.best_snapshot_path}")
            if os.path.exists(identity.best_snapshot_path):
                best_snapshot_path = identity.best_snapshot_path
                logger.info(f"[IDENTITIES] ✅ Using identity.best_snapshot_path: {best_snapshot_path}")
            else:
                logger.warning(f"[IDENTITIES] ⚠️ identity.best_snapshot_path doesn't exist: {identity.best_snapshot_path}")
        
        # Priority 2: Search database Face.face_image_path records (via helper function)
        if not best_snapshot_path:
            logger.info(f"[IDENTITIES] 📸 Searching database Face records for identity: {identity.id}")
            best_snapshot_path = await _find_best_image_from_storage_for_identity(identity, storage_dir, db)
            if best_snapshot_path:
                logger.info(f"[IDENTITIES] ✅ Found image from database Face records: {best_snapshot_path}")
        
        # Priority 3: Search storage directories (already done in _find_best_image_from_storage_for_identity)
        # This is handled inside the helper function as a fallback
        
        # Convert path to URL - handles both database paths and storage paths
        if best_snapshot_path:
            # Use path_to_url utility to normalize and convert to URL
            # This handles: /app/storage/..., storage/..., pipeline/name/file.jpg, etc.
            snapshot_url = path_to_url(best_snapshot_path, storage_dir)
            if snapshot_url:
                logger.info(f"[IDENTITIES] ✅ Converted to URL: {snapshot_url}")
            else:
                logger.warning(f"[IDENTITIES] ⚠️ Could not convert path to URL: {best_snapshot_path}")
                snapshot_url = None
        else:
            snapshot_url = None
        
        # Fallback to placeholder if no image found
        if not snapshot_url:
            logger.warning(f"[IDENTITIES] ❌ No image found for identity {identity.id}, using placeholder")
            snapshot_url = 'data:image/svg+xml,%3Csvg xmlns=%27http://www.w3.org/2000/svg%27 width=%27100%27 height=%27100%27%3E%3Crect fill=%27%23333%27 width=%27100%27 height=%27100%27/%3E%3Ccircle cx=%2750%27 cy=%2735%27 r=%2715%27 fill=%27%23999%27/%3E%3Cpath d=%27M 25 70 Q 25 60 35 60 L 65 60 Q 75 60 75 70 L 75 85 L 25 85 Z%27 fill=%27%23999%27/%3E%3C/svg%3E'
        
        # Backend constructs appearance snapshot URLs using centralized utility (all logic in backend)
        appearances_data = []
        for app in appearances:
            app_snapshot_url = path_to_url(app.best_snapshot_path, storage_dir) if app.best_snapshot_path else None
            
            appearances_data.append({
                "id": app.id,
                "pipeline_id": app.pipeline_id,
                "track_id": app.track_id,
                "start_time": app.start_time.isoformat(),
                "end_time": app.end_time.isoformat() if app.end_time else None,
                "best_snapshot_path": app.best_snapshot_path if app.best_snapshot_path and os.path.exists(app.best_snapshot_path) else None,  # Keep for reference
                "snapshot_url": app_snapshot_url  # Backend provides ready-to-use URL
            })
        
        return {
            "id": str(identity.id),
            "type": identity.type.value,
            "display_name": identity.display_name,
            "status": identity.status.value,
            "first_seen_at": identity.first_seen_at.isoformat(),
            "last_seen_at": identity.last_seen_at.isoformat(),
            "appearances_count": identity.appearances_count,
            "best_snapshot_path": best_snapshot_path,  # Keep original path for reference
            "snapshot_url": snapshot_url,  # Backend provides ready-to-use URL
            "pipeline_ids": pipeline_ids,  # List of pipeline IDs where this identity was seen
            "appearances": appearances_data,
            "embeddings_count": embeddings_count,
            "faces_count": faces_count
        }
    
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid identity ID: {identity_id}"
        )
    except Exception as e:
        logger.error(f"Error getting identity details: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to get identity details: {str(e)}"
        )


# =====================================================
# Promote Unknown to Known
# =====================================================

@router.post("/admin/unknown/{identity_id}/promote", summary="Promote Unknown to Known", description="Promote an unknown identity to known status with a display name (Admin or users with pipeline access)")
async def promote_unknown_to_known(
    identity_id: str,
    request: PromoteRequest,
    http_request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    Promote an unknown identity to known.
    - Admin: can promote any unknown identity
    - Regular users: can only promote unknown identities from their accessible pipelines
    """
    logger.info(f"[PROMOTE] ========================================")
    logger.info(f"[PROMOTE] 🚀 Starting promotion request")
    logger.info(f"[PROMOTE] User: {current_user.username} (ID: {current_user.id}, Role: {current_user.role})")
    logger.info(f"[PROMOTE] Identity ID: {identity_id}")
    logger.info(f"[PROMOTE] Requested display name: '{request.display_name}'")
    logger.info(f"[PROMOTE] Person code: {getattr(request, 'person_code', None)}")
    
    try:
        # Get identity_service dynamically (may be set during startup)
        logger.info(f"[PROMOTE] Step 1: Getting identity service...")
        identity_service = get_identity_service()
        if not identity_service:
            logger.error(f"[PROMOTE] ❌ Identity service not available")
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Identity service not available"
            )
        logger.info(f"[PROMOTE] ✅ Identity service available")
        logger.info(f"[PROMOTE]   Backend: {'pgvector' if identity_service.use_pgvector else 'faiss'}")
        
        # Validate display name (backend handles all validation)
        logger.info(f"[PROMOTE] Step 2: Validating display name...")
        if not request.display_name or not request.display_name.strip():
            logger.error(f"[PROMOTE] ❌ Display name is empty or whitespace")
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Display name is required and cannot be empty"
            )
        display_name_clean = request.display_name.strip()
        logger.info(f"[PROMOTE] ✅ Display name validated: '{display_name_clean}'")
        
        identity_uuid = uuid.UUID(identity_id)
        logger.info(f"[PROMOTE] Step 3: Parsed identity UUID: {identity_uuid}")
        
        # Check access for non-admin users
        logger.info(f"[PROMOTE] Step 4: Checking user access...")
        if current_user.role != "admin":
            from backend.auth.auth_service import check_identity_access
            has_access = await check_identity_access(identity_id, current_user, db)
            if not has_access:
                logger.warning(f"[PROMOTE] ❌ Access denied: User {current_user.username} does not have access to identity {identity_id}")
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Access denied to this identity"
                )
            logger.info(f"[PROMOTE] ✅ User has access to identity")
        else:
            logger.info(f"[PROMOTE] ✅ Admin user - has access to all identities")
        
        # Get identity from database to check current state
        logger.info(f"[PROMOTE] Step 5: Fetching identity from database...")
        identity_result = await db.execute(
            select(Identity).where(Identity.id == identity_uuid)
        )
        identity_before = identity_result.scalar_one_or_none()
        
        if not identity_before:
            logger.error(f"[PROMOTE] ❌ Identity {identity_id} not found in database")
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Identity {identity_id} not found"
            )
        
        logger.info(f"[PROMOTE] ✅ Identity found in database")
        logger.info(f"[PROMOTE]   Current type: {identity_before.type.value}")
        logger.info(f"[PROMOTE]   Current status: {identity_before.status.value}")
        logger.info(f"[PROMOTE]   Current display_name: {identity_before.display_name}")
        logger.info(f"[PROMOTE]   First seen: {identity_before.first_seen_at}")
        logger.info(f"[PROMOTE]   Last seen: {identity_before.last_seen_at}")
        logger.info(f"[PROMOTE]   Appearances count: {identity_before.appearances_count}")
        logger.info(f"[PROMOTE]   Best snapshot path: {identity_before.best_snapshot_path}")
        
        if identity_before.type != IdentityType.UNKNOWN:
            logger.error(f"[PROMOTE] ❌ Identity is not UNKNOWN (current type: {identity_before.type.value})")
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Identity is not unknown (current type: {identity_before.type.value})"
            )
        
        # Skip face detection validation - we already have a face detected when promoting
        # The identity was created from a detection that already had a face, so validation is unnecessary
        logger.info(f"[PROMOTE] Step 5.5: Skipping face detection validation (face already detected during identity creation)...")
        skip_face_validation = True  # Always skip for promotion since face was already detected
        
        if False:  # Disabled - kept for reference
            # Validate that at least one image for this identity contains a face
            logger.info(f"[PROMOTE] Step 5.5: Validating face detection in identity images...")
            try:
                import cv2
                import os
                from backend.core.model_manager import model_manager
                
                if not model_manager or not model_manager.detector:
                    logger.warning(f"[PROMOTE] ⚠️ Face detector not available, skipping face validation")
                else:
                    # Collect all available image paths for this identity
                    image_paths_to_check = []
                    
                    # 1. Best snapshot path (highest priority)
                    if identity_before.best_snapshot_path:
                        snapshot_path = identity_before.best_snapshot_path
                        if not os.path.isabs(snapshot_path):
                            storage_dir = settings.STORAGE_DIR
                            snapshot_path = os.path.join(storage_dir, snapshot_path.lstrip('storage/').lstrip('/'))
                        if os.path.exists(snapshot_path):
                            image_paths_to_check.append(('best_snapshot', snapshot_path))
                    
                    # 2. Get other snapshots from IdentityAppearance records
                    appearance_result = await db.execute(
                        select(IdentityAppearance.best_snapshot_path)
                        .where(
                            IdentityAppearance.identity_id == identity_uuid,
                            IdentityAppearance.best_snapshot_path.isnot(None)
                        )
                        .distinct()
                        .limit(10)
                    )
                    for row in appearance_result.scalars().all():
                        if row and row not in [path for _, path in image_paths_to_check]:
                            if not os.path.isabs(row):
                                storage_dir = settings.STORAGE_DIR
                                full_path = os.path.join(storage_dir, row.lstrip('storage/').lstrip('/'))
                            else:
                                full_path = row
                            if os.path.exists(full_path):
                                image_paths_to_check.append(('appearance', full_path))
                    
                    # 3. Get images from Face records
                    # PostgreSQL requires ORDER BY columns to be in SELECT when using DISTINCT
                    # So we select both columns, then extract unique paths in Python
                    face_result = await db.execute(
                        select(Face.face_image_path, Detection.timestamp)
                        .join(Detection, Face.detection_id == Detection.id)
                        .where(
                            Face.identity_id == identity_uuid,
                            Face.face_image_path.isnot(None)
                        )
                        .order_by(Detection.timestamp.desc())
                        .limit(50)  # Get more rows to ensure we have enough unique paths
                    )
                    # Extract unique paths (keeping first occurrence = most recent)
                    seen_paths = set()
                    for row in face_result.all():
                        face_path = row[0]  # face_image_path
                        if face_path and face_path not in seen_paths and face_path not in [path for _, path in image_paths_to_check]:
                            seen_paths.add(face_path)
                            if len(seen_paths) >= 10:  # Limit to 10 unique paths
                                break
                            if not os.path.isabs(face_path):
                                storage_dir = settings.STORAGE_DIR
                                full_path = os.path.join(storage_dir, face_path.lstrip('storage/').lstrip('/'))
                            else:
                                full_path = face_path
                            if os.path.exists(full_path):
                                image_paths_to_check.append(('face_record', full_path))
                    
                    logger.info(f"[PROMOTE]   Found {len(image_paths_to_check)} images to check for face detection")
                    
                    if not image_paths_to_check:
                        logger.warning(f"[PROMOTE] ⚠️ No images found for identity, skipping face validation")
                    else:
                        # Try each image until we find one with a face
                        face_found = False
                        valid_image_path = None
                        
                        for source, image_path in image_paths_to_check:
                            try:
                                logger.info(f"[PROMOTE]   Checking {source}: {image_path}")
                                
                                # Read image
                                image = cv2.imread(image_path)
                                if image is None:
                                    logger.debug(f"[PROMOTE]     ⚠️ Could not read image, skipping")
                                    continue
                                
                                # Detect face using SCRFD
                                bboxes, kpss = model_manager.detector.detect(image, max_num=1)
                                
                                if kpss is not None and len(kpss) > 0:
                                    confidence = bboxes[0][4] if len(bboxes) > 0 and len(bboxes[0]) > 4 else 'N/A'
                                    logger.info(f"[PROMOTE]     ✅ Face detected! (confidence: {confidence}, source: {source})")
                                    face_found = True
                                    valid_image_path = image_path
                                    
                                    # Update best_snapshot_path if we found a better image
                                    if source != 'best_snapshot' and valid_image_path != identity_before.best_snapshot_path:
                                        logger.info(f"[PROMOTE]     📸 Updating best_snapshot_path to image with detected face")
                                        # Convert to relative path for storage
                                        storage_dir = settings.STORAGE_DIR
                                        storage_dir_abs = os.path.abspath(storage_dir)
                                        if os.path.isabs(valid_image_path):
                                            valid_path_abs = os.path.abspath(valid_image_path)
                                            if valid_path_abs.startswith(storage_dir_abs):
                                                relative_path = os.path.relpath(valid_path_abs, storage_dir_abs)
                                                identity_before.best_snapshot_path = 'storage/' + relative_path.replace('\\', '/')
                                            else:
                                                identity_before.best_snapshot_path = valid_image_path
                                        else:
                                            identity_before.best_snapshot_path = valid_image_path
                                        # Flush (not commit) so the change is visible in the same transaction
                                        # The main promotion will commit everything together
                                        await db.flush()
                                        logger.info(f"[PROMOTE]     ✅ Updated best_snapshot_path to: {identity_before.best_snapshot_path} (flushed, will commit with promotion)")
                                    
                                    break  # Found a valid image, stop checking
                                else:
                                    logger.debug(f"[PROMOTE]     ❌ No face detected in {source}")
                            except Exception as e:
                                logger.debug(f"[PROMOTE]     ⚠️ Error checking {source}: {e}, continuing...")
                                continue
                        
                        if not face_found:
                            logger.warning(f"[PROMOTE] ⚠️ No face detected in any of {len(image_paths_to_check)} images for this identity")
                            # Rollback any pending changes (flushes) before raising error
                            try:
                                await db.rollback()
                            except Exception:
                                pass  # Ignore rollback errors
                            raise HTTPException(
                                status_code=status.HTTP_400_BAD_REQUEST,
                                detail="Promotion failed: No face detected in any of the identity's images. This may happen if the images were processed before face detection was enabled. Please ensure the identity has at least one image with a detectable face before promoting."
                            )
                        
                        logger.info(f"[PROMOTE] ✅ Face validation passed - found valid image: {valid_image_path}")
            except HTTPException:
                # Rollback any pending changes before re-raising HTTP exceptions
                try:
                    await db.rollback()
                except Exception:
                    pass  # Ignore rollback errors
                # Re-raise HTTP exceptions (our validation errors)
                raise
            except Exception as e:
                logger.error(f"[PROMOTE] ⚠️ Error during face detection validation: {e}", exc_info=True)
                # Rollback transaction to clear any aborted state before continuing
                try:
                    await db.rollback()
                except Exception as rollback_err:
                    logger.debug(f"[PROMOTE] Rollback error (may already be rolled back): {rollback_err}")
                # Don't block promotion if face detection fails due to technical issues
                logger.warning(f"[PROMOTE] ⚠️ Continuing with promotion despite face detection error")
        
        # Check embeddings before promotion
        logger.info(f"[PROMOTE] Step 6: Checking embeddings before promotion...")
        emb_before_result = await db.execute(
            select(func.count(IdentityEmbedding.id), func.count(IdentityEmbedding.id).filter(IdentityEmbedding.faiss_index_type == 'unknown'))
            .where(IdentityEmbedding.identity_id == identity_uuid)
        )
        emb_before_row = emb_before_result.first()
        total_embeddings_before = emb_before_row[0] or 0
        unknown_embeddings_before = emb_before_row[1] or 0
        
        logger.info(f"[PROMOTE]   Total embeddings: {total_embeddings_before}")
        logger.info(f"[PROMOTE]   UNKNOWN embeddings: {unknown_embeddings_before}")
        logger.info(f"[PROMOTE]   KNOWN embeddings: {total_embeddings_before - unknown_embeddings_before}")
        
        # Check Face records before promotion
        logger.info(f"[PROMOTE] Step 7: Checking Face records before promotion...")
        face_before_result = await db.execute(
            select(func.count(Face.id)).where(Face.identity_id == identity_uuid)
        )
        face_count_before = face_before_result.scalar() or 0
        logger.info(f"[PROMOTE]   Total Face records: {face_count_before}")
        
        # Promote identity
        logger.info(f"[PROMOTE] Step 8: Calling identity_service.promote_unknown_to_known()...")
        identity = await identity_service.promote_unknown_to_known(
            identity_id=identity_uuid,
            display_name=display_name_clean,
            user_id=current_user.id,
            db=db
        )
        logger.info(f"[PROMOTE] ✅ Promotion function completed")
        
        # Check identity after promotion
        await db.refresh(identity)
        logger.info(f"[PROMOTE] Step 9: Verifying identity changes...")
        logger.info(f"[PROMOTE]   ✅ Type changed: {identity_before.type.value} → {identity.type.value}")
        logger.info(f"[PROMOTE]   ✅ Status changed: {identity_before.status.value} → {identity.status.value}")
        logger.info(f"[PROMOTE]   ✅ Display name changed: '{identity_before.display_name}' → '{identity.display_name}'")
        logger.info(f"[PROMOTE]   Updated at: {identity.updated_at}")
        logger.info(f"[PROMOTE]   Best snapshot path: {identity.best_snapshot_path}")
        
        # Check embeddings after promotion
        logger.info(f"[PROMOTE] Step 10: Verifying embedding changes...")
        emb_after_result = await db.execute(
            select(func.count(IdentityEmbedding.id), func.count(IdentityEmbedding.id).filter(IdentityEmbedding.faiss_index_type == 'known'))
            .where(IdentityEmbedding.identity_id == identity_uuid)
        )
        emb_after_row = emb_after_result.first()
        total_embeddings_after = emb_after_row[0] or 0
        known_embeddings_after = emb_after_row[1] or 0
        
        logger.info(f"[PROMOTE]   Total embeddings: {total_embeddings_after} (was {total_embeddings_before})")
        logger.info(f"[PROMOTE]   KNOWN embeddings: {known_embeddings_after} (was {total_embeddings_before - unknown_embeddings_before})")
        logger.info(f"[PROMOTE]   UNKNOWN embeddings: {total_embeddings_after - known_embeddings_after} (was {unknown_embeddings_before})")
        
        if known_embeddings_after == 0:
            logger.warning(f"[PROMOTE] ⚠️ WARNING: No KNOWN embeddings found after promotion!")
        else:
            logger.info(f"[PROMOTE]   ✅ {unknown_embeddings_before} embeddings moved from UNKNOWN to KNOWN")
        
        # Check Face records after promotion
        logger.info(f"[PROMOTE] Step 11: Verifying Face record changes...")
        face_after_result = await db.execute(
            select(func.count(Face.id), func.count(Face.id).filter(Face.name == display_name_clean))
            .where(Face.identity_id == identity_uuid)
        )
        face_after_row = face_after_result.first()
        face_count_after = face_after_row[0] or 0
        faces_with_name = face_after_row[1] or 0
        
        logger.info(f"[PROMOTE]   Total Face records: {face_count_after} (was {face_count_before})")
        logger.info(f"[PROMOTE]   Faces with name '{display_name_clean}': {faces_with_name}")
        if faces_with_name == face_count_after:
            logger.info(f"[PROMOTE]   ✅ All {face_count_after} Face records updated with new name")
        else:
            logger.warning(f"[PROMOTE]   ⚠️ Only {faces_with_name}/{face_count_after} Face records have the new name")
        
        # Save identity index (FAISS only)
        if identity_index:
            logger.info(f"[PROMOTE] Step 12: Saving FAISS indexes to disk...")
            identity_index.save()
            logger.info(f"[PROMOTE]   ✅ Indexes saved to disk")
        else:
            logger.info(f"[PROMOTE] Step 12: No FAISS index to save (using pgvector)")
        
        await db.commit()
        logger.info(f"[PROMOTE] Step 13: Database transaction committed")
        
        logger.info(f"[PROMOTE] ✅✅✅ Promotion successful!")
        logger.info(f"[PROMOTE]   Identity: {identity_id}")
        logger.info(f"[PROMOTE]   New name: '{display_name_clean}'")
        logger.info(f"[PROMOTE]   Type: {identity.type.value}")
        logger.info(f"[PROMOTE]   Status: {identity.status.value}")
        logger.info(f"[PROMOTE]   Embeddings: {known_embeddings_after} KNOWN, {total_embeddings_after - known_embeddings_after} UNKNOWN")
        logger.info(f"[PROMOTE]   Face records: {face_count_after} updated")
        logger.info(f"[PROMOTE] ========================================")
        
        return {
            "success": True,
            "message": f"Identity promoted to known with name: {display_name_clean}",
            "identity": {
                "id": str(identity.id),
                "type": identity.type.value,
                "display_name": identity.display_name,
                "status": identity.status.value
            }
        }
    
    except ValueError as e:
        # Rollback before raising
        try:
            await db.rollback()
        except Exception:
            pass  # Ignore rollback errors
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e)
        )
    except HTTPException:
        # Re-raise HTTP exceptions (already handled above with rollback)
        raise
    except Exception as e:
        logger.error(f"Error promoting identity: {e}", exc_info=True)
        
        # Rollback transaction first to clear any aborted state
        try:
            await db.rollback()
        except Exception as rollback_error:
            logger.debug(f"[PROMOTE] Error during rollback (may already be rolled back): {rollback_error}")
        
        # Log error to audit (in a fresh transaction state)
        try:
            ip_address, user_agent = get_client_info(http_request)
            await IdentityAuditLogger.log_error(
                db=db,
                user_id=current_user.id,
                username=current_user.username,
                action_type="promote",
                error_message=str(e),
                identity_id=uuid.UUID(identity_id) if identity_id else None,
                ip_address=ip_address,
                user_agent=user_agent
            )
            # Commit audit log separately
            try:
                await db.commit()
            except Exception as commit_error:
                logger.debug(f"[PROMOTE] Error committing audit log (non-critical): {commit_error}")
        except Exception as audit_error:
            logger.error(f"Failed to log audit error: {audit_error}")
        
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to promote identity: {str(e)}"
        )


# =====================================================
# List All Identities (for Intelligence Analysis)
# =====================================================

async def _batch_pipeline_ids(db: AsyncSession, identity_ids: list) -> dict:
    """Batched (no N+1) pipeline id lookup for a page of identities."""
    pipeline_map = {iid: set() for iid in identity_ids}
    if not identity_ids:
        return pipeline_map
    appearance_result = await db.execute(
        select(IdentityAppearance.identity_id, IdentityAppearance.pipeline_id)
        .where(IdentityAppearance.identity_id.in_(identity_ids))
        .distinct()
    )
    for iid, pid in appearance_result:
        if pid:
            pipeline_map.setdefault(iid, set()).add(pid)
    # Fallback batch for identities without appearance rows
    missing = [iid for iid, pids in pipeline_map.items() if not pids]
    if missing:
        embedding_result = await db.execute(
            select(IdentityEmbedding.identity_id, IdentityEmbedding.pipeline_id)
            .where(IdentityEmbedding.identity_id.in_(missing))
            .distinct()
        )
        for iid, pid in embedding_result:
            if pid:
                pipeline_map.setdefault(iid, set()).add(pid)
    return pipeline_map


def _identity_iso_z(dt) -> Optional[str]:
    if dt is None:
        return None
    s = dt.isoformat()
    return s + "Z" if dt.tzinfo is None else s.replace("+00:00", "Z")


@router.get("/admin/identities", summary="List / Search Identities", description="List identities for Intelligence Analysis. Pass 'page' for the server-side paginated search mode - Admin only")
async def list_all_identities(
    limit: int = Query(100, ge=1, le=1000, description="Legacy mode: maximum identities to return"),
    identity_type: Optional[str] = Query(None, alias="type", description="Filter by type: 'known', 'unknown', or 'both' (default: both)"),
    q: Optional[str] = Query(None, max_length=200, description="Search by display name or identity id prefix"),
    page: Optional[int] = Query(None, ge=1, description="Page number — presence switches to the paginated envelope"),
    page_size: int = Query(25, ge=1, le=100, description="Results per page (paginated mode)"),
    pipeline_id: Optional[str] = Query(None, max_length=200, description="Only identities seen on this pipeline"),
    last_seen_within_days: Optional[int] = Query(None, ge=1, le=3650, description="Rolling window: last seen within N days (UTC)"),
    sort_by: str = Query("last_seen_at", description="Sort field: last_seen_at, first_seen_at, display_name, appearances_count"),
    sort_order: str = Query("desc", description="asc or desc"),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_role(["admin"]))
):
    """
    List identities for the Intelligence Analysis dropdown.

    Two modes:
    - Legacy (no `page` param): `{identities, total, limit, type_filter}` capped by `limit`.
    - Paginated (`page` given): server-side search/filter with
      `{items, total, page, page_size, total_pages}` — the browser never
      downloads the full identity population.
    """
    try:
        from sqlalchemy import String as SAString, cast as sa_cast

        filters = [Identity.status == IdentityStatus.ACTIVE]

        if identity_type and identity_type.lower() != "both":
            if identity_type.lower() == "known":
                filters.append(Identity.type == IdentityType.KNOWN)
            elif identity_type.lower() == "unknown":
                filters.append(Identity.type == IdentityType.UNKNOWN)

        if q:
            # Escape LIKE wildcards so user input is a literal, not a pattern
            escaped = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            filters.append(or_(
                Identity.display_name.ilike(f"%{escaped}%", escape="\\"),
                sa_cast(Identity.id, SAString).ilike(f"{escaped}%", escape="\\"),
            ))

        if pipeline_id:
            filters.append(
                select(IdentityAppearance.id)
                .where(and_(
                    IdentityAppearance.identity_id == Identity.id,
                    IdentityAppearance.pipeline_id == pipeline_id,
                ))
                .exists()
            )

        if last_seen_within_days:
            now = datetime.utcnow()
            cutoff = now - timedelta(days=last_seen_within_days)
            # Rolling UTC window; future timestamps (bad camera clocks) are
            # not treated as "recent" beyond a small skew allowance.
            filters.append(Identity.last_seen_at >= cutoff)
            filters.append(Identity.last_seen_at <= now + timedelta(minutes=5))

        sort_columns = {
            "last_seen_at": Identity.last_seen_at,
            "first_seen_at": Identity.first_seen_at,
            "display_name": Identity.display_name,
            "appearances_count": Identity.appearances_count,
        }
        sort_col = sort_columns.get(sort_by, Identity.last_seen_at)
        order = sort_col.asc().nulls_last() if sort_order.lower() == "asc" else sort_col.desc().nulls_last()

        base = select(Identity).where(and_(*filters)).order_by(order)

        paginated_mode = page is not None
        if paginated_mode:
            total = (await db.execute(
                select(func.count()).select_from(Identity).where(and_(*filters))
            )).scalar() or 0
            query = base.offset((page - 1) * page_size).limit(page_size)
        else:
            query = base.limit(limit)

        result = await db.execute(query)
        identities = result.scalars().all()

        pipeline_map = await _batch_pipeline_ids(db, [i.id for i in identities])

        from backend.utils.path_utils import path_to_url
        storage_dir = settings.STORAGE_DIR
        identity_list = [
            {
                "id": str(identity.id),
                "display_name": identity.display_name,
                "type": identity.type.value,
                "status": identity.status.value,
                "appearances_count": identity.appearances_count,
                "first_seen_at": _identity_iso_z(identity.first_seen_at),
                "last_seen_at": _identity_iso_z(identity.last_seen_at),
                "best_snapshot_path": identity.best_snapshot_path,
                "snapshot_url": path_to_url(identity.best_snapshot_path, storage_dir) if identity.best_snapshot_path else None,
                "pipeline_ids": sorted(pipeline_map.get(identity.id, set())),
            }
            for identity in identities
        ]

        if paginated_mode:
            total_pages = max(1, (total + page_size - 1) // page_size)
            return {
                "items": identity_list,
                "total": total,
                "page": page,
                "page_size": page_size,
                "total_pages": total_pages,
            }

        return {
            "identities": identity_list,
            "total": len(identity_list),
            "limit": limit,
            "type_filter": identity_type or "both"
        }

    except Exception as e:
        logger.error(f"Error listing identities: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to list identities"
        )


# =====================================================
# Search Identities (for merge form)
# =====================================================

@router.get("/admin/identities/search", summary="Search Identities", description="Search for identities by ID or display name (for merge form) - Admin or users with pipeline access")
async def search_identities(
    query: str = Query(..., description="Search query (identity ID or display name)"),
    limit: int = Query(10, ge=1, le=50, description="Maximum number of results"),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    Search for identities by ID or display name.
    Returns identities that match the search query.
    - Admin: can search all identities
    - Regular users: only see identities from their accessible pipelines
    
    **Search behavior:**
    - If query is a valid UUID, searches by exact ID match
    - Otherwise, searches by display name (case-insensitive partial match)
    - Returns up to `limit` results
    """
    try:
        # Note: We don't need identity_service for searching - we query the database directly
        # Get user pipelines for access control
        user_pipelines = []
        if current_user.role != "admin":
            from backend.auth.auth_service import AuthService
            user_pipelines = await AuthService.get_user_pipelines(current_user.id, db)
            if not user_pipelines:
                return {
                    "results": [],
                    "total": 0,
                    "query": query
                }
        
        # Try to parse as UUID (exact ID search)
        try:
            search_uuid = uuid.UUID(query)
            # Search by exact ID
            result = await db.execute(
                select(Identity).where(Identity.id == search_uuid)
            )
            identity = result.scalar_one_or_none()
            
            if identity:
                # Check access for non-admin users
                if current_user.role != "admin":
                    from backend.auth.auth_service import check_identity_access
                    has_access = await check_identity_access(str(identity.id), current_user, db)
                    if not has_access:
                        return {
                            "results": [],
                            "total": 0,
                            "query": query
                        }
                
                # Get best snapshot path
                best_snapshot_path = identity.best_snapshot_path
                if best_snapshot_path:
                    storage_dir = settings.STORAGE_DIR
                    storage_dir_abs = os.path.abspath(storage_dir)
                    if os.path.isabs(best_snapshot_path):
                        best_snapshot_path_abs = os.path.abspath(best_snapshot_path)
                        if best_snapshot_path_abs.startswith(storage_dir_abs):
                            relative_path = os.path.relpath(best_snapshot_path_abs, storage_dir_abs)
                            best_snapshot_path = 'storage/' + relative_path.replace('\\', '/')
                    elif not best_snapshot_path.startswith('storage/'):
                        best_snapshot_path = 'storage/' + best_snapshot_path.lstrip('/')
                
                # Backend constructs snapshot URL (all logic in backend)
                snapshot_url = None
                if best_snapshot_path:
                    if best_snapshot_path.startswith('storage/'):
                        snapshot_url = f"/{best_snapshot_path}"
                    elif not best_snapshot_path.startswith('/'):
                        snapshot_url = f"/storage/{best_snapshot_path}"
                    else:
                        snapshot_url = best_snapshot_path
                else:
                    snapshot_url = 'data:image/svg+xml,%3Csvg xmlns=%27http://www.w3.org/2000/svg%27 width=%27100%27 height=%27100%27%3E%3Crect fill=%27%23333%27 width=%27100%27 height=%27100%27/%3E%3Ccircle cx=%2750%27 cy=%2735%27 r=%2715%27 fill=%27%23999%27/%3E%3Cpath d=%27M 25 70 Q 25 60 35 60 L 65 60 Q 75 60 75 70 L 75 85 L 25 85 Z%27 fill=%27%23999%27/%3E%3C/svg%3E'
                
                return {
                    "results": [{
                        "id": str(identity.id),
                        "display_name": identity.display_name,
                        "type": identity.type.value,
                        "status": identity.status.value,
                        "appearances_count": identity.appearances_count,
                        "first_seen_at": identity.first_seen_at.isoformat(),
                        "best_snapshot_path": best_snapshot_path,  # Keep for reference
                        "snapshot_url": snapshot_url  # Backend provides ready-to-use URL
                    }],
                    "total": 1,
                    "query": query
                }
        except ValueError:
            # Not a valid UUID, search by display name
            pass
        
        # Search by display name (case-insensitive partial match)
        query_lower = query.lower()
        
        # Build base query
        base_query = select(Identity).where(
            Identity.display_name.ilike(f"%{query_lower}%")
        ).limit(limit)
        
        # For non-admin users, filter by accessible pipelines
        if current_user.role != "admin":
            # Get identities that have appearances/embeddings in user's pipelines
            # IdentityAppearance, IdentityEmbedding, Face, Detection are already imported at the top
            
            # Get identity IDs from user's pipelines
            accessible_identity_ids = set()
            
            # From IdentityAppearance
            appearance_result = await db.execute(
                select(IdentityAppearance.identity_id).where(
                    IdentityAppearance.pipeline_id.in_(user_pipelines)
                ).distinct()
            )
            for row in appearance_result:
                accessible_identity_ids.add(row[0])
            
            # From IdentityEmbedding
            embedding_result = await db.execute(
                select(IdentityEmbedding.identity_id).where(
                    IdentityEmbedding.pipeline_id.in_(user_pipelines)
                ).distinct()
            )
            for row in embedding_result:
                accessible_identity_ids.add(row[0])
            
            # From Face->Detection
            face_result = await db.execute(
                select(Face.identity_id).join(
                    Detection, Face.detection_id == Detection.id
                ).where(
                    Detection.pipeline_id.in_(user_pipelines),
                    Face.identity_id.isnot(None)
                ).distinct()
            )
            for row in face_result:
                if row[0]:
                    accessible_identity_ids.add(row[0])
            
            if not accessible_identity_ids:
                return {
                    "results": [],
                    "total": 0,
                    "query": query
                }
            
            # Filter by accessible identity IDs
            base_query = base_query.where(Identity.id.in_(list(accessible_identity_ids)))
        
        # Execute query
        result = await db.execute(base_query)
        identities = result.scalars().all()
        
        # Format results
        results = []
        storage_dir = settings.STORAGE_DIR
        storage_dir_abs = os.path.abspath(storage_dir)
        
        for identity in identities:
            best_snapshot_path = identity.best_snapshot_path
            if best_snapshot_path:
                if os.path.isabs(best_snapshot_path):
                    best_snapshot_path_abs = os.path.abspath(best_snapshot_path)
                    if best_snapshot_path_abs.startswith(storage_dir_abs):
                        relative_path = os.path.relpath(best_snapshot_path_abs, storage_dir_abs)
                        best_snapshot_path = 'storage/' + relative_path.replace('\\', '/')
                elif not best_snapshot_path.startswith('storage/'):
                    best_snapshot_path = 'storage/' + best_snapshot_path.lstrip('/')
            
            # Backend constructs snapshot URL (all logic in backend)
            snapshot_url = None
            if best_snapshot_path:
                if best_snapshot_path.startswith('storage/'):
                    snapshot_url = f"/{best_snapshot_path}"
                elif not best_snapshot_path.startswith('/'):
                    snapshot_url = f"/storage/{best_snapshot_path}"
                else:
                    snapshot_url = best_snapshot_path
            else:
                snapshot_url = 'data:image/svg+xml,%3Csvg xmlns=%27http://www.w3.org/2000/svg%27 width=%27100%27 height=%27100%27%3E%3Crect fill=%27%23333%27 width=%27100%27 height=%27100%27/%3E%3Ccircle cx=%2750%27 cy=%2735%27 r=%2715%27 fill=%27%23999%27/%3E%3Cpath d=%27M 25 70 Q 25 60 35 60 L 65 60 Q 75 60 75 70 L 75 85 L 25 85 Z%27 fill=%27%23999%27/%3E%3C/svg%3E'
            
            results.append({
                "id": str(identity.id),
                "display_name": identity.display_name,
                "type": identity.type.value,
                "status": identity.status.value,
                "appearances_count": identity.appearances_count,
                "first_seen_at": identity.first_seen_at.isoformat(),
                "best_snapshot_path": best_snapshot_path,  # Keep for reference
                "snapshot_url": snapshot_url  # Backend provides ready-to-use URL
            })
        
        return {
            "results": results,
            "total": len(results),
            "query": query
        }
    
    except Exception as e:
        logger.error(f"Error searching identities: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to search identities: {str(e)}"
        )


# =====================================================
# Merge Identities
# =====================================================

@router.post("/admin/identities/merge", summary="Merge Identities", description="Merge two identities into one (Admin or users with pipeline access)")
async def merge_identities(
    request: MergeRequest,
    http_request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    Merge two identities into one.
    - Admin: can merge any identities
    - Regular users: can only merge identities from their accessible pipelines
    """
    try:
        # Get identity_service dynamically (may be set during startup)
        identity_service = get_identity_service()
        if not identity_service:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Identity service not available"
            )
        
        # Validate merge request (backend handles all validation)
        if not request.from_identity_id or not request.from_identity_id.strip():
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Source identity ID is required"
            )
        
        if not request.to_identity_id or not request.to_identity_id.strip():
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Target identity ID is required"
            )
        
        if request.from_identity_id.strip() == request.to_identity_id.strip():
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Cannot merge identity with itself"
            )
        
        from_uuid = uuid.UUID(request.from_identity_id)
        to_uuid = uuid.UUID(request.to_identity_id)
        
        # Get identities before merge for audit
        from_result = await db.execute(
            select(Identity).where(Identity.id == from_uuid)
        )
        from_identity = from_result.scalar_one_or_none()
        
        to_result = await db.execute(
            select(Identity).where(Identity.id == to_uuid)
        )
        to_identity = to_result.scalar_one_or_none()
        
        if not from_identity or not to_identity:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="One or both identities not found"
            )
        
        # Check access for non-admin users
        if current_user.role != "admin":
            from backend.auth.auth_service import check_identity_access
            has_access_from = await check_identity_access(request.from_identity_id, current_user, db)
            has_access_to = await check_identity_access(request.to_identity_id, current_user, db)
            if not (has_access_from and has_access_to):
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Access denied to one or both identities"
                )
        
        # Capture before state for audit
        before_state = {
            "from_identity": {
                "id": str(from_identity.id),
                "type": from_identity.type.value,
                "status": from_identity.status.value,
                "display_name": from_identity.display_name,
                "appearances_count": from_identity.appearances_count
            },
            "to_identity": {
                "id": str(to_identity.id),
                "type": to_identity.type.value,
                "status": to_identity.status.value,
                "display_name": to_identity.display_name,
                "appearances_count": to_identity.appearances_count
            }
        }
        
        # Get client info for audit
        ip_address, user_agent = get_client_info(http_request)
        
        # Get identity_service dynamically
        identity_service = get_identity_service()
        
        # Merge identities
        merged_identity = await identity_service.merge_identities(
            from_identity_id=from_uuid,
            to_identity_id=to_uuid,
            user_id=current_user.id,
            notes=request.notes,
            db=db
        )
        
        # Capture after state for audit
        after_state = {
            "merged_identity": {
                "id": str(merged_identity.id),
                "type": merged_identity.type.value,
                "status": merged_identity.status.value,
                "display_name": merged_identity.display_name,
                "appearances_count": merged_identity.appearances_count
            }
        }
        
        # Save identity index
        if identity_index:
            identity_index.save()
        
        await db.commit()
        
        # Log audit entry
        await IdentityAuditLogger.log_merge(
            db=db,
            user_id=current_user.id,
            username=current_user.username,
            from_identity_id=from_uuid,
            to_identity_id=to_uuid,
            before_state=before_state,
            after_state=after_state,
            ip_address=ip_address,
            user_agent=user_agent,
            notes=request.notes
        )
        await db.commit()
        
        logger.info(f"Admin {current_user.username} merged identity {request.from_identity_id} into {request.to_identity_id}")
        
        return {
            "success": True,
            "message": "Identities merged successfully",
            "identity": {
                "id": str(merged_identity.id),
                "type": merged_identity.type.value,
                "display_name": merged_identity.display_name,
                "status": merged_identity.status.value
            }
        }
    
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e)
        )
    except Exception as e:
        logger.error(f"Error merging identities: {e}", exc_info=True)
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to merge identities: {str(e)}"
        )


# =====================================================
# Merge Preview (Production Feature)
# =====================================================

@router.post("/admin/identities/merge-preview", response_model=MergePreviewResponse, summary="Preview Merge", description="Preview what will happen when merging identities - shows target selection, type promotion, pipeline distribution (Admin or users with pipeline access)")
async def preview_merge(
    request: MergePreviewRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    Production-grade merge preview endpoint.
    
    Shows detailed information about what will happen when merging identities:
    - Target identity selection (with scoring breakdown)
    - Type promotion (UNKNOWN + KNOWN → KNOWN)
    - Best snapshot selection
    - Pipeline distribution
    - Warnings for cross-pipeline merges
    
    Use this before actual merge to review changes.
    """
    try:
        # Validate request
        if not request.identity_ids or len(request.identity_ids) < 2:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="At least 2 identity IDs required for preview"
            )
        
        # Remove duplicates and validate UUIDs
        unique_ids = list(set(request.identity_ids))
        if len(unique_ids) < 2:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="At least 2 unique identity IDs required"
            )
        
        identity_uuids = []
        for id_str in unique_ids:
            try:
                identity_uuids.append(uuid.UUID(id_str))
            except ValueError:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Invalid identity ID format: {id_str}"
                )
        
        # Fetch all identities
        result = await db.execute(
            select(Identity).where(Identity.id.in_(identity_uuids))
        )
        identities = {id.id: id for id in result.scalars().all()}
        
        if len(identities) != len(identity_uuids):
            missing = set(identity_uuids) - set(identities.keys())
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Some identities not found: {[str(id) for id in missing]}"
            )
        
        # Check access for non-admin users
        if current_user.role != "admin":
            from backend.auth.auth_service import check_identity_access
            for identity_id in identity_uuids:
                has_access = await check_identity_access(str(identity_id), current_user, db)
                if not has_access:
                    raise HTTPException(
                        status_code=status.HTTP_403_FORBIDDEN,
                        detail=f"Access denied to identity {identity_id}"
                    )
        
        # Get identity service for target selection
        identity_service = get_identity_service()
        if not identity_service:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Identity service not available"
            )
        
        # Determine target identity using production scoring
        target_uuid = None
        selection_details = None
        
        if request.target_identity_id:
            try:
                target_uuid = uuid.UUID(request.target_identity_id)
                if target_uuid not in identity_uuids:
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail="Target identity ID must be in the merge list"
                    )
            except ValueError:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Invalid target identity ID format: {request.target_identity_id}"
                )
        else:
            # Auto-select best identity with production scoring
            target_uuid, selection_details = await identity_service.find_best_identity(identity_uuids, db)
        
        target_identity = identities[target_uuid]
        source_identities = [identities[id] for id in identity_uuids if id != target_uuid]
        
        # Gather pipeline information for all identities
        pipeline_distribution = {}
        all_pipelines = set()
        
        for identity in identities.values():
            pipeline_result = await db.execute(
                select(IdentityAppearance.pipeline_id)
                .where(IdentityAppearance.identity_id == identity.id)
                .distinct()
            )
            pipelines = [row[0] for row in pipeline_result if row[0]]
            
            # Fallback to embeddings
            if not pipelines:
                emb_result = await db.execute(
                    select(IdentityEmbedding.pipeline_id)
                    .where(IdentityEmbedding.identity_id == identity.id)
                    .distinct()
                )
                pipelines = [row[0] for row in emb_result if row[0]]
            
            pipeline_distribution[str(identity.id)] = {
                "pipelines": pipelines,
                "count": len(pipelines),
                "display_name": identity.display_name,
                "type": identity.type.value
            }
            all_pipelines.update(pipelines)
        
        # Predict type promotion
        type_promotion = {
            "will_change": False,
            "from_type": target_identity.type.value,
            "to_type": target_identity.type.value,
            "reason": None,
            "inherited_name": None
        }
        
        if target_identity.type == IdentityType.UNKNOWN:
            for source in source_identities:
                if source.type == IdentityType.KNOWN:
                    type_promotion = {
                        "will_change": True,
                        "from_type": "unknown",
                        "to_type": "known",
                        "reason": f"Source identity {source.id} is KNOWN",
                        "inherited_name": source.display_name if not target_identity.display_name else None
                    }
                    break
        
        # Predict best snapshot selection
        best_snapshot = {
            "current_path": target_identity.best_snapshot_path,
            "will_change": False,
            "new_source": None,
            "new_path": None
        }
        
        # Get quality scores from embeddings
        target_quality = 0.0
        target_emb_result = await db.execute(
            select(IdentityEmbedding)
            .where(IdentityEmbedding.identity_id == target_uuid)
            .order_by(IdentityEmbedding.quality.desc().nullslast())
            .limit(1)
        )
        target_emb = target_emb_result.scalar_one_or_none()
        if target_emb and target_emb.quality:
            target_quality = target_emb.quality
        
        for source in source_identities:
            if source.best_snapshot_path:
                source_emb_result = await db.execute(
                    select(IdentityEmbedding)
                    .where(IdentityEmbedding.identity_id == source.id)
                    .order_by(IdentityEmbedding.quality.desc().nullslast())
                    .limit(1)
                )
                source_emb = source_emb_result.scalar_one_or_none()
                source_quality = source_emb.quality if source_emb and source_emb.quality else 0.0
                
                if source_quality > target_quality:
                    best_snapshot = {
                        "current_path": target_identity.best_snapshot_path,
                        "will_change": True,
                        "new_source": str(source.id),
                        "new_path": source.best_snapshot_path,
                        "quality_improvement": f"{target_quality:.3f} → {source_quality:.3f}"
                    }
                    target_quality = source_quality
        
        # Calculate statistics
        total_appearances = sum(id.appearances_count for id in identities.values())
        total_embeddings = 0
        for identity in identities.values():
            emb_count = (await db.execute(
                select(func.count(IdentityEmbedding.id))
                .where(IdentityEmbedding.identity_id == identity.id)
            )).scalar() or 0
            total_embeddings += emb_count
        
        statistics = {
            "total_identities": len(identities),
            "total_appearances": total_appearances,
            "total_embeddings": total_embeddings,
            "total_pipelines": len(all_pipelines),
            "pipeline_list": list(all_pipelines)
        }
        
        # Generate warnings
        warnings = []
        
        if len(all_pipelines) > 1:
            warnings.append(f"⚠️ Cross-pipeline merge: Identities span {len(all_pipelines)} different pipelines ({', '.join(all_pipelines)})")
        
        if type_promotion["will_change"]:
            warnings.append(f"ℹ️ Type promotion: Target will be promoted from UNKNOWN to KNOWN")
        
        if best_snapshot["will_change"]:
            warnings.append(f"ℹ️ Snapshot update: A better quality snapshot will be used from source identity")
        
        # Check for mixed types
        known_count = sum(1 for id in identities.values() if id.type == IdentityType.KNOWN)
        unknown_count = len(identities) - known_count
        if known_count > 0 and unknown_count > 0:
            warnings.append(f"ℹ️ Mixed types: Merging {known_count} KNOWN and {unknown_count} UNKNOWN identities")
        
        # Check for merged identities
        merged_count = sum(1 for id in identities.values() if id.status == IdentityStatus.MERGED)
        if merged_count > 0:
            warnings.append(f"❌ Invalid: {merged_count} identities are already merged and cannot be merged again")
        
        # Backend constructs snapshot URLs for target identity (all logic in backend)
        target_snapshot_url = None
        if target_identity.best_snapshot_path:
            if target_identity.best_snapshot_path.startswith('storage/'):
                target_snapshot_url = f"/{target_identity.best_snapshot_path}"
            elif not target_identity.best_snapshot_path.startswith('/'):
                target_snapshot_url = f"/storage/{target_identity.best_snapshot_path}"
            else:
                target_snapshot_url = target_identity.best_snapshot_path
        else:
            target_snapshot_url = 'data:image/svg+xml,%3Csvg xmlns="http://www.w3.org/2000/svg" width="100" height="100"%3E%3Crect fill="%23333" width="100" height="100"/%3E%3Ctext fill="%23999" x="50" y="50" text-anchor="middle" dominant-baseline="middle" font-size="40"%3E%3F%3C/text%3E%3C/svg%3E'
        
        # Build response
        return {
            "success": True,
            "target_identity": {
                "id": str(target_identity.id),
                "type": target_identity.type.value,
                "status": target_identity.status.value,
                "display_name": target_identity.display_name,
                "appearances_count": target_identity.appearances_count,
                "best_snapshot_path": target_identity.best_snapshot_path,  # Keep for reference
                "snapshot_url": target_snapshot_url,  # Backend provides ready-to-use URL
                "pipelines": pipeline_distribution.get(str(target_identity.id), {}).get("pipelines", []),
                "auto_selected": request.target_identity_id is None
            },
            "source_identities": [
                {
                    "id": str(source.id),
                    "type": source.type.value,
                    "status": source.status.value,
                    "display_name": source.display_name,
                    "appearances_count": source.appearances_count,
                    "best_snapshot_path": source.best_snapshot_path,  # Keep for reference
                    "snapshot_url": (
                        f"/{source.best_snapshot_path}" if source.best_snapshot_path and source.best_snapshot_path.startswith('storage/') else
                        f"/storage/{source.best_snapshot_path}" if source.best_snapshot_path and not source.best_snapshot_path.startswith('/') else
                        source.best_snapshot_path if source.best_snapshot_path else
                        'data:image/svg+xml,%3Csvg xmlns="http://www.w3.org/2000/svg" width="100" height="100"%3E%3Crect fill="%23333" width="100" height="100"/%3E%3Ctext fill="%23999" x="50" y="50" text-anchor="middle" dominant-baseline="middle" font-size="40"%3E%3F%3C/text%3E%3C/svg%3E'
                    ),  # Backend provides ready-to-use URL
                    "pipelines": pipeline_distribution.get(str(source.id), {}).get("pipelines", [])
                }
                for source in source_identities
            ],
            "pipeline_distribution": pipeline_distribution,
            "type_promotion": type_promotion,
            "snapshot_selection": best_snapshot,
            "statistics": statistics,
            "warnings": warnings,
            "selection_details": selection_details
        }
    
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error generating merge preview: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to generate merge preview: {str(e)}"
        )


@router.post("/admin/identities/merge-multiple", summary="Merge Multiple Identities", description="Merge multiple identities into one efficiently (Admin or users with pipeline access)")
async def merge_multiple_identities(
    request: MergeMultipleRequest,
    http_request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    Merge multiple identities into one efficiently.
    
    Smart approach:
    - If target_identity_id is provided, merge all others into it
    - Otherwise, automatically finds the best identity (most appearances, best quality snapshot)
    - Time complexity: O(n) where n = number of identities
    
    - Admin: can merge any identities
    - Regular users: can only merge identities from their accessible pipelines
    """
    try:
        # Get identity_service dynamically (may be set during startup)
        identity_service = get_identity_service()
        if not identity_service:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Identity service not available"
            )
        
        # Validate request
        if not request.identity_ids or len(request.identity_ids) < 2:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="At least 2 identity IDs required"
            )
        
        # Remove duplicates and validate UUIDs
        unique_ids = list(set(request.identity_ids))
        if len(unique_ids) < 2:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="At least 2 unique identity IDs required"
            )
        
        identity_uuids = []
        for id_str in unique_ids:
            try:
                identity_uuids.append(uuid.UUID(id_str))
            except ValueError:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Invalid identity ID format: {id_str}"
                )
        
        target_uuid = None
        if request.target_identity_id:
            try:
                target_uuid = uuid.UUID(request.target_identity_id)
                if target_uuid not in identity_uuids:
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail="Target identity ID must be in the merge list"
                    )
            except ValueError:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Invalid target identity ID format: {request.target_identity_id}"
                )
        
        # Get all identities before merge for audit
        result = await db.execute(
            select(Identity).where(Identity.id.in_(identity_uuids))
        )
        identities_before = {id.id: id for id in result.scalars().all()}
        
        if len(identities_before) != len(identity_uuids):
            missing = set(identity_uuids) - set(identities_before.keys())
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Some identities not found: {[str(id) for id in missing]}"
            )
        
        # Check access for non-admin users
        if current_user.role != "admin":
            from backend.auth.auth_service import check_identity_access
            for identity_id in identity_uuids:
                has_access = await check_identity_access(str(identity_id), current_user, db)
                if not has_access:
                    raise HTTPException(
                        status_code=status.HTTP_403_FORBIDDEN,
                        detail=f"Access denied to identity {identity_id}"
                    )
        
        # Capture before state for audit
        before_state = {
            "identities": [
                {
                    "id": str(id.id),
                    "type": id.type.value,
                    "status": id.status.value,
                    "display_name": id.display_name,
                    "appearances_count": id.appearances_count
                }
                for id in identities_before.values()
            ]
        }
        
        # Get client info for audit
        ip_address, user_agent = get_client_info(http_request)
        
        # Get identity_service dynamically
        identity_service = get_identity_service()
        
        # Merge identities (production-grade with smart selection if target not provided)
        merge_result = await identity_service.merge_multiple_identities(
            identity_ids=identity_uuids,
            target_identity_id=target_uuid,
            user_id=current_user.id,
            notes=request.notes,
            db=db
        )
        
        # Extract merged identity from result
        merged_identity = merge_result["identity"]
        merge_statistics = merge_result["statistics"]
        type_promotion = merge_result["type_promotion"]
        snapshot_selection = merge_result["snapshot_selection"]
        
        # Capture after state for audit with enhanced details
        after_state = {
            "merged_identity": {
                "id": str(merged_identity.id),
                "type": merged_identity.type.value,
                "status": merged_identity.status.value,
                "display_name": merged_identity.display_name,
                "appearances_count": merged_identity.appearances_count
            },
            "statistics": merge_statistics,
            "type_promotion": type_promotion,
            "pipeline_distribution": merge_result.get("pipeline_distribution", {})
        }
        
        # Save identity index
        if identity_index:
            identity_index.save()
        
        await db.commit()
        
        # Log audit entry with enhanced details
        source_ids = [id for id in identity_uuids if id != merged_identity.id]
        if source_ids:
            # Build enhanced notes with pipeline info
            enhanced_notes = (
                f"Multi-merge: {len(source_ids)} identities merged. "
                f"Pipelines: {merge_statistics.get('pipelines', [])}. "
                f"Type changed: {type_promotion.get('changed', False)}. "
                f"{request.notes or ''}"
            )
            
            await IdentityAuditLogger.log_merge(
                db=db,
                user_id=current_user.id,
                username=current_user.username,
                from_identity_id=source_ids[0],  # First source as representative
                to_identity_id=merged_identity.id,
                before_state=before_state,
                after_state=after_state,
                ip_address=ip_address,
                user_agent=user_agent,
                notes=enhanced_notes
            )
            await db.commit()
        
        logger.info(f"Admin {current_user.username} merged {len(source_ids)} identities into {merged_identity.id} "
                   f"(pipelines: {merge_statistics.get('pipeline_count', 0)}, type_changed: {type_promotion.get('changed', False)})")
        
        # Return enhanced response with all production details
        return {
            "success": True,
            "message": f"Successfully merged {len(source_ids)} identities into target identity",
            "identity": {
                "id": str(merged_identity.id),
                "type": merged_identity.type.value,
                "display_name": merged_identity.display_name,
                "status": merged_identity.status.value,
                "appearances_count": merged_identity.appearances_count,
                "best_snapshot_path": merged_identity.best_snapshot_path
            },
            "merged_count": len(source_ids),
            "auto_selected_target": target_uuid is None,
            # Production-grade additional fields
            "statistics": merge_statistics,
            "type_promotion": type_promotion,
            "snapshot_selection": snapshot_selection,
            "pipeline_distribution": merge_result.get("pipeline_distribution", {}),
            "selection_details": merge_result.get("selection_details"),
            "timestamps": merge_result.get("timestamps", {})
        }
    
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e)
        )
    except Exception as e:
        logger.error(f"Error merging multiple identities: {e}", exc_info=True)
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error merging identities: {str(e)}"
        )


# =====================================================
# Search by Image
# =====================================================

@router.post("/search/by-image", response_model=List[SearchResult], summary="Search by Image", description="Search for identities by uploading an image (Admin only)")
async def search_by_image(
    image: UploadFile = File(...),
    scope: str = Form("both"),
    top_k: int = Form(10),
    date_from: Optional[str] = Form(None),
    date_to: Optional[str] = Form(None),
    pipeline_id: Optional[str] = Form(None),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_role(["admin"]))
):
    """
    Search for identities by uploading an image (admin only).
    """
    try:
        # Check if pgvector backend is enabled
        use_pgvector = getattr(settings, 'VECTOR_BACKEND', 'faiss').lower() == 'pgvector'
        
        # Get pgvector index if enabled, otherwise use FAISS
        if use_pgvector:
            from backend.core.identity_index_pgvector import get_pgvector_index
            pgvector_index = get_pgvector_index()
            
            if not pgvector_index or not model_manager:
                logger.error(f"[SEARCH] Service unavailable: pgvector_index={pgvector_index is not None}, model_manager={model_manager is not None}")
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail="pgvector search service not available. Please wait for system initialization or check VECTOR_BACKEND configuration."
                )
        else:
            # Fallback to FAISS
            current_identity_index = get_identity_index()
            
            if not current_identity_index or not model_manager:
                logger.error(f"[SEARCH] Service unavailable: identity_index={current_identity_index is not None}, model_manager={model_manager is not None}")
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail="Identity search service not available. Please wait for system initialization."
                )
        
        # Read and decode image
        image_bytes = await image.read()
        frame = cv2.imdecode(np.frombuffer(image_bytes, np.uint8), cv2.IMREAD_COLOR)
        
        if frame is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid image file"
            )
        
        # Detect face
        bboxes, kpss = model_manager.detector.detect(frame, max_num=1)
        if kpss is None or len(kpss) == 0:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="No face detected in image"
            )
        
        # Align and generate embedding
        from utils.helpers import face_alignment, reference_alignment
        
        aligned_face, _ = face_alignment(frame, kpss[0], image_size=112)
        aligned_face_rgb = cv2.cvtColor(aligned_face, cv2.COLOR_BGR2RGB)
        aligned_face_rgb = np.clip(aligned_face_rgb, 0, 255).astype(np.uint8)
        
        aligned_landmarks = reference_alignment.copy()
        embedding = model_manager.recognizer.get_embedding(
            aligned_face_rgb,
            aligned_landmarks
        )
        embedding = embedding / np.linalg.norm(embedding)  # Normalize
        
        # Search based on scope using pgvector or FAISS
        results = []
        
        if use_pgvector:
            # Use pgvector for search
            logger.info(f"[SEARCH] Using pgvector backend for image search")
            
            if scope in ["known", "both"]:
                known_matches = await pgvector_index.search_known(
                    embedding=embedding,
                    db=db,
                    top_k=top_k,
                    threshold=0.4
                )
                results.extend([(id_str, sim, "known") for id_str, sim in known_matches])
            
            if scope in ["unknown", "both"]:
                unknown_matches = await pgvector_index.search_unknown(
                    embedding=embedding,
                    db=db,
                    top_k=top_k,
                    threshold=0.35
                )
                results.extend([(id_str, sim, "unknown") for id_str, sim in unknown_matches])
        else:
            # Use FAISS for search (fallback)
            logger.info(f"[SEARCH] Using FAISS backend for image search")
            
            if scope in ["known", "both"]:
                known_matches = current_identity_index.search_known(embedding, top_k=top_k, threshold=0.4)
                results.extend([(id_str, sim, "known") for id_str, sim in known_matches])
            
            if scope in ["unknown", "both"]:
                unknown_matches = current_identity_index.search_unknown(embedding, top_k=top_k, threshold=0.35)
                results.extend([(id_str, sim, "unknown") for id_str, sim in unknown_matches])
        
        # Sort by similarity
        results.sort(key=lambda x: x[1], reverse=True)
        results = results[:top_k]
        
        # Get identity details from database
        identity_ids = [uuid.UUID(id_str) for id_str, _, _ in results]
        
        if not identity_ids:
            return []
        
        identities_result = await db.execute(
            select(Identity).where(Identity.id.in_(identity_ids))
        )
        identities_dict = {str(id.id): id for id in identities_result.scalars().all()}
        
        # Build response
        search_results = []
        for id_str, similarity, id_type in results:
            identity = identities_dict.get(id_str)
            if not identity:
                continue
            
            # Apply date/pipeline filters if specified
            if date_from or date_to or pipeline_id:
                appearance_query = select(IdentityAppearance).where(
                    IdentityAppearance.identity_id == identity.id
                )
                
                if date_from:
                    date_from_dt = datetime.fromisoformat(date_from.replace('Z', '+00:00'))
                    appearance_query = appearance_query.where(IdentityAppearance.start_time >= date_from_dt)
                
                if date_to:
                    date_to_dt = datetime.fromisoformat(date_to.replace('Z', '+00:00'))
                    appearance_query = appearance_query.where(IdentityAppearance.start_time <= date_to_dt)
                
                if pipeline_id:
                    appearance_query = appearance_query.where(IdentityAppearance.pipeline_id == pipeline_id)
                
                appearance_result = await db.execute(appearance_query)
                if appearance_result.scalar_one_or_none() is None:
                    continue  # Skip if no matching appearances
            
            search_results.append({
                "identity_id": id_str,
                "type": identity.type.value,
                "display_name": identity.display_name,
                "similarity": float(similarity),
                "best_snapshot_path": identity.best_snapshot_path,
                "last_seen_at": identity.last_seen_at.isoformat(),
                "appearances_count": identity.appearances_count
            })
        
        # Log search
        logger.info(f"Admin {current_user.username} searched by image: {len(search_results)} results")
        
        return search_results
    
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error searching by image: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to search by image: {str(e)}"
        )


# =====================================================
# Merge Suggestions
# =====================================================

@router.get("/admin/merge-suggestions/pipeline/{pipeline_id}", response_model=List[MergeSuggestionResponse], summary="Get Merge Suggestions for Pipeline", description="Get merge suggestions for a specific pipeline based on similarity between images (Admin or users with pipeline access)")
async def get_merge_suggestions_for_pipeline(
    pipeline_id: str,
    status_filter: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    Get merge suggestions for a specific pipeline using DBSCAN clustering.
    - Admin: can access any pipeline
    - Regular users: must have access to the specified pipeline
    - Collects all embeddings for the pipeline and runs DBSCAN to find clusters
    - Generates merge suggestions on-the-fly based on clusters found
    """
    try:
        from db_models import IdentityAppearance
        from backend.auth.auth_service import AuthService
        from sqlalchemy import text
        
        # Check if sklearn is available for DBSCAN
        try:
            from sklearn.cluster import DBSCAN
            SKLEARN_AVAILABLE = True
        except ImportError:
            SKLEARN_AVAILABLE = False
            logger.warning("[MERGE_SUGGESTIONS] [PIPELINE] ⚠️ scikit-learn not available - DBSCAN clustering disabled")
        
        logger.info(f"[MERGE_SUGGESTIONS] [PIPELINE] ========================================")
        logger.info(f"[MERGE_SUGGESTIONS] [PIPELINE] 🔍 Starting DBSCAN-based pipeline merge suggestions for: {pipeline_id}")
        logger.info(f"[MERGE_SUGGESTIONS] [PIPELINE] User: {current_user.username} (ID: {current_user.id}, Role: {current_user.role})")
        
        # Check user has access to this pipeline
        if current_user.role != "admin":
            logger.info(f"[MERGE_SUGGESTIONS] [PIPELINE] Checking pipeline access for regular user...")
            user_pipelines = await AuthService.get_user_pipelines(current_user.id, db)
            logger.info(f"[MERGE_SUGGESTIONS] [PIPELINE] User has access to {len(user_pipelines)} pipelines: {user_pipelines}")
            if pipeline_id not in user_pipelines:
                logger.warning(f"[MERGE_SUGGESTIONS] [PIPELINE] ❌ Access denied: User does not have access to pipeline {pipeline_id}")
                raise HTTPException(status_code=403, detail=f"You do not have permission to access pipeline: {pipeline_id}")
            logger.info(f"[MERGE_SUGGESTIONS] [PIPELINE] ✅ User has access to pipeline {pipeline_id}")
        else:
            logger.info(f"[MERGE_SUGGESTIONS] [PIPELINE] ✅ Admin user - has access to all pipelines")
        
        # Step 1: Get all UNKNOWN identities for this pipeline
        logger.info(f"[MERGE_SUGGESTIONS] [PIPELINE] 🔍 Step 1: Fetching UNKNOWN identities for pipeline {pipeline_id}...")
        identities_result = await db.execute(
            select(Identity).join(
                IdentityAppearance, IdentityAppearance.identity_id == Identity.id
            ).where(
                and_(
                    IdentityAppearance.pipeline_id == pipeline_id,
                    Identity.type == IdentityType.UNKNOWN,
                    Identity.status == IdentityStatus.ACTIVE
                )
            ).distinct()
        )
        pipeline_identities = identities_result.scalars().all()
        
        logger.info(f"[MERGE_SUGGESTIONS] [PIPELINE] ✅ Found {len(pipeline_identities)} UNKNOWN identities in pipeline {pipeline_id}")
        if len(pipeline_identities) > 0:
            for idx, identity in enumerate(pipeline_identities[:5], 1):
                logger.info(f"[MERGE_SUGGESTIONS] [PIPELINE]   {idx}. ID: {identity.id}, Name: {identity.display_name}")
        
        if len(pipeline_identities) < 2:
            logger.info(f"[MERGE_SUGGESTIONS] [PIPELINE] ⚠️ Pipeline {pipeline_id} has {len(pipeline_identities)} identities - not enough for clustering (need at least 2)")
            logger.info(f"[MERGE_SUGGESTIONS] [PIPELINE] ========================================")
            return []
        
        # Step 2: Collect all embeddings for these identities
        logger.info(f"[MERGE_SUGGESTIONS] [PIPELINE] 🔍 Step 2: Collecting embeddings for {len(pipeline_identities)} identities...")
        identity_embeddings_map = {}  # identity_id -> (embedding_vector, quality)
        embeddings_list = []  # List of embeddings for DBSCAN
        identity_order = []  # Track order of identities for cluster mapping
        
        # Determine which backend to use
        USE_PGVECTOR = getattr(settings, 'VECTOR_BACKEND', 'faiss').lower() == 'pgvector'
        
        if USE_PGVECTOR:
            # Get embeddings from pgvector (PostgreSQL)
            logger.info(f"[MERGE_SUGGESTIONS] [PIPELINE] Using pgvector backend to extract embeddings...")
            for identity in pipeline_identities:
                # Get all embedding records for this identity (pgvector stores as vector type)
                emb_result = await db.execute(
                    select(IdentityEmbedding).where(
                        and_(
                            IdentityEmbedding.identity_id == identity.id,
                            IdentityEmbedding.faiss_index_type == 'unknown'
                        )
                    ).order_by(IdentityEmbedding.quality.desc().nulls_last())
                )
                embedding_records = emb_result.scalars().all()
                
                logger.debug(f"[MERGE_SUGGESTIONS] [PIPELINE]   Identity {identity.id}: Found {len(embedding_records)} embedding records")
                
                # Find the first record with a non-NULL embedding (pgvector stores as vector type)
                embedding_record = None
                for emb_rec in embedding_records:
                    # Check if embedding exists using raw SQL (pgvector vector type)
                    check_result = await db.execute(
                        text("""
                            SELECT embedding IS NOT NULL as has_embedding
                            FROM identity_embeddings
                            WHERE id = :emb_id
                        """),
                        {"emb_id": emb_rec.id}
                    )
                    has_embedding = check_result.scalar()
                    logger.debug(f"[MERGE_SUGGESTIONS] [PIPELINE]   Embedding record {emb_rec.id}: has_embedding={has_embedding}, quality={emb_rec.quality}")
                    if has_embedding:
                        embedding_record = emb_rec
                        break
                
                if embedding_record:
                    try:
                        # Extract vector from pgvector using raw SQL
                        vector_result = await db.execute(
                            text("""
                                SELECT embedding::text 
                                FROM identity_embeddings 
                                WHERE id = :emb_id
                            """),
                            {"emb_id": embedding_record.id}
                        )
                        vector_text = vector_result.scalar()
                        
                        if vector_text:
                            # Parse vector string like "[0.1, 0.2, ...]"
                            vector_str = vector_text.strip('[]')
                            embedding_array = np.array([float(x) for x in vector_str.split(',')], dtype=np.float32)
                            
                            # Normalize for cosine similarity
                            norm = np.linalg.norm(embedding_array)
                            if norm > 0:
                                embedding_array = embedding_array / norm
                            
                            identity_embeddings_map[str(identity.id)] = (embedding_array, embedding_record.quality or 0.5)
                            embeddings_list.append(embedding_array)
                            identity_order.append(str(identity.id))
                            logger.debug(f"[MERGE_SUGGESTIONS] [PIPELINE]   ✅ Extracted embedding for {identity.id}: shape={embedding_array.shape}, norm={np.linalg.norm(embedding_array):.6f}")
                        else:
                            logger.warning(f"[MERGE_SUGGESTIONS] [PIPELINE]   ⚠️ No vector text returned for identity {identity.id}")
                    except Exception as e:
                        logger.warning(f"[MERGE_SUGGESTIONS] [PIPELINE]   ⚠️ Failed to extract embedding for {identity.id}: {e}", exc_info=True)
                else:
                    if len(embedding_records) == 0:
                        logger.warning(f"[MERGE_SUGGESTIONS] [PIPELINE]   ⚠️ No embedding records found for identity {identity.id} (faiss_index_type='unknown')")
                        logger.warning(f"[MERGE_SUGGESTIONS] [PIPELINE]   This means the identity was created but no embedding was saved (possibly quality too low)")
                    else:
                        logger.warning(f"[MERGE_SUGGESTIONS] [PIPELINE]   ⚠️ Found {len(embedding_records)} embedding records but all have NULL embeddings for identity {identity.id}")
        else:
            # FAISS backend - reconstruct from FAISS index
            logger.info(f"[MERGE_SUGGESTIONS] [PIPELINE] Using FAISS backend to extract embeddings...")
            from backend.core.identity_index import identity_index
            if identity_index and identity_index.unknown_index:
                for identity in pipeline_identities:
                    # Get FAISS ID from embedding record
                    emb_result = await db.execute(
                        select(IdentityEmbedding).where(
                            and_(
                                IdentityEmbedding.identity_id == identity.id,
                                IdentityEmbedding.faiss_id.isnot(None),
                                IdentityEmbedding.faiss_index_type == 'unknown'
                            )
                        ).order_by(IdentityEmbedding.quality.desc().nulls_last()).limit(1)
                    )
                    embedding_record = emb_result.scalar_one_or_none()
                    
                    if embedding_record and embedding_record.faiss_id is not None:
                        try:
                            with identity_index.lock:
                                if embedding_record.faiss_id < identity_index.unknown_index.ntotal:
                                    embedding_array = identity_index.unknown_index.reconstruct(int(embedding_record.faiss_id))
                                    # Normalize
                                    norm = np.linalg.norm(embedding_array)
                                    if norm > 0:
                                        embedding_array = embedding_array / norm
                                    
                                    identity_embeddings_map[str(identity.id)] = (embedding_array.astype(np.float32), embedding_record.quality or 0.5)
                                    embeddings_list.append(embedding_array.astype(np.float32))
                                    identity_order.append(str(identity.id))
                                    logger.debug(f"[MERGE_SUGGESTIONS] [PIPELINE]   ✅ Reconstructed embedding for {identity.id} from FAISS")
                        except Exception as e:
                            logger.warning(f"[MERGE_SUGGESTIONS] [PIPELINE]   ⚠️ Failed to reconstruct embedding for {identity.id}: {e}")
        
        logger.info(f"[MERGE_SUGGESTIONS] [PIPELINE] ✅ Collected {len(embeddings_list)} embeddings from {len(pipeline_identities)} identities")
        
        if len(embeddings_list) < 2:
            logger.info(f"[MERGE_SUGGESTIONS] [PIPELINE] ⚠️ Not enough embeddings ({len(embeddings_list)}) for clustering (need at least 2)")
            logger.info(f"[MERGE_SUGGESTIONS] [PIPELINE] ========================================")
            return []
        
        if not SKLEARN_AVAILABLE:
            logger.warning(f"[MERGE_SUGGESTIONS] [PIPELINE] ⚠️ scikit-learn not available, cannot run DBSCAN clustering")
            logger.info(f"[MERGE_SUGGESTIONS] [PIPELINE] ========================================")
            return []
        
        # Step 3: Run DBSCAN clustering
        logger.info(f"[MERGE_SUGGESTIONS] [PIPELINE] 🔍 Step 3: Running DBSCAN clustering on {len(embeddings_list)} embeddings...")
        
        # Get DBSCAN parameters from config
        eps = getattr(settings, 'CLUSTER_EPS', 0.35)
        min_samples = getattr(settings, 'CLUSTER_MIN_SAMPLES', 2)
        
        logger.info(f"[MERGE_SUGGESTIONS] [PIPELINE] DBSCAN parameters: eps={eps}, min_samples={min_samples}")
        
        # Convert embeddings list to numpy array
        embeddings_matrix = np.array(embeddings_list, dtype=np.float32)
        logger.info(f"[MERGE_SUGGESTIONS] [PIPELINE] Embeddings matrix shape: {embeddings_matrix.shape}")
        
        # Run DBSCAN (using cosine distance via 'cosine' metric)
        # Note: DBSCAN with cosine distance works well for normalized embeddings
        dbscan = DBSCAN(eps=eps, min_samples=min_samples, metric='cosine', algorithm='brute')
        cluster_labels = dbscan.fit_predict(embeddings_matrix)
        
        # Analyze clusters
        unique_clusters = set(cluster_labels)
        noise_count = sum(1 for label in cluster_labels if label == -1)
        valid_clusters = [c for c in unique_clusters if c != -1]
        
        logger.info(f"[MERGE_SUGGESTIONS] [PIPELINE] ✅ DBSCAN clustering complete:")
        logger.info(f"[MERGE_SUGGESTIONS] [PIPELINE]   • Total clusters found: {len(valid_clusters)}")
        logger.info(f"[MERGE_SUGGESTIONS] [PIPELINE]   • Noise points (outliers): {noise_count}")
        logger.info(f"[MERGE_SUGGESTIONS] [PIPELINE]   • Cluster sizes: {[sum(1 for l in cluster_labels if l == c) for c in valid_clusters]}")
        
        if len(valid_clusters) == 0:
            logger.info(f"[MERGE_SUGGESTIONS] [PIPELINE] ⚠️ No clusters found - all identities are unique or too dissimilar")
            logger.info(f"[MERGE_SUGGESTIONS] [PIPELINE] ========================================")
            return []
        
        # Step 4: Generate merge suggestions from clusters
        logger.info(f"[MERGE_SUGGESTIONS] [PIPELINE] 🔍 Step 4: Generating merge suggestions from {len(valid_clusters)} clusters...")
        suggestions_data = []
        
        for cluster_id in valid_clusters:
            # Get all identity IDs in this cluster
            cluster_identity_ids = [identity_order[i] for i, label in enumerate(cluster_labels) if label == cluster_id]
            cluster_size = len(cluster_identity_ids)
            
            logger.info(f"[MERGE_SUGGESTIONS] [PIPELINE]   Cluster {cluster_id}: {cluster_size} identities")
            
            if cluster_size < 2:
                logger.debug(f"[MERGE_SUGGESTIONS] [PIPELINE]   ⚠️ Cluster {cluster_id} has only {cluster_size} identity, skipping")
                continue
            
            # Calculate average similarity within cluster (for confidence score)
            similarities = []
            for i, id1 in enumerate(cluster_identity_ids):
                for id2 in cluster_identity_ids[i+1:]:
                    emb1, _ = identity_embeddings_map[id1]
                    emb2, _ = identity_embeddings_map[id2]
                    # Cosine similarity for normalized vectors
                    similarity = np.dot(emb1, emb2)
                    similarities.append(similarity)
            
            avg_similarity = np.mean(similarities) if similarities else 0.0
            confidence = min(0.95, max(0.5, avg_similarity))  # Clamp between 0.5 and 0.95
            
            logger.info(f"[MERGE_SUGGESTIONS] [PIPELINE]   ✅ Cluster {cluster_id}: {cluster_size} identities, avg similarity: {avg_similarity:.3f}, confidence: {confidence:.3f}")
            
            # Get representative snapshots
            snapshots = []
            for identity_id in cluster_identity_ids[:3]:  # Get up to 3 snapshots
                identity = next((id for id in pipeline_identities if str(id.id) == identity_id), None)
                if identity and identity.best_snapshot_path:
                    snapshots.append(identity.best_snapshot_path)
            
            # Format snapshots for frontend
            formatted_snapshots = []
            if snapshots:
                storage_dir = settings.STORAGE_DIR
                storage_dir_abs = os.path.abspath(storage_dir)
                for snap in snapshots:
                    if snap:
                        if os.path.isabs(snap):
                            snap_abs = os.path.abspath(snap)
                            if snap_abs.startswith(storage_dir_abs):
                                relative_path = os.path.relpath(snap_abs, storage_dir_abs)
                                snap = 'storage/' + relative_path.replace('\\', '/')
                        elif not snap.startswith('storage/'):
                            snap = 'storage/' + snap.lstrip('/')
                        formatted_snapshots.append(snap)
            
            # Generate recommendation
            if confidence >= 0.7:
                recommendation = "✅ High confidence - safe to approve"
            elif confidence >= 0.5:
                recommendation = "⚡ Medium confidence - review carefully"
            else:
                recommendation = "⚠️ Low confidence - review very carefully"
            
            suggestion_data = {
                "id": None,  # Not saved to DB, generated on-the-fly
                "cluster_id": f"pipeline_dbscan_{pipeline_id}_cluster_{cluster_id}",
                "identity_ids": cluster_identity_ids,
                "identity_count": cluster_size,
                "confidence": float(confidence),
                "confidence_percent": round(float(confidence) * 100, 1),
                "status": "pending",
                "representative_snapshots": formatted_snapshots,
                "snapshot_count": len(formatted_snapshots),
                "created_at": datetime.utcnow().isoformat(),
                "recommendation": recommendation,
                "display_name": f"Pipeline {pipeline_id} - Cluster of {cluster_size} identities (DBSCAN)"
            }
            suggestions_data.append(suggestion_data)
        
        logger.info(f"[MERGE_SUGGESTIONS] [PIPELINE] ✅ Generated {len(suggestions_data)} merge suggestions from {len(valid_clusters)} clusters")
        logger.info(f"[MERGE_SUGGESTIONS] [PIPELINE] 📤 Returning {len(suggestions_data)} merge suggestions for pipeline {pipeline_id}")
        logger.info(f"[MERGE_SUGGESTIONS] [PIPELINE] ========================================")
        
        return suggestions_data
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[MERGE_SUGGESTIONS] [PIPELINE] Error getting pipeline suggestions: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/admin/merge-suggestions", response_model=List[MergeSuggestionResponse], summary="Get Merge Suggestions", description="Get pending merge suggestions for unknown identities (Admin or users with pipeline access)")
async def get_merge_suggestions(
    status_filter: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    Get merge suggestions.
    - Admin: sees all merge suggestions
    - Regular users: only see merge suggestions for identities from their accessible pipelines
    """
    try:
        from db_models import MergeSuggestion, MergeSuggestionStatus
        
        query = select(MergeSuggestion)
        
        if status_filter:
            query = query.where(MergeSuggestion.status == MergeSuggestionStatus(status_filter))
        else:
            query = query.where(MergeSuggestion.status == MergeSuggestionStatus.PENDING)
        
        query = query.order_by(MergeSuggestion.confidence.desc(), MergeSuggestion.created_at.desc())
        
        result = await db.execute(query)
        suggestions = result.scalars().all()
        
        logger.info(f"[MERGE_SUGGESTIONS] Query executed: Found {len(suggestions)} suggestions with status_filter='{status_filter}'")
        
        # Check if there are any suggestions at all (any status) for debugging
        if len(suggestions) == 0:
            all_suggestions_query = select(func.count(MergeSuggestion.id))
            all_count_result = await db.execute(all_suggestions_query)
            all_count = all_count_result.scalar() or 0
            logger.info(f"[MERGE_SUGGESTIONS] No suggestions found with current filter. Total suggestions in database (all statuses): {all_count}")
            if all_count > 0:
                # Check status distribution
                status_query = select(MergeSuggestion.status, func.count(MergeSuggestion.id)).group_by(MergeSuggestion.status)
                status_result = await db.execute(status_query)
                status_counts = {str(row[0].value): row[1] for row in status_result.all()}
                logger.info(f"[MERGE_SUGGESTIONS] Status distribution: {status_counts}")
                logger.info(f"[MERGE_SUGGESTIONS] 💡 Tip: Try calling with ?status_filter=approved or ?status_filter=rejected to see all suggestions")
        
        # Filter by user's accessible pipelines if not admin
        filtered_suggestions = []
        if current_user.role != "admin":
            from backend.auth.auth_service import AuthService, check_identity_access
            user_pipelines = await AuthService.get_user_pipelines(current_user.id, db)
            if not user_pipelines:
                return []
            
            for s in suggestions:
                # Check if user has access to any identity in the suggestion
                identity_ids = s.identity_ids if isinstance(s.identity_ids, list) else []
                has_access = False
                for identity_id in identity_ids:
                    try:
                        if await check_identity_access(str(identity_id), current_user, db):
                            has_access = True
                            break
                    except:
                        continue
                if has_access:
                    filtered_suggestions.append(s)
        else:
            filtered_suggestions = suggestions
        
        logger.info(f"[MERGE_SUGGESTIONS] After filtering: {len(filtered_suggestions)} suggestions for user {current_user.username} (role: {current_user.role})")
        
        # Backend prepares all data for frontend - frontend just displays
        suggestions_data = []
        for s in filtered_suggestions:
            identity_ids = s.identity_ids if isinstance(s.identity_ids, list) else []
            snapshots = s.representative_snapshots if isinstance(s.representative_snapshots, list) else []
            
            # Format snapshots for frontend (ensure proper paths)
            formatted_snapshots = []
            if snapshots:
                import os
                storage_dir = settings.STORAGE_DIR
                storage_dir_abs = os.path.abspath(storage_dir)
                for snap in snapshots:
                    if snap:
                        if os.path.isabs(snap):
                            snap_abs = os.path.abspath(snap)
                            if snap_abs.startswith(storage_dir_abs):
                                relative_path = os.path.relpath(snap_abs, storage_dir_abs)
                                snap = 'storage/' + relative_path.replace('\\', '/')
                        elif not snap.startswith('storage/'):
                            snap = 'storage/' + snap.lstrip('/')
                        formatted_snapshots.append(snap)
            
            # Determine cluster type for display
            cluster_id_str = str(s.cluster_id) if s.cluster_id else ""
            is_cross_camera = "cross_camera" in cluster_id_str
            
            if "graph_cluster" in cluster_id_str:
                cluster_type = "graph_cluster"
            elif is_cross_camera:
                cluster_type = "cross_camera"  # NEW: Cross-camera match
            else:
                cluster_type = "hybrid_pair"
            
            is_large_cluster = len(identity_ids) >= 3
            
            # Get pipelines for the identities in this suggestion
            suggestion_pipelines = set()
            for identity_id in identity_ids:
                try:
                    from db_models import IdentityAppearance
                    from uuid import UUID
                    app_result = await db.execute(
                        select(IdentityAppearance.pipeline_id).where(
                            IdentityAppearance.identity_id == UUID(identity_id)
                        ).distinct()
                    )
                    for row in app_result.scalars().all():
                        if row:
                            suggestion_pipelines.add(row)
                except:
                    pass
            
            # Generate recommendation
            if is_cross_camera:
                recommendation = "⚠️ Cross-camera match - verify faces carefully" if s.confidence >= 0.5 else "⚠️ Cross-camera, low confidence - review very carefully"
            elif s.confidence >= 0.7:
                recommendation = "✅ High confidence - safe to approve"
            elif s.confidence >= 0.5:
                recommendation = "⚡ Medium confidence - review carefully"
            else:
                recommendation = "⚠️ Low confidence - review very carefully"
            
            # Generate display name
            if is_cross_camera:
                display_name = f"Cross-camera match ({len(suggestion_pipelines)} cameras)"
            elif is_large_cluster:
                display_name = f"Cluster of {len(identity_ids)} identities"
            else:
                display_name = "Same-camera pair"
            
            suggestions_data.append({
                "id": s.id,
                "cluster_id": s.cluster_id or f"suggestion_{s.id}",
                "identity_ids": identity_ids,
                "identity_count": len(identity_ids),
                "confidence": float(s.confidence),
                "confidence_percent": round(float(s.confidence) * 100, 1),
                "status": s.status.value,
                "representative_snapshots": formatted_snapshots,
                "snapshot_count": len(formatted_snapshots),
                "created_at": s.created_at.isoformat(),
                "cluster_type": cluster_type,
                "is_large_cluster": is_large_cluster,
                "is_cross_camera": is_cross_camera,  # NEW
                "pipelines": list(suggestion_pipelines),  # NEW
                # Additional metadata for frontend
                "display_name": display_name,
                "recommendation": recommendation
            })
        
        logger.info(f"[MERGE_SUGGESTIONS] Returning {len(suggestions_data)} formatted suggestions to frontend")
        if len(suggestions_data) > 0:
            logger.debug(f"[MERGE_SUGGESTIONS] First suggestion sample: id={suggestions_data[0].get('id')}, identity_count={suggestions_data[0].get('identity_count')}, confidence={suggestions_data[0].get('confidence')}")
        else:
            logger.info(f"[MERGE_SUGGESTIONS] No suggestions to return - this is normal if no merge suggestions have been generated yet")
        
        return suggestions_data
    
    except Exception as e:
        logger.error(f"Error getting merge suggestions: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to get merge suggestions: {str(e)}"
        )


@router.post("/admin/merge-suggestions/{suggestion_id}/approve", summary="Approve Merge Suggestion", description="Approve and execute a merge suggestion (Admin or users with pipeline access)")
async def approve_merge_suggestion(
    suggestion_id: int,
    http_request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    Approve and execute a merge suggestion.
    - Admin: can approve any merge suggestion
    - Regular users: can only approve merge suggestions for identities from their accessible pipelines
    """
    try:
        from db_models import MergeSuggestion, MergeSuggestionStatus
        
        # Get identity_service dynamically (may be set during startup)
        identity_service = get_identity_service()
        if not identity_service:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Identity service not available"
            )
        
        # Get suggestion
        result = await db.execute(
            select(MergeSuggestion).where(MergeSuggestion.id == suggestion_id)
        )
        suggestion = result.scalar_one_or_none()
        
        if not suggestion:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Merge suggestion not found"
            )
        
        # Check access for non-admin users
        if current_user.role != "admin":
            from backend.auth.auth_service import check_identity_access
            identity_ids = suggestion.identity_ids if isinstance(suggestion.identity_ids, list) else []
            if len(identity_ids) < 2:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Merge suggestion must contain at least 2 identities"
                )
            # Check access to all identities in the suggestion
            for identity_id in identity_ids:
                has_access = await check_identity_access(str(identity_id), current_user, db)
                if not has_access:
                    raise HTTPException(
                        status_code=status.HTTP_403_FORBIDDEN,
                        detail="Access denied to one or more identities in this merge suggestion"
                    )
        
        if suggestion.status != MergeSuggestionStatus.PENDING:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Suggestion already {suggestion.status.value}"
            )
        
        if len(suggestion.identity_ids) < 2:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Suggestion must have at least 2 identities"
            )
        
        # Merge all identities into the first one
        primary_id = uuid.UUID(suggestion.identity_ids[0])
        
        # Get identity_service dynamically
        identity_service = get_identity_service()
        
        for identity_id_str in suggestion.identity_ids[1:]:
            from_id = uuid.UUID(identity_id_str)
            await identity_service.merge_identities(
                from_identity_id=from_id,
                to_identity_id=primary_id,
                user_id=current_user.id,
                notes=f"Auto-merged from suggestion {suggestion_id}",
                db=db
            )
        
        # Update suggestion status
        suggestion.status = MergeSuggestionStatus.APPROVED
        suggestion.reviewed_at = datetime.utcnow()
        suggestion.reviewed_by = current_user.id
        
        # Collect training data for similarity model
        try:
            from backend.core.similarity_model import similarity_model
            from backend.core.pipeline_aware_clustering import pipeline_aware_clustering
            
            # Get identity features to extract training features
            identity_ids = suggestion.identity_ids if isinstance(suggestion.identity_ids, list) else []
            if len(identity_ids) >= 2:
                # Fetch identities to get their features
                id1_result = await db.execute(
                    select(Identity).where(Identity.id == uuid.UUID(identity_ids[0]))
                )
                id2_result = await db.execute(
                    select(Identity).where(Identity.id == uuid.UUID(identity_ids[1]))
                )
                identity1 = id1_result.scalar_one_or_none()
                identity2 = id2_result.scalar_one_or_none()
                
                if identity1 and identity2:
                    # Get user pipelines for feature extraction
                    from backend.auth.auth_service import AuthService
                    user_pipelines = await AuthService.get_user_pipelines(current_user.id, db) or []
                    
                    # Build features (simplified - we'll extract what we can)
                    # Get embeddings to calculate similarity
                    emb1_result = await db.execute(
                        select(IdentityEmbedding).where(
                            IdentityEmbedding.identity_id == identity1.id
                        ).order_by(IdentityEmbedding.quality.desc()).limit(1)
                    )
                    emb2_result = await db.execute(
                        select(IdentityEmbedding).where(
                            IdentityEmbedding.identity_id == identity2.id
                        ).order_by(IdentityEmbedding.quality.desc()).limit(1)
                    )
                    
                    emb1 = emb1_result.scalar_one_or_none()
                    emb2 = emb2_result.scalar_one_or_none()
                    
                    if emb1 and emb2:
                        # Calculate features
                        from backend.core.identity_index import identity_index
                        if identity_index:
                            try:
                                emb1_vec = identity_index.unknown_index.reconstruct(int(emb1.faiss_id)) if emb1.faiss_id is not None else None
                                emb2_vec = identity_index.unknown_index.reconstruct(int(emb2.faiss_id)) if emb2.faiss_id is not None else None
                                
                                if emb1_vec is not None and emb2_vec is not None:
                                    emb1_vec = emb1_vec / np.linalg.norm(emb1_vec)
                                    emb2_vec = emb2_vec / np.linalg.norm(emb2_vec)
                                    embedding_sim = float(np.dot(emb1_vec, emb2_vec))
                                    
                                    # Get pipeline overlap
                                    from db_models import IdentityAppearance
                                    app1_result = await db.execute(
                                        select(IdentityAppearance.pipeline_id).where(
                                            IdentityAppearance.identity_id == identity1.id
                                        ).distinct()
                                    )
                                    app2_result = await db.execute(
                                        select(IdentityAppearance.pipeline_id).where(
                                            IdentityAppearance.identity_id == identity2.id
                                        ).distinct()
                                    )
                                    pipelines1 = {row[0] for row in app1_result if row[0]}
                                    pipelines2 = {row[0] for row in app2_result if row[0]}
                                    common = pipelines1 & pipelines2
                                    all_pipelines = pipelines1 | pipelines2
                                    pipeline_overlap = len(common) / len(all_pipelines) if all_pipelines else 0.0
                                    is_cross = len(common) == 0
                                    
                                    # Add training sample (approved = 1.0)
                                    similarity_model.add_training_sample(
                                        embedding_similarity=embedding_sim,
                                        pipeline_overlap=pipeline_overlap,
                                        quality_score_1=emb1.quality or 0.5,
                                        quality_score_2=emb2.quality or 0.5,
                                        appearances_diff=abs(identity1.appearances_count - identity2.appearances_count),
                                        is_cross_pipeline=is_cross,
                                        label=1.0,  # Approved = positive sample
                                        db_session=db,
                                        identity_id_1=identity1.id,
                                        identity_id_2=identity2.id,
                                        user_id=current_user.id
                                    )
                            except Exception as e:
                                logger.debug(f"Could not collect training data: {e}")
        except Exception as e:
            logger.debug(f"Training data collection failed: {e}")
        
        # Save identity index
        if identity_index:
            identity_index.save()
        
        await db.commit()
        
        logger.info(f"Admin {current_user.username} approved merge suggestion {suggestion_id}")
        
        return {
            "success": True,
            "message": "Merge suggestion approved and executed",
            "merged_identities": len(suggestion.identity_ids) - 1
        }
    
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e)
        )
    except Exception as e:
        logger.error(f"Error approving merge suggestion: {e}", exc_info=True)
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to approve merge suggestion: {str(e)}"
        )


@router.post("/admin/merge-suggestions/{suggestion_id}/reject", summary="Reject Merge Suggestion", description="Reject a merge suggestion (Admin or users with pipeline access)")
async def reject_merge_suggestion(
    suggestion_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    Reject a merge suggestion.
    - Admin: can reject any merge suggestion
    - Regular users: can only reject merge suggestions for identities from their accessible pipelines
    """
    try:
        from db_models import MergeSuggestion, MergeSuggestionStatus
        
        result = await db.execute(
            select(MergeSuggestion).where(MergeSuggestion.id == suggestion_id)
        )
        suggestion = result.scalar_one_or_none()
        
        if not suggestion:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Merge suggestion not found"
            )
        
        # Check access for non-admin users
        if current_user.role != "admin":
            from backend.auth.auth_service import check_identity_access
            identity_ids = suggestion.identity_ids if isinstance(suggestion.identity_ids, list) else []
            # Check access to all identities in the suggestion
            for identity_id in identity_ids:
                has_access = await check_identity_access(str(identity_id), current_user, db)
                if not has_access:
                    raise HTTPException(
                        status_code=status.HTTP_403_FORBIDDEN,
                        detail="Access denied to one or more identities in this merge suggestion"
                    )
        
        suggestion.status = MergeSuggestionStatus.REJECTED
        suggestion.reviewed_at = datetime.utcnow()
        suggestion.reviewed_by = current_user.id
        
        # Collect training data for similarity model (rejected = negative sample)
        try:
            from backend.core.similarity_model import similarity_model
            
            identity_ids = suggestion.identity_ids if isinstance(suggestion.identity_ids, list) else []
            if len(identity_ids) >= 2:
                id1_result = await db.execute(
                    select(Identity).where(Identity.id == uuid.UUID(identity_ids[0]))
                )
                id2_result = await db.execute(
                    select(Identity).where(Identity.id == uuid.UUID(identity_ids[1]))
                )
                identity1 = id1_result.scalar_one_or_none()
                identity2 = id2_result.scalar_one_or_none()
                
                if identity1 and identity2:
                    emb1_result = await db.execute(
                        select(IdentityEmbedding).where(
                            IdentityEmbedding.identity_id == identity1.id
                        ).order_by(IdentityEmbedding.quality.desc()).limit(1)
                    )
                    emb2_result = await db.execute(
                        select(IdentityEmbedding).where(
                            IdentityEmbedding.identity_id == identity2.id
                        ).order_by(IdentityEmbedding.quality.desc()).limit(1)
                    )
                    
                    emb1 = emb1_result.scalar_one_or_none()
                    emb2 = emb2_result.scalar_one_or_none()
                    
                    if emb1 and emb2:
                        from backend.core.identity_index import identity_index
                        if identity_index:
                            try:
                                emb1_vec = identity_index.unknown_index.reconstruct(int(emb1.faiss_id)) if emb1.faiss_id is not None else None
                                emb2_vec = identity_index.unknown_index.reconstruct(int(emb2.faiss_id)) if emb2.faiss_id is not None else None
                                
                                if emb1_vec is not None and emb2_vec is not None:
                                    emb1_vec = emb1_vec / np.linalg.norm(emb1_vec)
                                    emb2_vec = emb2_vec / np.linalg.norm(emb2_vec)
                                    embedding_sim = float(np.dot(emb1_vec, emb2_vec))
                                    
                                    app1_result = await db.execute(
                                        select(IdentityAppearance.pipeline_id).where(
                                            IdentityAppearance.identity_id == identity1.id
                                        ).distinct()
                                    )
                                    app2_result = await db.execute(
                                        select(IdentityAppearance.pipeline_id).where(
                                            IdentityAppearance.identity_id == identity2.id
                                        ).distinct()
                                    )
                                    pipelines1 = {row[0] for row in app1_result if row[0]}
                                    pipelines2 = {row[0] for row in app2_result if row[0]}
                                    common = pipelines1 & pipelines2
                                    all_pipelines = pipelines1 | pipelines2
                                    pipeline_overlap = len(common) / len(all_pipelines) if all_pipelines else 0.0
                                    is_cross = len(common) == 0
                                    
                                    # Add training sample (rejected = 0.0)
                                    similarity_model.add_training_sample(
                                        embedding_similarity=embedding_sim,
                                        pipeline_overlap=pipeline_overlap,
                                        quality_score_1=emb1.quality or 0.5,
                                        quality_score_2=emb2.quality or 0.5,
                                        appearances_diff=abs(identity1.appearances_count - identity2.appearances_count),
                                        is_cross_pipeline=is_cross,
                                        label=0.0,  # Rejected = negative sample
                                        db_session=db,
                                        identity_id_1=identity1.id,
                                        identity_id_2=identity2.id,
                                        user_id=current_user.id
                                    )
                            except Exception as e:
                                logger.debug(f"Could not collect training data: {e}")
        except Exception as e:
            logger.debug(f"Training data collection failed: {e}")
        
        await db.commit()
        
        return {
            "success": True,
            "message": "Merge suggestion rejected"
        }
    
    except Exception as e:
        logger.error(f"Error rejecting merge suggestion: {e}", exc_info=True)
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to reject merge suggestion: {str(e)}"
        )


@router.post("/admin/merge-suggestions/generate-pipeline-aware", summary="Generate Pipeline-Aware Merge Suggestions", description="Generate merge suggestions using ML-based pipeline-aware clustering (Admin only)")
async def generate_pipeline_aware_suggestions(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_role(["admin"]))
):
    """
    Generate merge suggestions using advanced ML-based pipeline-aware clustering.
    
    This endpoint:
    - Filters identities by user's accessible pipelines (if user_id provided)
    - Uses pipeline-specific embeddings
    - Applies weighted similarity based on pipeline context
    - Leverages embeddings and image features together
    
    Admin only.
    """
    try:
        from backend.core.pipeline_aware_clustering import pipeline_aware_clustering
        from backend.auth.auth_service import AuthService
        
        # Get current user's pipelines (for filtering)
        user_pipelines = await AuthService.get_user_pipelines(current_user.id, db)
        
        # Generate suggestions
        suggestions_count = await pipeline_aware_clustering.generate_pipeline_aware_suggestions(
            db=db,
            user_id=current_user.id if current_user.role != "admin" else None,
            user_pipelines=user_pipelines if current_user.role != "admin" else None
        )
        
        return {
            "message": "Pipeline-aware merge suggestions generated successfully",
            "suggestions_created": suggestions_count,
            "user_pipelines": user_pipelines if current_user.role != "admin" else "all"
        }
        
    except Exception as e:
        logger.error(f"Error generating pipeline-aware suggestions: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to generate pipeline-aware suggestions: {str(e)}"
        )


# =====================================================
# ML Similarity Model lifecycle (hardened)
#   * training runs as a background job (202 + job_id, 409 single-flight)
#   * candidate/active separation via similarity_model_registry
#   * quality gates + activation/reject/rollback with CSRF + audit
#   * status is DB-backed (persistent samples), strictly typed, and never
#   exposes filesystem paths — clients get logical artifact names
# =====================================================

from fastapi import BackgroundTasks
from fastapi.responses import JSONResponse as _MLJSONResponse


def _require_ml_csrf(request: Request):
    """CSRF defense for cookie-authenticated model mutations."""
    if request.headers.get("authorization"):
        return
    if request.headers.get("x-requested-with", "").lower() != "xmlhttprequest":
        raise HTTPException(status_code=403,
                            detail="CSRF check failed: X-Requested-With header required")


def _ml_reference_id() -> str:
    return f"ML-{uuid.uuid4().hex[:8]}"


def _ml_safe_500(action: str, exc: Exception) -> HTTPException:
    ref = _ml_reference_id()
    logger.error("[ML_MODEL] action=%s status=error reference_id=%s error=%s",
                 action, ref, exc, exc_info=True)
    return HTTPException(status_code=500,
                         detail=f"Internal error during {action}. Reference: {ref}")


def _ml_audit(action: str, current_user, model_id=None, result: str = "success", **fields):
    """Structured audit line — never embeddings, datasets, tokens or paths."""
    extra = " ".join(f"{k}={v}" for k, v in fields.items() if v is not None)
    logger.info("[MODEL_AUDIT] action=%s user_id=%s model_id=%s result=%s %s",
                action, getattr(current_user, "id", None), model_id, result, extra)


def _ml_error(status_code: int, code: str, message: str, **extra):
    detail = {"error_code": code, "message": message}
    detail.update(extra)
    return HTTPException(status_code=status_code, detail=detail)


@router.post("/admin/merge-suggestions/training-jobs", summary="Schedule Training Job",
             description="Schedule similarity-model training as a background job (202 + job_id; 409 while one runs)")
async def create_training_job(
    request: Request,
    background_tasks: BackgroundTasks,
    min_samples: Optional[int] = Query(default=None, ge=10, le=1000),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_role(["admin"])),
    _csrf: None = Depends(_require_ml_csrf)
):
    """Training NEVER runs inside this request — it is a staged background
    job producing a versioned candidate that awaits approval."""
    from backend.core import model_training_service as mts

    # Fail fast with a structured error when the dataset cannot train
    readiness = await mts.dataset_readiness(db, min_samples)
    if not readiness["ready_to_train"]:
        raise _ml_error(400, "DATASET_NOT_READY",
                        "The training dataset does not meet readiness requirements.",
                        readiness_reason=readiness["readiness_reason"],
                        readiness_checks=readiness["readiness_checks"],
                        retryable=False)

    job_id = f"simtrain-{uuid.uuid4().hex[:8]}"
    running = mts.try_acquire_training(job_id)
    if running is not None:
        raise _ml_error(409, "TRAINING_ALREADY_RUNNING",
                        "A training job is already running.", job_id=running)

    try:
        from backend.core.task_history import task_history_manager
        task_id = await task_history_manager.create_job(
            job_id=job_id,
            task_type="similarity_model_training",
            task_name="Train Similarity Model",
            description="Train merge-suggestion similarity model candidate",
        )
        background_tasks.add_task(mts.run_training_job, job_id, min_samples,
                                  getattr(current_user, "id", None))
        _ml_audit("training_requested", current_user, job_id=job_id,
                  min_samples=min_samples or "default")
        return _MLJSONResponse(status_code=202, content={
            "accepted": True, "job_id": job_id, "task_id": task_id,
            "status": "scheduled", "task_type": "similarity_model_training",
        })
    except HTTPException:
        mts.release_training(job_id)
        raise
    except Exception as e:
        mts.release_training(job_id)
        raise _ml_safe_500("training job scheduling", e)


@router.get("/admin/merge-suggestions/training-jobs/{job_id}", summary="Get Training Job")
async def get_training_job(
    job_id: str,
    current_user: User = Depends(require_role(["admin"]))
):
    from backend.core.task_history import task_history_manager
    task = await task_history_manager.get_task_by_job_id(job_id)
    if not task or task.get("task_type") != "similarity_model_training":
        raise HTTPException(status_code=404, detail="Job not found")
    resp = _MLJSONResponse(content=task)
    resp.headers["Cache-Control"] = "no-store"
    return resp


@router.get("/admin/merge-suggestions/training-jobs", summary="List Training Jobs")
async def list_training_jobs(
    limit: int = Query(default=20, ge=1, le=100),
    current_user: User = Depends(require_role(["admin"]))
):
    """Recent training history (Background Tasks records)."""
    try:
        from backend.core.task_history import task_history_manager
        from db_models import BackgroundTaskHistory
        from db_connection import db_manager
        async with db_manager.get_session() as db:
            rows = (await db.execute(
                select(BackgroundTaskHistory)
                .where(BackgroundTaskHistory.task_type == "similarity_model_training")
                .order_by(BackgroundTaskHistory.created_at.desc())
                .limit(limit)
            )).scalars().all()
            items = [task_history_manager._task_to_dict(r) for r in rows]
        resp = _MLJSONResponse(content={"items": items, "count": len(items)})
        resp.headers["Cache-Control"] = "no-store"
        return resp
    except Exception as e:
        raise _ml_safe_500("training job listing", e)


@router.post("/admin/merge-suggestions/training-jobs/{job_id}/cancel", summary="Cancel Training Job")
async def cancel_training_job(
    job_id: str,
    request: Request,
    current_user: User = Depends(require_role(["admin"])),
    _csrf: None = Depends(_require_ml_csrf)
):
    """Best-effort cancellation between training stages."""
    from backend.core import model_training_service as mts
    if not mts.request_cancel(job_id):
        raise HTTPException(status_code=404, detail="No running job with this id")
    _ml_audit("training_cancelled", current_user, job_id=job_id)
    return {"success": True, "job_id": job_id, "status": "cancel_requested"}


@router.post("/admin/merge-suggestions/models/{model_id}/activate", summary="Activate Candidate Model")
async def activate_similarity_model(
    model_id: int,
    request: Request,
    reason: Optional[str] = Query(default=None, max_length=500),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_role(["admin"])),
    _csrf: None = Depends(_require_ml_csrf)
):
    """Atomic activation: gates + artifact hash + load test, then archive
    previous active and refresh the runtime — the old model keeps serving
    until every check passes."""
    from backend.core import model_training_service as mts
    try:
        outcome = await mts.activate_model(db, model_id, getattr(current_user, "id", None),
                                           reason=reason, is_rollback=False)
        _ml_audit("model_activated", current_user, model_id,
                  new_version=outcome["version"],
                  previous_version=outcome["previous_version"],
                  degraded=outcome["runtime_degraded"],
                  reason=(reason or "")[:100] or None)
        return {"success": True, **outcome}
    except mts.ActivationError as ae:
        _ml_audit("model_activated", current_user, model_id, result=ae.code)
        raise _ml_error(409 if ae.code in ("INVALID_STATUS", "QUALITY_GATES_FAILED") else 400,
                        ae.code, str(ae))
    except HTTPException:
        raise
    except Exception as e:
        raise _ml_safe_500("model activation", e)


@router.post("/admin/merge-suggestions/models/{model_id}/reject", summary="Reject Candidate Model")
async def reject_similarity_model(
    model_id: int,
    request: Request,
    reason: Optional[str] = Query(default=None, max_length=500),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_role(["admin"])),
    _csrf: None = Depends(_require_ml_csrf)
):
    from backend.core import model_training_service as mts
    try:
        row = await mts.reject_candidate(db, model_id, getattr(current_user, "id", None), reason)
        _ml_audit("candidate_rejected", current_user, model_id,
                  version=row.version, reason=(reason or "")[:100] or None)
        return {"success": True, "model_id": row.id, "status": row.status}
    except mts.ActivationError as ae:
        raise _ml_error(409 if ae.code == "INVALID_STATUS" else 404, ae.code, str(ae))
    except HTTPException:
        raise
    except Exception as e:
        raise _ml_safe_500("candidate rejection", e)


@router.post("/admin/merge-suggestions/models/{model_id}/rollback", summary="Rollback to Archived Model")
async def rollback_similarity_model(
    model_id: int,
    request: Request,
    reason: Optional[str] = Query(default=None, max_length=500),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_role(["admin"])),
    _csrf: None = Depends(_require_ml_csrf)
):
    """Rollback = activate a previously-active archived version (revalidated)."""
    from backend.core import model_training_service as mts
    try:
        outcome = await mts.activate_model(db, model_id, getattr(current_user, "id", None),
                                           reason=reason, is_rollback=True)
        _ml_audit("model_rolled_back", current_user, model_id,
                  new_version=outcome["version"],
                  previous_version=outcome["previous_version"],
                  reason=(reason or "")[:100] or None)
        return {"success": True, **outcome}
    except mts.ActivationError as ae:
        raise _ml_error(409 if ae.code == "INVALID_STATUS" else 400, ae.code, str(ae))
    except HTTPException:
        raise
    except Exception as e:
        raise _ml_safe_500("model rollback", e)


@router.get("/admin/merge-suggestions/models", summary="List Model Versions")
async def list_similarity_models(
    limit: int = Query(default=20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_role(["admin"]))
):
    """Model registry history (no filesystem paths in the response)."""
    try:
        from backend.core import model_training_service as mts
        from db_models import SimilarityModelRegistry
        rows = (await db.execute(
            select(SimilarityModelRegistry)
            .where(SimilarityModelRegistry.model_type == mts.MODEL_TYPE)
            .order_by(SimilarityModelRegistry.created_at.desc())
            .limit(limit)
        )).scalars().all()
        return {"items": [mts.serialize_registry_row(r) for r in rows], "count": len(rows)}
    except Exception as e:
        raise _ml_safe_500("model listing", e)


@router.post("/admin/merge-suggestions/train-model", summary="Train Similarity Model (DEPRECATED)",
             description="DEPRECATED: schedules a background training job. Use /training-jobs.")
async def train_similarity_model(
    request: Request,
    background_tasks: BackgroundTasks,
    min_samples: int = Query(default=50, ge=10, le=1000, description="Minimum samples required for training"),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_role(["admin"])),
    _csrf: None = Depends(_require_ml_csrf)
):
    """Compatibility shim — training no longer runs inside the request."""
    return await create_training_job(request, background_tasks, min_samples, db, current_user)


@router.get("/admin/merge-suggestions/model-status", summary="Get Model Status",
            description="Typed model status: active/candidate versions, DB-backed dataset readiness, runtime health (Admin only)")
async def get_model_status(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_role(["admin"]))
):
    """Strictly-typed status. Sample counts come from the PERSISTENT dataset
    table — never the in-memory list that resets on restart. Filesystem
    paths stay server-side; clients see logical artifact names."""
    try:
        from backend.core.similarity_model import similarity_model
        from backend.core import model_training_service as mts

        runtime = similarity_model.get_status()
        readiness = await mts.dataset_readiness(db)
        active = await mts.get_active_model(db)
        candidate = await mts.get_latest_candidate(db)
        min_samples = int(getattr(settings, 'SIMILARITY_MODEL_MIN_SAMPLES', 50))

        payload = {
            "is_trained": bool(runtime['is_trained']),
            "model_available": bool(runtime['model_available']),
            "sklearn_available": bool(runtime['sklearn_available']),
            "runtime_loaded": bool(runtime['model_available'] and runtime['is_trained']),
            "training_samples": int(readiness["total_samples"]),
            "approved_samples": int(readiness["approved_samples"]),
            "rejected_samples": int(readiness["rejected_samples"]),
            "unique_identity_pairs": int(readiness["unique_identity_pairs"]),
            "min_samples": min_samples,
            "ready_to_train": bool(readiness["ready_to_train"]),
            "readiness_reason": readiness["readiness_reason"],
            "readiness_checks": readiness["readiness_checks"],
            "active_model": mts.serialize_registry_row(active) if active else None,
            "candidate_model": mts.serialize_registry_row(candidate) if candidate else None,
            "training_job_running": mts.running_training_job(),
            "configuration": {
                "auto_train": bool(getattr(settings, 'SIMILARITY_MODEL_AUTO_TRAIN', True)),
                "minimum_samples": min_samples,
                "feature_schema_version": mts.FEATURE_SCHEMA_VERSION,
                "quality_gates": mts.QUALITY_GATES,
                "configuration_source": "config",
                "effective_at": datetime.utcnow().isoformat() + "Z",
            },
        }
        resp = _MLJSONResponse(content=payload)
        resp.headers["Cache-Control"] = "no-store"
        return resp

    except Exception as e:
        raise _ml_safe_500("model status", e)


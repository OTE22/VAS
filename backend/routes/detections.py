"""
Detection Routes
================
Routes for querying detections.
"""

import os
import sys
import logging
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from sqlalchemy.orm import selectinload

# Add parent directory to path
parent_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if parent_dir not in sys.path:
    sys.path.insert(0, parent_dir)

from db_connection import get_db
from backend.utils.pagination import resolve_page, resolve_page_size
from db_models import Detection, Face
from backend.routes.utils import validate_pipeline_id
from backend.auth.auth_service import get_current_user, require_pipeline_access
from db_models import User
from backend.utils.time_utils import iso_utc, utc_now

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Detections"])


@router.get("/api/detections")
async def get_all_detections(
    limit: int = None,
    offset: int = 0,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Get recent detections - filtered by user's pipeline access"""
    # Bounds from configuration; this used to accept any limit a caller sent.
    limit = resolve_page_size(limit, field="limit")
    try:
        from backend.auth.auth_service import AuthService
        
        # Eager-load faces: the response counts d.faces, and the async engine
        # cannot lazy-load a relationship after the request coroutine has left
        # the greenlet (it raised "greenlet_spawn has not been called" and
        # 500'd every call).
        # Get user's accessible pipelines
        if current_user.role == "admin":
            # Admin sees all detections
            query = select(Detection).options(selectinload(Detection.faces))
        else:
            # Regular users only see detections from their assigned pipelines
            user_pipelines = await AuthService.get_user_pipelines(current_user.id, db)
            if not user_pipelines:
                # User has no pipeline access
                return {
                    "total": 0,
                    "detections": []
                }
            query = (select(Detection)
                     .options(selectinload(Detection.faces))
                     .where(Detection.pipeline_id.in_(user_pipelines)))
        
        result = await db.execute(
            query.order_by(Detection.timestamp.desc())
            .limit(limit)
            .offset(offset)
        )
        detections = result.scalars().all()

        return {
            "total": len(detections),
            "detections": [
                {
                    "id": d.id,
                    "pipeline_id": d.pipeline_id,
                    "timestamp": iso_utc(d.timestamp),
                    "processing_time_ms": d.processing_time_ms,
                    "faces_count": len(d.faces) if d.faces else 0,
                }
                for d in detections
            ]
        }
    except Exception as e:
        logger.error(f"Error getting detections: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/api/detections/{pipeline_id}")
async def get_pipeline_detections(
    pipeline_id: str,
    limit: int = None,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_pipeline_access)
):
    """Get detections for specific pipeline with face details"""
    limit = resolve_page_size(limit, field="limit")
    try:
        pipeline_id = validate_pipeline_id(pipeline_id)

        result = await db.execute(
            select(Detection)
            .options(selectinload(Detection.faces))
            .where(Detection.pipeline_id == pipeline_id)
            .order_by(Detection.timestamp.desc())
            .limit(limit)
        )
        detections = result.scalars().all()

        output = []
        for detection in detections:
            # Get faces for this detection
            faces_result = await db.execute(
                select(Face).where(Face.detection_id == detection.id)
            )
            faces = faces_result.scalars().all()

            # Filter out "Unknown" faces - they should only appear in the unknown page
            filtered_faces = [f for f in faces if f.name.lower() != "unknown"]
            
            # Only add detection if it has non-unknown faces
            if filtered_faces:
                output.append({
                    "detection_id": detection.id,
                    "pipeline_id": detection.pipeline_id,
                    "timestamp": iso_utc(detection.timestamp),
                    "processing_time_ms": detection.processing_time_ms,
                    "worker_id": detection.worker_id,
                    "faces": [
                        {
                            "id": f.id,
                            "name": f.name,
                            "similarity": f.similarity,
                            "face_image_path": f.face_image_path,
                            "bbox": [f.bbox_x1, f.bbox_y1, f.bbox_x2, f.bbox_y2],
                        }
                        for f in filtered_faces
                    ]
                })

        return {
            "pipeline_id": pipeline_id,
            "total_detections": len(output),
            "detections": output
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting pipeline detections: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


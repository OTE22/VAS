"""
Management Routes
=================
Routes for system management (cleanup, circuit breaker, face tracker).
"""

import os
import sys
import logging
from fastapi import APIRouter, BackgroundTasks, HTTPException

# Add parent directory to path
parent_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if parent_dir not in sys.path:
    sys.path.insert(0, parent_dir)

from backend.config import FACE_TRACKING_ENABLED
from backend.core import retention_manager, db_circuit_breaker, face_tracker
from backend.routes.utils import validate_pipeline_id

logger = logging.getLogger(__name__)

router = APIRouter()


@router.post("/api/cleanup/manual")
async def manual_cleanup(background_tasks: BackgroundTasks):
    """Manually trigger cleanup of old data"""
    background_tasks.add_task(retention_manager.cleanup_old_data)
    return {"status": "cleanup_scheduled", "message": "Cleanup task started in background"}


@router.get("/api/circuit-breaker/status")
async def circuit_breaker_status():
    """Get circuit breaker status"""
    return await db_circuit_breaker.get_status()


@router.get("/api/face-tracker/stats")
async def face_tracker_stats_fixed():
    """Get face tracker statistics (FIXED)"""
    if not FACE_TRACKING_ENABLED:
        return {"enabled": False, "message": "Face tracking is disabled"}

    try:
        return await face_tracker.get_stats()
    except Exception as e:
        logger.error(f"Face tracker stats error: {e}")
        raise HTTPException(status_code=500, detail=f"Internal server error: {str(e)}")


@router.post("/api/face-tracker/reset/{pipeline_id}")
async def reset_face_tracker(pipeline_id: str):
    """Reset face tracker for a specific pipeline"""
    if not FACE_TRACKING_ENABLED:
        raise HTTPException(status_code=400, detail="Face tracking is disabled")

    try:
        pipeline_id = validate_pipeline_id(pipeline_id)
        async with face_tracker._lock:
            if pipeline_id in face_tracker.tracked_faces:
                del face_tracker.tracked_faces[pipeline_id]
                logger.info(f"Reset face tracker for pipeline: {pipeline_id}")
                return {"success": True, "message": f"Face tracker reset for pipeline {pipeline_id}"}
            else:
                return {"success": True, "message": f"No tracked faces for pipeline {pipeline_id}"}
    except Exception as e:
        logger.error(f"Error resetting face tracker: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


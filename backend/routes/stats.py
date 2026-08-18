"""
Stats Routes
============
Routes for system statistics.
"""

import os
import sys
import logging
from datetime import datetime, timedelta
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Header, Response
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, and_, or_

# Add parent directory to path
parent_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if parent_dir not in sys.path:
    sys.path.insert(0, parent_dir)

from config import settings
from db_connection import get_db
from db_models import Pipeline, Detection, Face, User, Identity, IdentityType, IdentityStatus, LabelState
from backend.config import FACE_TRACKING_ENABLED, CACHE_ENABLED
from backend.core import (
    processing_queue, retention_manager, face_tracker,
    cache_manager
)
from backend.auth.auth_service import get_current_user, AuthService
from backend.utils.time_utils import iso_utc, utc_now

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Statistics"])


@router.get("/api/stats")
async def get_stats(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Get system statistics - filtered by the caller's pipeline access.

    Authentication is required. Previously this endpoint parsed the Bearer
    header by hand and, on any miss, fell through to an unfiltered ("show all
    pipelines") anonymous branch. Two consequences: an unauthenticated caller
    read system-wide aggregates, and a COOKIE-authenticated browser (which
    sends no Authorization header) also hit the anonymous branch and saw every
    pipeline regardless of its role. get_current_user resolves both cookie and
    bearer credentials, so filtering now follows the real user.
    """
    try:
        user_pipelines = None
        if current_user.role == "admin":
            # Admin sees all pipelines
            user_pipelines = None
            logger.info(f"[STATS] Admin {current_user.username} requesting stats - showing all data")
        else:
            # Regular users get their assigned pipelines
            user_pipelines = await AuthService.get_user_pipelines(current_user.id, db)
            logger.info(f"[STATS] User {current_user.username} requesting stats for pipelines: {user_pipelines}")

        # Build queries with optional filtering
        pipeline_query = select(func.count(Pipeline.id)).where(Pipeline.is_active == 1)
        detection_query = select(func.count(Detection.id))
        
        # CRITICAL: Count only KNOWN faces that are DETECTED, RECOGNIZED, and WITHIN RETENTION PERIOD
        # Same logic as dashboard - only faces that would be sent to dashboard
        display_hours = settings.DASHBOARD_FACE_DISPLAY_HOURS
        retention_cutoff = datetime.utcnow() - timedelta(hours=display_hours)
        
        # Count KNOWN faces from:
        # 1. Recent detections (within retention period), OR
        # 2. KNOWN identities seen within retention period (even if detection is older)
        # This matches the dashboard logic exactly
        
        # Subquery: Get KNOWN identities seen within retention period
        known_identities_subquery = select(Identity.id).where(
            and_(
                Identity.type == IdentityType.KNOWN,
                Identity.status == IdentityStatus.ACTIVE,
                Identity.last_seen_at >= retention_cutoff
            )
        ).subquery()
        
        # Count KNOWN faces that are:
        # - From detections within retention period, OR
        # - From KNOWN identities seen within retention period
        face_query = select(func.count(Face.id)).join(
            Detection, Face.detection_id == Detection.id
        ).outerjoin(
            Identity, Face.identity_id == Identity.id
        ).where(
            and_(
                # Only KNOWN faces
                or_(
                    and_(Face.identity_id.isnot(None), Identity.type == IdentityType.KNOWN),
                    Face.label_state == LabelState.AUTO_KNOWN
                ),
                # Within retention period (detection timestamp OR identity last_seen_at)
                or_(
                    Detection.timestamp >= retention_cutoff,
                    Face.identity_id.in_(select(known_identities_subquery.c.id))
                )
            )
        )
        
        # Filter by user's pipeline access if user is authenticated and not admin
        if current_user and user_pipelines is not None:
            if user_pipelines:  # User has assigned pipelines
                # Filter pipelines to only active ones that user has access to
                # Note: Pipeline.id is integer, but pipeline_id is string (the actual identifier)
                pipeline_query = select(func.count(Pipeline.id)).where(
                    Pipeline.is_active == 1,
                    Pipeline.pipeline_id.in_(user_pipelines)
                )
                # Filter detections and faces by pipeline_id
                detection_query = select(func.count(Detection.id)).where(
                    Detection.pipeline_id.in_(user_pipelines)
                )
                # Join Face -> Detection to filter by pipeline_id AND only count KNOWN faces
                # Count only KNOWN faces that are DETECTED, RECOGNIZED, and WITHIN RETENTION PERIOD
                display_hours = settings.DASHBOARD_FACE_DISPLAY_HOURS
                retention_cutoff = datetime.utcnow() - timedelta(hours=display_hours)
                
                # Subquery: Get KNOWN identities seen within retention period
                known_identities_subquery = select(Identity.id).where(
                    and_(
                        Identity.type == IdentityType.KNOWN,
                        Identity.status == IdentityStatus.ACTIVE,
                        Identity.last_seen_at >= retention_cutoff
                    )
                ).subquery()
                
                face_query = select(func.count(Face.id)).join(
                    Detection, Face.detection_id == Detection.id
                ).outerjoin(
                    Identity, Face.identity_id == Identity.id
                ).where(
                    and_(
                        Detection.pipeline_id.in_(user_pipelines),
                        # Only count KNOWN faces
                        or_(
                            and_(Face.identity_id.isnot(None), Identity.type == IdentityType.KNOWN),
                            Face.label_state == LabelState.AUTO_KNOWN
                        ),
                        # Within retention period (detection timestamp OR identity last_seen_at)
                        or_(
                            Detection.timestamp >= retention_cutoff,
                            Face.identity_id.in_(select(known_identities_subquery.c.id))
                        )
                    )
                )
            else:
                # User has no pipeline access - return zeros
                active_pipelines = 0
                total_detections = 0
                total_faces = 0
                return {
                    "service": settings.APP_NAME,
                    "version": settings.VERSION,
                    "timestamp": iso_utc(utc_now()),
                    "pipelines": {
                        "active": 0,
                        "total_detections": 0,
                    },
                    "faces": {"total": 0},
                    "queue": await processing_queue.get_stats(),
                    "storage": await retention_manager.get_storage_stats(),
                    "database": {},
                    "cache": {"enabled": CACHE_ENABLED, "healthy": False},
                    "tracker": {"enabled": FACE_TRACKING_ENABLED},
                    "retention_days": settings.DATA_RETENTION_DAYS,
                }
        
        # Execute queries
        pipelines_result = await db.execute(pipeline_query)
        detections_result = await db.execute(detection_query)
        faces_result = await db.execute(face_query)

        active_pipelines = pipelines_result.scalar() or 0
        total_detections = detections_result.scalar() or 0
        total_faces = faces_result.scalar() or 0

        # Get async stats
        queue_stats = await processing_queue.get_stats()
        storage_stats = await retention_manager.get_storage_stats()

        # Get tracker stats
        tracker_stats = {"enabled": False}
        if FACE_TRACKING_ENABLED:
            try:
                tracker_stats = await face_tracker.get_stats()
            except Exception as e:
                tracker_stats = {"enabled": True, "error": str(e)}

        # Get cache stats
        cache_stats = {"enabled": CACHE_ENABLED, "healthy": False}
        if CACHE_ENABLED:
            try:
                cache_stats["healthy"] = await cache_manager.health_check()
            except Exception:
                pass

        # Get database connection stats
        from db_connection import db_manager
        # get_stats(), not get_connection_stats() — the latter does not exist
        # on DatabaseManager, so this raised AttributeError on EVERY request and
        # the bare except below swallowed it, leaving "database": {} forever.
        # The home page could not report database status as a result.
        db_stats = {}
        try:
            db_stats = db_manager.get_stats()
        except Exception as e:
            logger.warning("[STATS] database stats unavailable: %s", type(e).__name__)

        return {
            "service": settings.APP_NAME,
            "version": settings.VERSION,
            "timestamp": iso_utc(utc_now()),
            "pipelines": {
                "active": active_pipelines,
                "total_detections": total_detections,
            },
            "faces": {"total": total_faces},
            "queue": queue_stats,
            "storage": storage_stats,
            "database": db_stats,
            "cache": cache_stats,
            "tracker": tracker_stats,
            "retention_days": settings.DATA_RETENTION_DAYS,
        }

    except Exception as e:
        logger.error(f"Stats error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/api/dashboard/config")
async def get_dashboard_config(response: Response):
    """
    Get dashboard configuration settings.

    THREE SEPARATE DURATIONS — never conflated:
      face_display_*             how long a known face stays VISIBLE on the dashboard
      alert_notification_window_* cooldown before the same person may re-alert
      database_retention_days    how long detections/files persist in STORAGE
    """
    try:
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
        # Get alert notification window from face tracker
        notification_window_hours = settings.ALERT_NOTIFICATION_WINDOW_HOURS
        display_hours = settings.DASHBOARD_FACE_DISPLAY_HOURS
        
        # Calculate milliseconds
        face_display_ms = int(display_hours * 60 * 60 * 1000)
        alert_notification_window_ms = int(notification_window_hours * 60 * 60 * 1000)
        
        # Log config values for debugging
        logger.info(f"[CONFIG] Dashboard config requested:")
        logger.info(f"[CONFIG]   DASHBOARD_FACE_DISPLAY_HOURS: {display_hours}h ({face_display_ms}ms)")
        logger.info(f"[CONFIG]   ALERT_NOTIFICATION_WINDOW_HOURS: {notification_window_hours}h ({alert_notification_window_ms}ms)")
        logger.info(f"[CONFIG]   ⚠️ Verifying: face_display_ms ({face_display_ms}ms) != alert_notification_window_ms ({alert_notification_window_ms}ms)")
        
        if face_display_ms == alert_notification_window_ms:
            logger.error(f"[CONFIG] ❌ ERROR: face_display_ms equals alert_notification_window_ms! This would cause faces to expire after alert window!")
        
        return {
            "success": True,
            "config": {
                # Face display duration on dashboard (in hours)
                "face_display_hours": display_hours,
                "face_display_ms": face_display_ms,  # In milliseconds for frontend
                
                # Alert notification window (how long between alerts for same person)
                "alert_notification_window_hours": notification_window_hours,
                "alert_notification_window_ms": alert_notification_window_ms,
                
                # Other useful settings
                "show_unknown_on_dashboard": settings.SHOW_UNKNOWN_FACES_ON_DASHBOARD,
                "face_tracking_enabled": FACE_TRACKING_ENABLED,

                # DATABASE/file retention (owned by the backend retention job —
                # the frontend must NEVER hard-code or invent this)
                "database_retention_days": int(settings.DATA_RETENTION_DAYS),
                "retention_source": "settings",

                # Cleanup interval for frontend expiry sweeps (in milliseconds)
                "cleanup_interval_ms": int(settings.DASHBOARD_CLEANUP_INTERVAL_SECONDS * 1000),

                # Upload limits the UI must mirror. upload-modal.js rejected at
                # 5 MB against an enforced 10 MB and told the user "5MB limit",
                # so half of every allowed upload was blocked before it left
                # the browser. Published here because the upload modal appears
                # on non-admin pages that cannot read /api/search/config.
                "max_file_size_bytes": int(settings.MAX_FILE_SIZE),
                "allowed_extensions": sorted(settings.allowed_image_extensions_list),

                "source": "runtime",
                "effective_at": datetime.utcnow().isoformat() + "Z",
            },
            "description": {
                "face_display_hours": "How many hours of detections to show on dashboard",
                "alert_notification_window_hours": "Minimum hours between alerts for same person on same camera",
                "show_unknown_on_dashboard": "Whether unknown faces appear on main dashboard",
                "face_tracking_enabled": "Whether face tracking optimization is enabled"
            }
        }
    except Exception as e:
        logger.error(f"Dashboard config error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error getting dashboard config: {str(e)}")


@router.get("/api/dashboard/pipelines")
async def get_dashboard_pipelines(
    response: Response,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Authoritative pipeline reconciliation endpoint for the dashboard.

    Returns the COMPLETE, access-filtered set of pipelines for the current
    user, explicitly marked `complete: true`. The dashboard may only prune
    cards based on THIS response — never on a paginated/partial listing.

    display_name precedence: admin-approved DB location_name → pipeline_id.
    (Webhook-reported names never overwrite an admin-set DB name — enforced
    in backend/services/image_processing.ensure_pipeline_registered.)
    """
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"

    if current_user.role == "admin":
        rows = (await db.execute(select(Pipeline).order_by(Pipeline.pipeline_id))).scalars().all()
    else:
        accessible = await AuthService.get_user_pipelines(current_user.id, db)
        if not accessible:
            return {"pipelines": [], "complete": True,
                    "generated_at": datetime.utcnow().isoformat() + "Z"}
        rows = (await db.execute(
            select(Pipeline).where(Pipeline.pipeline_id.in_(accessible))
            .order_by(Pipeline.pipeline_id)
        )).scalars().all()

    return {
        "pipelines": [
            {
                "pipeline_id": p.pipeline_id,
                "display_name": (p.location_name or "").strip() or p.pipeline_id,
                "location_name": p.location_name,
                "is_active": bool(p.is_active),
                "last_webhook_at": p.updated_at.isoformat() + "Z" if p.updated_at else None,
            }
            for p in rows
        ],
        "complete": True,
        "generated_at": datetime.utcnow().isoformat() + "Z",
    }

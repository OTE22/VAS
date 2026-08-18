"""
Retention Status & Manual-Run Routes
====================================
Admin endpoints that make the retention system observable and testable:

    GET  /api/admin/retention/status                — stored/effective values, last run, next run
    POST /api/admin/retention/run?dry_run=true      — full selection/counting, deletes NOTHING
    POST /api/admin/retention/run?dry_run=false     — real run (lock-guarded, 409 if running)
"""

import os
import sys
import logging
from datetime import datetime, timedelta
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel
from sqlalchemy import select, text as sa_text
from sqlalchemy.ext.asyncio import AsyncSession

parent_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if parent_dir not in sys.path:
    sys.path.insert(0, parent_dir)

from config import settings
from db_connection import get_db
from db_models import User, SettingsAuditLog
from backend.auth.auth_service import require_role
from backend.core import runtime_settings
from backend.core.data_retention import retention_manager
from backend.utils.time_utils import iso_utc, utc_now

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/admin/retention", tags=["Retention"])

RETENTION_KEYS = (
    "DATA_RETENTION_DAYS", "CLEANUP_INTERVAL_HOURS", "MAX_STORAGE_GB",
    "LOGS_LIFE_TIME_HOURS", "SNAPSHOT_RETENTION_DAYS", "INACTIVE_THRESHOLD_DAYS",
    "IDENTITY_CLEANUP_INTERVAL_HOURS", "MAX_EMBEDDINGS_PER_IDENTITY",
    "SEARCH_HISTORY_RETENTION_DAYS", "SEARCH_HISTORY_MAX_PER_USER",
    "AUDIT_LOG_RETENTION_DAYS",
)


@router.get("/status")
async def retention_status(
    response: Response,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_role(["admin"])),
):
    """Retention configuration + last/next run report."""
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"

    # Per-key stored vs effective vs source
    from db_models import Setting
    rows = (await db.execute(select(Setting).where(Setting.key.in_(RETENTION_KEYS)))).scalars().all()
    stored = {r.key: r for r in rows}

    keys = {}
    for key in RETENTION_KEYS:
        row = stored.get(key)
        meta = runtime_settings.get_meta(key, row.category if row else "retention")
        src = runtime_settings.describe_source(key, row.value if row else None,
                                               row.category if row else "retention")
        keys[key] = {
            "stored_value": src["stored_value"],
            "effective_value": runtime_settings.effective_value(key),
            "default_value": src["default_value"],
            "source": src["source"],
            "apply_mode": meta.apply_mode,
            "unit": meta.unit,
        }

    # Last recorded run from background_task_history (survives restarts)
    last_history = None
    try:
        hist = (await db.execute(sa_text("""
            SELECT status, created_at, completed_at, duration_seconds, details
            FROM background_task_history
            WHERE task_type = 'data_retention' AND status IN ('completed', 'failed')
            ORDER BY created_at DESC LIMIT 1
        """))).first()
        if hist:
            last_history = {
                "status": hist[0],
                "created_at": iso_utc(hist[1]),
                "completed_at": iso_utc(hist[2]),
                "duration_seconds": hist[3],
                "details": hist[4],
            }
    except Exception as e:
        logger.debug(f"[RETENTION] task history unavailable: {e}")

    interval_hours = retention_manager.cleanup_interval_hours
    next_run = None
    if retention_manager.last_run_at:
        next_run = iso_utc(retention_manager.last_run_at + timedelta(hours=interval_hours))

    from backend.core.retention_job import retention_busy
    last = retention_manager.last_result or {}
    dr = keys.get("DATA_RETENTION_DAYS", {})

    return {
        # Flattened summary (stable contract for the monitor page)
        "enabled": True,
        "stored_retention_days": dr.get("stored_value"),
        "effective_retention_days": dr.get("effective_value"),
        "source": dr.get("source"),
        "apply_mode": dr.get("apply_mode"),
        "cleanup_interval_seconds": interval_hours * 3600,
        "currently_running": retention_busy(),
        "last_run_at": iso_utc(retention_manager.last_run_at),
        "last_run_status": last.get("status"),
        "last_run_dry_run": last.get("dry_run"),
        "last_run_duration_seconds": last.get("duration_seconds"),
        "last_rows_scanned": last.get("rows_scanned"),
        "last_candidate_rows": last.get("candidate_rows"),
        "last_deleted_rows": last.get("rows_deleted"),
        "last_deleted_files": last.get("files_deleted"),
        "last_missing_files": last.get("missing_files"),
        "last_freed_bytes": last.get("bytes_freed"),
        "last_errors": (last.get("failures") or [])[:10],
        "next_run_at": next_run,
        # Full detail (kept for the settings page / tests)
        "settings": keys,
        "running": retention_busy(),
        "last_result": retention_manager.last_result,
        "last_recorded_run": last_history,
        "next_scheduled_run": next_run,
        "interval_hours": interval_hours,
    }


RETENTION_CONFIRMATION = "DELETE_EXPIRED_DATA"


class RetentionRunRequest(BaseModel):
    dry_run: bool = True
    confirmation: Optional[str] = None


@router.post("/run", status_code=status.HTTP_202_ACCEPTED)
async def retention_run(
    request: Request,
    body: Optional[RetentionRunRequest] = None,
    dry_run: Optional[bool] = None,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_role(["admin"])),
):
    """Schedule a retention run as a background job and return immediately.

    Body: {"dry_run": true}  — counts candidates, deletes NOTHING.
          {"dry_run": false, "confirmation": "DELETE_EXPIRED_DATA"} — real run.
    (dry_run may also be passed as a query parameter for compatibility.)

    Returns 202 {accepted, job_id, task_id, status} — monitor the job via
    GET /api/tasks/{task_id}. A concurrent run gets 409. Real runs REQUIRE
    the exact confirmation string — no single-click destructive execution.
    """
    # Body is authoritative; the query param is legacy compatibility only.
    if body is not None:
        effective_dry_run = body.dry_run
    elif dry_run is not None:
        effective_dry_run = dry_run
    else:
        effective_dry_run = True

    if not effective_dry_run:
        supplied = body.confirmation if body is not None else None
        if supplied != RETENTION_CONFIRMATION:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Real cleanup requires confirmation='{RETENTION_CONFIRMATION}'",
            )

    from backend.core.retention_job import launch_retention_job
    launched = await launch_retention_job(
        dry_run=effective_dry_run,
        created_by_user_id=current_user.id,
        request_id=getattr(getattr(request, "state", None), "request_id", None),
    )
    if launched is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A retention run is already in progress"
        )

    logger.info(
        "[RETENTION] Manual %s scheduled by %s as job %s",
        "DRY RUN" if effective_dry_run else "EXECUTION",
        current_user.username, launched["job_id"],
    )

    # Audit the action (counts land in the task result — never sensitive data)
    try:
        from backend.utils.identity_audit import get_client_info
        ip_address, user_agent = get_client_info(request)
        audit = SettingsAuditLog(
            setting_key="DATA_RETENTION_DAYS",
            old_value=None,
            new_value=f"job_id={launched['job_id']}",
            value_type="run_summary",
            changed_by_user_id=current_user.id,
            changed_by_username=current_user.username,
            change_reason="manual retention run",
            ip_address=ip_address,
            user_agent=user_agent,
        )
        if hasattr(audit, "action"):
            audit.action = "retention_dry_run" if effective_dry_run else "retention_executed"
        db.add(audit)
        await db.commit()
    except Exception as e:
        logger.warning(f"[RETENTION] Failed to audit manual run: {e}")

    return {"accepted": True, "dry_run": effective_dry_run, **launched}

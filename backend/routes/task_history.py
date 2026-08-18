"""
Background Task API Routes
==========================
Monitoring + control endpoints for background jobs:

    GET  /api/tasks/stats                 SQL-aggregate statistics (admin)
    GET  /api/tasks/history               server-side pagination/filter/sort/search
    GET  /api/tasks/alerts                scheduled tasks starting soon (stable alert ids)
    GET  /api/tasks/upcoming, /running    legacy list endpoints (kept)
    GET  /api/tasks/{task_id}             full task details
    POST /api/tasks/{task_id}/cancel      admin — atomic cancel / cooperative cancel
    POST /api/tasks/{task_id}/retry       admin — relaunch a failed retryable job

All monitoring responses carry Cache-Control: no-store. Detailed error
messages are admin-only; non-admins get a safe reference (correlation id).
"""

import os
import sys
import logging
from datetime import datetime
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Query, Response, status

# Add parent directory to path
parent_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if parent_dir not in sys.path:
    sys.path.insert(0, parent_dir)

from backend.auth.auth_service import get_current_user, require_role
from backend.core.task_history import task_history_manager, STORED_STATUSES, VIRTUAL_STATUSES
from db_models import User
from db_connection import get_db
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Background Tasks"])

NO_STORE = "no-store, no-cache, must-revalidate"

VALID_STATUS_FILTERS = STORED_STATUSES | VIRTUAL_STATUSES
VALID_SORT_FIELDS = {"created_at", "scheduled_time", "started_at", "completed_at",
                     "duration_seconds", "task_name", "task_type", "status", "id"}


def _parse_dt(value: Optional[str], name: str) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        raise HTTPException(status_code=422, detail=f"Invalid {name}: expected ISO-8601 datetime")


def _sanitize_task(task: dict, user_is_admin: bool) -> dict:
    """Non-admins never see raw error internals — only a safe reference."""
    if not user_is_admin and task.get("error_message"):
        task = dict(task)
        task["error_message"] = f"An error occurred (reference: {task.get('correlation_id') or task.get('job_id') or task['id']})"
    return task


@router.get(
    "/api/tasks/stats",
    summary="Get Task Statistics",
    description="Aggregate statistics about background tasks (admin only). Computed with SQL aggregates — no row scanning."
)
async def get_task_stats(
    response: Response,
    window_days: int = Query(30, ge=1, le=365),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_role(["admin"]))
):
    response.headers["Cache-Control"] = NO_STORE
    return await task_history_manager.get_stats(window_days=window_days)


@router.get(
    "/api/tasks/history",
    summary="Get Background Task History (paginated)",
    description="Server-side pagination, filtering, sorting and search over task history."
)
async def get_task_history(
    response: Response,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    task_type: Optional[str] = Query(None, max_length=50),
    status_filter: Optional[str] = Query(None, alias="status",
                                         description="scheduled|running|completed|failed|cancelled|upcoming|overdue"),
    date_from: Optional[str] = Query(None, description="ISO datetime lower bound (created_at)"),
    date_to: Optional[str] = Query(None, description="ISO datetime upper bound (created_at)"),
    search: Optional[str] = Query(None, max_length=100),
    sort_by: str = Query("created_at"),
    sort_order: str = Query("desc"),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    response.headers["Cache-Control"] = NO_STORE

    if status_filter and status_filter not in VALID_STATUS_FILTERS:
        raise HTTPException(status_code=422,
                            detail=f"Invalid status. Allowed: {sorted(VALID_STATUS_FILTERS)}")
    if sort_by not in VALID_SORT_FIELDS:
        raise HTTPException(status_code=422,
                            detail=f"Invalid sort_by. Allowed: {sorted(VALID_SORT_FIELDS)}")
    if sort_order not in ("asc", "desc"):
        raise HTTPException(status_code=422, detail="sort_order must be 'asc' or 'desc'")

    user_is_admin = current_user.role == "admin"
    result = await task_history_manager.get_history_page(
        page=page,
        page_size=page_size,
        task_type=task_type,
        status=status_filter,
        date_from=_parse_dt(date_from, "date_from"),
        date_to=_parse_dt(date_to, "date_to"),
        search=search,
        sort_by=sort_by,
        sort_order=sort_order,
        user_is_admin=user_is_admin,
    )
    result["items"] = [_sanitize_task(t, user_is_admin) for t in result["items"]]
    return result


@router.get(
    "/api/tasks/alerts",
    summary="Get Tasks Requiring Alerts",
    description="Scheduled tasks starting within 1 minute. Each alert carries a stable alert_instance_id for client-side deduplication."
)
async def get_task_alerts(
    response: Response,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    response.headers["Cache-Control"] = NO_STORE
    user_is_admin = current_user.role == "admin"
    alerts = await task_history_manager.get_alerts(user_is_admin=user_is_admin, lead_seconds=60)
    if alerts:
        logger.info(f"[TASK_ALERTS] Returning {len(alerts)} task alert(s)")
    return alerts


@router.get("/api/tasks/upcoming", summary="Get Upcoming Tasks")
async def get_upcoming_tasks(
    response: Response,
    limit: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Scheduled tasks whose scheduled_time is in the future (excludes overdue)."""
    response.headers["Cache-Control"] = NO_STORE
    user_is_admin = current_user.role == "admin"
    return await task_history_manager.get_upcoming_tasks(user_is_admin=user_is_admin, limit=limit)


@router.get("/api/tasks/running", summary="Get Running Tasks")
async def get_running_tasks(
    response: Response,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Currently running background tasks (up to 50), newest first, as a bare JSON array. Non-admin callers get error internals replaced by a correlation reference."""
    response.headers["Cache-Control"] = NO_STORE
    user_is_admin = current_user.role == "admin"
    return await task_history_manager.get_running_tasks(user_is_admin=user_is_admin)


@router.get(
    "/api/tasks/{task_id}",
    summary="Get Task Details",
    description="Full detail for one task: lifecycle timestamps, progress, retries, structured result, correlation ids."
)
async def get_task_detail(
    task_id: int,
    response: Response,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    response.headers["Cache-Control"] = NO_STORE
    user_is_admin = current_user.role == "admin"
    task = await task_history_manager.get_task_by_id(task_id, user_is_admin=user_is_admin)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    return _sanitize_task(task, user_is_admin)


@router.post(
    "/api/tasks/{task_id}/cancel",
    summary="Cancel a Task",
    description="Admin only. Scheduled tasks are cancelled atomically; running jobs receive a cooperative cancel request honored between batches."
)
async def cancel_task(
    task_id: int,
    response: Response,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_role(["admin"]))
):
    response.headers["Cache-Control"] = NO_STORE
    ok, outcome = await task_history_manager.request_cancel(task_id)
    if not ok:
        if outcome == "not_found":
            raise HTTPException(status_code=404, detail="Task not found")
        raise HTTPException(status_code=409, detail=f"Task cannot be cancelled ({outcome})")
    logger.info(f"[TASK] Cancel requested by {current_user.username}: task_id={task_id} outcome={outcome}")
    return {"success": True, "task_id": task_id, "outcome": outcome}


@router.post(
    "/api/tasks/{task_id}/retry",
    summary="Retry a Failed Task",
    description="Admin only. Relaunches a failed/cancelled job of a retryable type (currently: data_retention) as a NEW job linked by correlation_id."
)
async def retry_task(
    task_id: int,
    response: Response,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_role(["admin"]))
):
    response.headers["Cache-Control"] = NO_STORE
    task = await task_history_manager.get_task_by_id(task_id, user_is_admin=True)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    if task["status"] not in ("failed", "cancelled"):
        raise HTTPException(status_code=409,
                            detail=f"Only failed/cancelled tasks can be retried (status={task['status']})")

    if task["task_type"] == "data_retention":
        from backend.core.retention_job import launch_retention_job
        dry_run = bool((task.get("result") or {}).get("dry_run", True))
        launched = await launch_retention_job(
            dry_run=dry_run,
            created_by_user_id=current_user.id,
            correlation_id=task.get("correlation_id") or task.get("job_id"),
            retry_count=(task.get("retry_count") or 0) + 1,
        )
        if launched is None:
            raise HTTPException(status_code=409, detail="A retention run is already in progress")
        logger.info(f"[TASK] Retry by {current_user.username}: task_id={task_id} -> new job {launched['job_id']}")
        return {"success": True, "retried_task_id": task_id, **launched}

    raise HTTPException(status_code=400,
                        detail=f"Task type '{task['task_type']}' is not retryable via the API")

"""
Background Task History Manager
================================
Job lifecycle + storage for background tasks.

Lifecycle (status is a DB column; transitions are atomic UPDATEs):

    scheduled -> running -> completed
    scheduled -> running -> failed
    scheduled -> cancelled

"overdue" is a VIRTUAL status (never stored): status='scheduled' AND
scheduled_time < now. "upcoming" is the complement (scheduled_time >= now).

Every transition emits a structured [TASK] log line so the whole lifecycle
is visible in `docker logs`. Result payloads carry counts and durations
only — never secrets, tokens or biometric data.
"""

import os
import sys
import socket
import logging
from datetime import datetime, timedelta
from typing import List, Optional, Dict, Any, Tuple
from enum import Enum

# Add parent directory to path
parent_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if parent_dir not in sys.path:
    sys.path.insert(0, parent_dir)

from db_connection import db_manager
from db_models import BackgroundTaskHistory
from sqlalchemy import select, update, desc, asc, and_, or_, func

logger = logging.getLogger(__name__)

HOSTNAME = socket.gethostname()
WORKER_NAME = f"api-pid{os.getpid()}"


class TaskStatus(str, Enum):
    """Task execution status (stored values)"""
    SCHEDULED = "scheduled"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


# Virtual statuses accepted by history filters (computed, never stored)
VIRTUAL_STATUSES = {"upcoming", "overdue"}
STORED_STATUSES = {s.value for s in TaskStatus}

SORTABLE_COLUMNS = {
    "created_at": BackgroundTaskHistory.created_at,
    "scheduled_time": BackgroundTaskHistory.scheduled_time,
    "started_at": BackgroundTaskHistory.started_at,
    "completed_at": BackgroundTaskHistory.completed_at,
    "duration_seconds": BackgroundTaskHistory.duration_seconds,
    "task_name": BackgroundTaskHistory.task_name,
    "task_type": BackgroundTaskHistory.task_type,
    "status": BackgroundTaskHistory.status,
    "id": BackgroundTaskHistory.id,
}


def _task_log(job_id, task_type, status, **fields):
    """Structured [TASK] line for docker logs. Counts/ids only — no secrets."""
    extras = " ".join(f"{k}={v}" for k, v in fields.items() if v is not None)
    logger.info(f"[TASK] job_id={job_id or '-'} task_type={task_type} status={status} {extras}".rstrip())


class TaskHistoryManager:
    """Manages background task history storage, lifecycle and retrieval."""

    def __init__(self, max_history_days: int = 30):
        self.max_history_days = max_history_days
        # Cooperative cancellation: job runners poll is_cancel_requested()
        # between batches. In-process only (WORKERS=1 deployment).
        self._cancel_requested: set = set()

    # ------------------------------------------------------------------
    # Job lifecycle (job_id-keyed, atomic transitions)
    # ------------------------------------------------------------------

    async def create_job(
        self,
        job_id: str,
        task_type: str,
        task_name: str,
        description: Optional[str] = None,
        scheduled_time: Optional[datetime] = None,
        created_by_user_id: Optional[int] = None,
        request_id: Optional[str] = None,
        correlation_id: Optional[str] = None,
        max_retries: int = 0,
        notify_all_users: bool = False,
    ) -> int:
        """Create a job row in 'scheduled' state. Returns the task id (-1 on error)."""
        try:
            async with db_manager.get_session() as db:
                row = BackgroundTaskHistory(
                    job_id=job_id,
                    task_type=task_type,
                    task_name=task_name,
                    status=TaskStatus.SCHEDULED.value,
                    description=description,
                    scheduled_time=scheduled_time or datetime.utcnow(),
                    created_by_user_id=created_by_user_id,
                    request_id=request_id,
                    correlation_id=correlation_id or job_id,
                    retry_count=0,
                    max_retries=max_retries,
                    worker_name=WORKER_NAME,
                    hostname=HOSTNAME,
                    notify_all_users=notify_all_users,
                    created_at=datetime.utcnow(),
                )
                db.add(row)
                await db.commit()
                await db.refresh(row)
                _task_log(job_id, task_type, "scheduled", task_id=row.id,
                          scheduled_time=row.scheduled_time.isoformat() if row.scheduled_time else None)
                return row.id
        except Exception as e:
            logger.error(f"[TASK_HISTORY] Failed to create job {job_id}: {e}", exc_info=True)
            return -1

    async def mark_running(self, job_id: str) -> bool:
        """scheduled -> running (atomic; only fires if still scheduled)."""
        try:
            async with db_manager.get_session() as db:
                result = await db.execute(
                    update(BackgroundTaskHistory)
                    .where(BackgroundTaskHistory.job_id == job_id,
                           BackgroundTaskHistory.status == TaskStatus.SCHEDULED.value)
                    .values(status=TaskStatus.RUNNING.value,
                            started_at=datetime.utcnow(),
                            progress_percent=0,
                            updated_at=datetime.utcnow(),
                            worker_name=WORKER_NAME,
                            hostname=HOSTNAME)
                )
                await db.commit()
                ok = (result.rowcount or 0) > 0
                if ok:
                    row = await self.get_task_by_job_id(job_id)
                    _task_log(job_id, row["task_type"] if row else "?", "running")
                return ok
        except Exception as e:
            logger.error(f"[TASK_HISTORY] Failed to mark {job_id} running: {e}", exc_info=True)
            return False

    async def update_progress(self, job_id: str, percent: int,
                              details: Optional[Dict[str, Any]] = None) -> None:
        """Update progress on a running job (best effort)."""
        try:
            percent = max(0, min(100, int(percent)))
            async with db_manager.get_session() as db:
                values = {"progress_percent": percent, "updated_at": datetime.utcnow()}
                if details is not None:
                    values["details"] = details
                await db.execute(
                    update(BackgroundTaskHistory)
                    .where(BackgroundTaskHistory.job_id == job_id,
                           BackgroundTaskHistory.status == TaskStatus.RUNNING.value)
                    .values(**values)
                )
                await db.commit()
        except Exception as e:
            logger.debug(f"[TASK_HISTORY] Progress update failed for {job_id}: {e}")

    async def finish_job(
        self,
        job_id: str,
        success: bool,
        result: Optional[Dict[str, Any]] = None,
        error_code: Optional[str] = None,
        error_message: Optional[str] = None,
        cancelled: bool = False,
    ) -> bool:
        """running -> completed/failed/cancelled (atomic). Computes duration."""
        try:
            now = datetime.utcnow()
            if cancelled:
                final = TaskStatus.CANCELLED.value
            else:
                final = TaskStatus.COMPLETED.value if success else TaskStatus.FAILED.value
            async with db_manager.get_session() as db:
                row = (await db.execute(
                    select(BackgroundTaskHistory).where(BackgroundTaskHistory.job_id == job_id)
                )).scalar_one_or_none()
                if not row:
                    return False
                duration = None
                if row.started_at:
                    duration = round((now - row.started_at).total_seconds(), 2)
                upd = await db.execute(
                    update(BackgroundTaskHistory)
                    .where(BackgroundTaskHistory.job_id == job_id,
                           BackgroundTaskHistory.status.in_(
                               [TaskStatus.RUNNING.value, TaskStatus.SCHEDULED.value]))
                    .values(status=final,
                            success=success and not cancelled,
                            completed_at=now,
                            duration_seconds=duration,
                            progress_percent=100 if (success and not cancelled) else row.progress_percent,
                            result=result,
                            error_code=error_code,
                            error_message=(error_message or "")[:2000] or None,
                            updated_at=now)
                )
                await db.commit()
                self._cancel_requested.discard(job_id)
                ok = (upd.rowcount or 0) > 0
                if ok:
                    _task_log(job_id, row.task_type, final,
                              duration=duration, error_code=error_code)
                return ok
        except Exception as e:
            logger.error(f"[TASK_HISTORY] Failed to finish {job_id}: {e}", exc_info=True)
            return False

    async def request_cancel(self, task_id: int) -> Tuple[bool, str]:
        """Cancel a task. Scheduled tasks flip to 'cancelled' atomically;
        running jobs get a cooperative cancel flag (runner checks between
        batches). Returns (ok, outcome)."""
        try:
            async with db_manager.get_session() as db:
                row = (await db.execute(
                    select(BackgroundTaskHistory).where(BackgroundTaskHistory.id == task_id)
                )).scalar_one_or_none()
                if not row:
                    return False, "not_found"
                if row.status == TaskStatus.SCHEDULED.value:
                    upd = await db.execute(
                        update(BackgroundTaskHistory)
                        .where(BackgroundTaskHistory.id == task_id,
                               BackgroundTaskHistory.status == TaskStatus.SCHEDULED.value)
                        .values(status=TaskStatus.CANCELLED.value,
                                completed_at=datetime.utcnow(),
                                updated_at=datetime.utcnow())
                    )
                    await db.commit()
                    if (upd.rowcount or 0) > 0:
                        _task_log(row.job_id, row.task_type, "cancelled", task_id=task_id)
                        return True, "cancelled"
                    return False, "state_changed"
                if row.status == TaskStatus.RUNNING.value:
                    if row.job_id:
                        self._cancel_requested.add(row.job_id)
                        _task_log(row.job_id, row.task_type, "cancel_requested", task_id=task_id)
                        return True, "cancel_requested"
                    return False, "not_cancellable"
                return False, f"already_{row.status}"
        except Exception as e:
            logger.error(f"[TASK_HISTORY] Cancel failed for task {task_id}: {e}", exc_info=True)
            return False, "error"

    def is_cancel_requested(self, job_id: str) -> bool:
        return job_id in self._cancel_requested

    # ------------------------------------------------------------------
    # Queries (server-side pagination / filtering / sorting / search)
    # ------------------------------------------------------------------

    def _visibility_conditions(self, user_is_admin: bool):
        return [] if user_is_admin else [BackgroundTaskHistory.notify_all_users == True]  # noqa: E712

    async def get_history_page(
        self,
        page: int = 1,
        page_size: int = 20,
        task_type: Optional[str] = None,
        status: Optional[str] = None,
        date_from: Optional[datetime] = None,
        date_to: Optional[datetime] = None,
        search: Optional[str] = None,
        sort_by: str = "created_at",
        sort_order: str = "desc",
        user_is_admin: bool = True,
    ) -> Dict[str, Any]:
        """Authoritative server-side pagination + filtering + sorting.

        `status` accepts stored statuses plus virtual 'upcoming'/'overdue'.
        """
        try:
            now = datetime.utcnow()
            conditions = self._visibility_conditions(user_is_admin)

            if task_type:
                conditions.append(BackgroundTaskHistory.task_type == task_type)

            if status:
                if status == "upcoming":
                    conditions.append(BackgroundTaskHistory.status == TaskStatus.SCHEDULED.value)
                    conditions.append(BackgroundTaskHistory.scheduled_time >= now)
                elif status == "overdue":
                    conditions.append(BackgroundTaskHistory.status == TaskStatus.SCHEDULED.value)
                    conditions.append(BackgroundTaskHistory.scheduled_time < now)
                elif status in STORED_STATUSES:
                    conditions.append(BackgroundTaskHistory.status == status)

            if date_from:
                conditions.append(BackgroundTaskHistory.created_at >= date_from)
            if date_to:
                conditions.append(BackgroundTaskHistory.created_at <= date_to)

            if search:
                needle = f"%{search[:100]}%"
                conditions.append(or_(
                    BackgroundTaskHistory.task_name.ilike(needle),
                    BackgroundTaskHistory.description.ilike(needle),
                    BackgroundTaskHistory.task_type.ilike(needle),
                    BackgroundTaskHistory.job_id.ilike(needle),
                ))

            where = and_(*conditions) if conditions else None

            sort_col = SORTABLE_COLUMNS.get(sort_by, BackgroundTaskHistory.created_at)
            order = desc(sort_col) if sort_order != "asc" else asc(sort_col)

            async with db_manager.get_session() as db:
                count_q = select(func.count(BackgroundTaskHistory.id))
                if where is not None:
                    count_q = count_q.where(where)
                total = (await db.execute(count_q)).scalar() or 0

                total_pages = max(1, (total + page_size - 1) // page_size)
                page = max(1, min(page, total_pages))

                q = select(BackgroundTaskHistory)
                if where is not None:
                    q = q.where(where)
                q = q.order_by(order, desc(BackgroundTaskHistory.id))
                q = q.limit(page_size).offset((page - 1) * page_size)

                rows = (await db.execute(q)).scalars().all()

                return {
                    "items": [self._task_to_dict(t, now=now) for t in rows],
                    "total": total,
                    "page": page,
                    "page_size": page_size,
                    "total_pages": total_pages,
                }
        except Exception as e:
            logger.error(f"[TASK_HISTORY] Failed to get history page: {e}", exc_info=True)
            return {"items": [], "total": 0, "page": 1, "page_size": page_size, "total_pages": 1}

    async def get_task_by_id(self, task_id: int,
                             user_is_admin: bool = True) -> Optional[Dict[str, Any]]:
        try:
            async with db_manager.get_session() as db:
                conditions = [BackgroundTaskHistory.id == task_id]
                conditions += self._visibility_conditions(user_is_admin)
                row = (await db.execute(
                    select(BackgroundTaskHistory).where(and_(*conditions))
                )).scalar_one_or_none()
                return self._task_to_dict(row) if row else None
        except Exception as e:
            logger.error(f"[TASK_HISTORY] Failed to get task {task_id}: {e}", exc_info=True)
            return None

    async def get_task_by_job_id(self, job_id: str) -> Optional[Dict[str, Any]]:
        try:
            async with db_manager.get_session() as db:
                row = (await db.execute(
                    select(BackgroundTaskHistory).where(BackgroundTaskHistory.job_id == job_id)
                )).scalar_one_or_none()
                return self._task_to_dict(row) if row else None
        except Exception as e:
            logger.error(f"[TASK_HISTORY] Failed to get job {job_id}: {e}", exc_info=True)
            return None

    async def get_stats(self, window_days: int = 30) -> Dict[str, Any]:
        """SQL-aggregate statistics — never loads task rows into memory."""
        try:
            now = datetime.utcnow()
            since = now - timedelta(days=window_days)
            async with db_manager.get_session() as db:
                by_status_rows = (await db.execute(
                    select(BackgroundTaskHistory.status, func.count(BackgroundTaskHistory.id))
                    .where(BackgroundTaskHistory.created_at >= since)
                    .group_by(BackgroundTaskHistory.status)
                )).all()
                by_status = {row[0]: row[1] for row in by_status_rows}

                overdue = (await db.execute(
                    select(func.count(BackgroundTaskHistory.id))
                    .where(BackgroundTaskHistory.status == TaskStatus.SCHEDULED.value,
                           BackgroundTaskHistory.scheduled_time < now)
                )).scalar() or 0

                upcoming = (await db.execute(
                    select(func.count(BackgroundTaskHistory.id))
                    .where(BackgroundTaskHistory.status == TaskStatus.SCHEDULED.value,
                           BackgroundTaskHistory.scheduled_time >= now)
                )).scalar() or 0

                by_type_rows = (await db.execute(
                    select(BackgroundTaskHistory.task_type,
                           BackgroundTaskHistory.status,
                           func.count(BackgroundTaskHistory.id))
                    .where(BackgroundTaskHistory.created_at >= since)
                    .group_by(BackgroundTaskHistory.task_type, BackgroundTaskHistory.status)
                )).all()
                by_type: Dict[str, Dict[str, int]] = {}
                for ttype, tstatus, count in by_type_rows:
                    bucket = by_type.setdefault(ttype, {"total": 0, "completed": 0, "failed": 0, "cancelled": 0})
                    bucket["total"] += count
                    if tstatus in ("completed", "failed", "cancelled"):
                        bucket[tstatus] += count

            completed = by_status.get("completed", 0)
            failed = by_status.get("failed", 0)
            finished = completed + failed
            return {
                "window_days": window_days,
                "total_tasks": sum(by_status.values()),
                "completed": completed,
                "failed": failed,
                "running": by_status.get("running", 0),
                "scheduled": by_status.get("scheduled", 0),
                "cancelled": by_status.get("cancelled", 0),
                "overdue": overdue,
                "upcoming": upcoming,
                "success_rate": (completed / finished * 100) if finished else 0,
                "by_type": by_type,
            }
        except Exception as e:
            logger.error(f"[TASK_HISTORY] Failed to get stats: {e}", exc_info=True)
            return {"total_tasks": 0, "completed": 0, "failed": 0, "running": 0,
                    "scheduled": 0, "cancelled": 0, "overdue": 0, "upcoming": 0,
                    "success_rate": 0, "by_type": {}}

    async def get_alerts(self, user_is_admin: bool = True,
                         lead_seconds: int = 60) -> List[Dict[str, Any]]:
        """Scheduled tasks starting within lead_seconds. Each carries a stable
        alert_instance_id so the frontend can deduplicate across polls."""
        try:
            now = datetime.utcnow()
            horizon = now + timedelta(seconds=lead_seconds)
            conditions = [
                BackgroundTaskHistory.status == TaskStatus.SCHEDULED.value,
                BackgroundTaskHistory.scheduled_time >= now,
                BackgroundTaskHistory.scheduled_time <= horizon,
            ]
            conditions += self._visibility_conditions(user_is_admin)
            async with db_manager.get_session() as db:
                rows = (await db.execute(
                    select(BackgroundTaskHistory).where(and_(*conditions))
                    .order_by(BackgroundTaskHistory.scheduled_time).limit(50)
                )).scalars().all()

            alerts = []
            for t in rows:
                sched_iso = t.scheduled_time.isoformat() + "Z"
                alerts.append({
                    "task_id": t.id,
                    "job_id": t.job_id,
                    "task_name": t.task_name,
                    "task_type": t.task_type,
                    "alert_type": "starting_soon",
                    "scheduled_time": sched_iso,
                    "alert_instance_id": f"{t.id}:{sched_iso}:starting_soon",
                    "starts_in_seconds": max(0, int((t.scheduled_time - now).total_seconds())),
                })
            return alerts
        except Exception as e:
            logger.error(f"[TASK_HISTORY] Failed to get alerts: {e}", exc_info=True)
            return []

    # ------------------------------------------------------------------
    # Legacy recorders (kept for existing callers: notifier, relationship task)
    # ------------------------------------------------------------------

    async def record_task_scheduled(self, task_type, task_name, description,
                                    scheduled_time, notify_all_users=False) -> int:
        """Record a scheduled task. Reuses an existing open 'scheduled' row of
        the same type (job_id-less) instead of inserting duplicates."""
        try:
            async with db_manager.get_session() as db:
                existing = (await db.execute(
                    select(BackgroundTaskHistory)
                    .where(BackgroundTaskHistory.task_type == task_type,
                           BackgroundTaskHistory.status == TaskStatus.SCHEDULED.value,
                           BackgroundTaskHistory.job_id.is_(None))
                    .order_by(desc(BackgroundTaskHistory.created_at)).limit(1)
                )).scalar_one_or_none()
                if existing:
                    existing.scheduled_time = scheduled_time
                    existing.task_name = task_name
                    existing.description = description
                    existing.updated_at = datetime.utcnow()
                    await db.commit()
                    _task_log(None, task_type, "scheduled", task_id=existing.id, reused=True)
                    return existing.id
                row = BackgroundTaskHistory(
                    task_type=task_type, task_name=task_name,
                    status=TaskStatus.SCHEDULED.value, description=description,
                    scheduled_time=scheduled_time, notify_all_users=notify_all_users,
                    worker_name=WORKER_NAME, hostname=HOSTNAME,
                    created_at=datetime.utcnow(),
                )
                db.add(row)
                await db.commit()
                await db.refresh(row)
                _task_log(None, task_type, "scheduled", task_id=row.id)
                return row.id
        except Exception as e:
            logger.error(f"[TASK_HISTORY] Failed to record scheduled task: {e}", exc_info=True)
            return -1

    async def record_task_started(self, task_type, task_name, description=None,
                                  notify_all_users=False) -> int:
        """Record that a task started. Promotes the open scheduled row if any."""
        try:
            async with db_manager.get_session() as db:
                existing = (await db.execute(
                    select(BackgroundTaskHistory)
                    .where(BackgroundTaskHistory.task_type == task_type,
                           BackgroundTaskHistory.status == TaskStatus.SCHEDULED.value,
                           BackgroundTaskHistory.job_id.is_(None))
                    .order_by(desc(BackgroundTaskHistory.created_at)).limit(1)
                )).scalar_one_or_none()
                if existing:
                    existing.status = TaskStatus.RUNNING.value
                    existing.started_at = datetime.utcnow()
                    existing.updated_at = datetime.utcnow()
                    await db.commit()
                    _task_log(None, task_type, "running", task_id=existing.id)
                    return existing.id
                row = BackgroundTaskHistory(
                    task_type=task_type, task_name=task_name,
                    status=TaskStatus.RUNNING.value, description=description,
                    started_at=datetime.utcnow(), notify_all_users=notify_all_users,
                    worker_name=WORKER_NAME, hostname=HOSTNAME,
                    created_at=datetime.utcnow(),
                )
                db.add(row)
                await db.commit()
                await db.refresh(row)
                _task_log(None, task_type, "running", task_id=row.id)
                return row.id
        except Exception as e:
            logger.error(f"[TASK_HISTORY] Failed to record started task: {e}", exc_info=True)
            return -1

    async def record_task_completed(self, task_type, task_name, success,
                                    duration_seconds=None, details=None,
                                    notify_all_users=False) -> int:
        """Record completion. CLOSES the open scheduled/running row of the same
        type instead of leaving it 'scheduled' forever (the old overdue-pileup
        bug); inserts a fresh completed row only when none is open."""
        final = TaskStatus.COMPLETED.value if success else TaskStatus.FAILED.value
        now = datetime.utcnow()
        try:
            async with db_manager.get_session() as db:
                existing = (await db.execute(
                    select(BackgroundTaskHistory)
                    .where(BackgroundTaskHistory.task_type == task_type,
                           BackgroundTaskHistory.status.in_(
                               [TaskStatus.SCHEDULED.value, TaskStatus.RUNNING.value]),
                           BackgroundTaskHistory.job_id.is_(None))
                    .order_by(desc(BackgroundTaskHistory.created_at)).limit(1)
                )).scalar_one_or_none()
                if existing:
                    existing.status = final
                    existing.success = success
                    existing.completed_at = now
                    if not existing.started_at:
                        existing.started_at = now - timedelta(seconds=duration_seconds or 0)
                    existing.duration_seconds = duration_seconds
                    existing.details = details
                    existing.result = details
                    existing.progress_percent = 100 if success else existing.progress_percent
                    existing.updated_at = now
                    await db.commit()
                    _task_log(None, task_type, final, task_id=existing.id,
                              duration=duration_seconds)
                    return existing.id
                row = BackgroundTaskHistory(
                    task_type=task_type, task_name=task_name, status=final,
                    success=success,
                    started_at=now - timedelta(seconds=duration_seconds or 0),
                    completed_at=now, duration_seconds=duration_seconds,
                    details=details, result=details,
                    progress_percent=100 if success else None,
                    notify_all_users=notify_all_users,
                    worker_name=WORKER_NAME, hostname=HOSTNAME, created_at=now,
                )
                db.add(row)
                await db.commit()
                await db.refresh(row)
                _task_log(None, task_type, final, task_id=row.id, duration=duration_seconds)
                return row.id
        except Exception as e:
            logger.error(f"[TASK_HISTORY] Failed to record completed task: {e}", exc_info=True)
            return -1

    # ------------------------------------------------------------------
    # Legacy list APIs (still used by /upcoming and /running routes)
    # ------------------------------------------------------------------

    async def get_task_history(self, task_type=None, status=None, limit=100,
                               offset=0, user_is_admin=True) -> List[Dict[str, Any]]:
        page = await self.get_history_page(
            page=(offset // max(1, limit)) + 1, page_size=limit,
            task_type=task_type, status=status, user_is_admin=user_is_admin,
        )
        return page["items"]

    async def get_upcoming_tasks(self, user_is_admin=True, limit=20) -> List[Dict[str, Any]]:
        page = await self.get_history_page(
            page=1, page_size=limit, status="upcoming",
            sort_by="scheduled_time", sort_order="asc", user_is_admin=user_is_admin,
        )
        return page["items"]

    async def get_running_tasks(self, user_is_admin=True) -> List[Dict[str, Any]]:
        page = await self.get_history_page(
            page=1, page_size=50, status="running",
            sort_by="started_at", sort_order="desc", user_is_admin=user_is_admin,
        )
        return page["items"]

    async def cleanup_old_history(self) -> int:
        """Remove task history older than max_history_days."""
        try:
            cutoff_date = datetime.utcnow() - timedelta(days=self.max_history_days)
            async with db_manager.get_session() as db:
                from sqlalchemy import delete
                result = await db.execute(
                    delete(BackgroundTaskHistory).where(
                        BackgroundTaskHistory.created_at < cutoff_date
                    )
                )
                await db.commit()
                deleted_count = result.rowcount
                logger.info(f"[TASK_HISTORY] Cleaned up {deleted_count} old task history records")
                return deleted_count
        except Exception as e:
            logger.error(f"[TASK_HISTORY] Failed to cleanup old history: {e}", exc_info=True)
            return 0

    # ------------------------------------------------------------------

    def _task_to_dict(self, task: BackgroundTaskHistory,
                      now: Optional[datetime] = None) -> Dict[str, Any]:
        now = now or datetime.utcnow()
        is_overdue = (
            task.status == TaskStatus.SCHEDULED.value
            and task.scheduled_time is not None
            and task.scheduled_time < now
        )
        return {
            "id": task.id,
            "job_id": task.job_id,
            "task_type": task.task_type,
            "task_name": task.task_name,
            "status": task.status,
            "effective_status": "overdue" if is_overdue else task.status,
            "is_overdue": is_overdue,
            "description": task.description,
            "scheduled_time": task.scheduled_time.isoformat() if task.scheduled_time else None,
            "started_at": task.started_at.isoformat() if task.started_at else None,
            "completed_at": task.completed_at.isoformat() if task.completed_at else None,
            "duration_seconds": task.duration_seconds,
            "progress_percent": task.progress_percent,
            "success": task.success,
            "details": task.details,
            "result": task.result,
            "retry_count": task.retry_count,
            "max_retries": task.max_retries,
            "error_code": task.error_code,
            "error_message": task.error_message,
            "created_by_user_id": task.created_by_user_id,
            "request_id": task.request_id,
            "correlation_id": task.correlation_id,
            "worker_name": task.worker_name,
            "hostname": task.hostname,
            "notify_all_users": task.notify_all_users,
            "created_at": task.created_at.isoformat() if task.created_at else None,
            "updated_at": task.updated_at.isoformat() if task.updated_at else None,
        }


# Global instance
task_history_manager = TaskHistoryManager()

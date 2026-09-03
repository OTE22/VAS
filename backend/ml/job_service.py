"""Durable command boundary for expensive ML operations.

HTTP and scheduled callers only validate and enqueue.  The independent
``backend.ml.worker`` process owns execution, leasing and crash detection.
The existing background_task_history row is both queue item and public job
record, avoiding dual-write lifecycle drift.
"""

import uuid as uuid_mod
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from db_models import BackgroundTaskHistory


ML_QUEUE = "ml"
ML_TASK_TYPES = (
    "ml_training",
    "ml_feature_computation",
    "ml_dataset_build",
    "ml_dataset_hash_backfill",
    "ml_drift_check",
)
TERMINAL_STATUSES = ("completed", "failed", "cancelled")
ACTIVE_STATUSES = ("scheduled", "running")

_JOB_DEFINITIONS = {
    "training": {
        "prefix": "mltrain", "task_type": "ml_training",
        "task_name": "ML Anomaly Model Training",
    },
    "collection": {
        "prefix": "mlcollect", "task_type": "ml_feature_computation",
        "task_name": "ML Feature Collection",
    },
    "dataset": {
        "prefix": "mldataset", "task_type": "ml_dataset_build",
        "task_name": "ML Dataset Build",
    },
    "backfill": {
        "prefix": "mlbackfill", "task_type": "ml_dataset_hash_backfill",
        "task_name": "ML Dataset Hash Verification",
    },
    "drift": {
        "prefix": "mldrift", "task_type": "ml_drift_check",
        "task_name": "ML Drift Check",
    },
}


class MLJobConflict(RuntimeError):
    def __init__(self, existing: Optional[Dict[str, Any]] = None):
        super().__init__("an active job of this type already exists")
        self.existing = existing or {}


def job_kind(task_type: Optional[str]) -> str:
    for kind, definition in _JOB_DEFINITIONS.items():
        if definition["task_type"] == task_type:
            return kind
    return "unknown"


async def _active_job(db: AsyncSession, task_type: str):
    return (await db.execute(
        select(BackgroundTaskHistory)
        .where(
            BackgroundTaskHistory.queue_name == ML_QUEUE,
            BackgroundTaskHistory.task_type == task_type,
            BackgroundTaskHistory.status.in_(ACTIVE_STATUSES),
        )
        .order_by(BackgroundTaskHistory.created_at.desc())
        .limit(1)
    )).scalar_one_or_none()


async def enqueue_ml_job(
    db: AsyncSession,
    *,
    kind: str,
    payload: Dict[str, Any],
    description: str,
    created_by_user_id: Optional[int] = None,
    request_id: Optional[str] = None,
    correlation_id: Optional[str] = None,
    job_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Persist a validated command in the caller's transaction.

    The API writes the queue item and its ML audit event in one commit.  The
    migration's partial unique index is the final cross-process single-flight
    guard; this early lookup provides a useful conflict response.
    """
    definition = _JOB_DEFINITIONS.get(kind)
    if definition is None:
        raise ValueError(f"unknown ML job kind {kind!r}")
    existing = await _active_job(db, definition["task_type"])
    if existing is not None:
        from backend.core.task_history import task_history_manager
        raise MLJobConflict(task_history_manager._task_to_dict(existing))

    job_id = job_id or f"{definition['prefix']}-{uuid_mod.uuid4().hex[:8]}"
    now = datetime.utcnow()
    row = BackgroundTaskHistory(
        job_id=job_id,
        task_type=definition["task_type"],
        task_name=definition["task_name"],
        status="scheduled",
        description=description[:2000],
        scheduled_time=now,
        progress_percent=0,
        details={"stage": "queued"},
        created_by_user_id=created_by_user_id,
        request_id=request_id,
        correlation_id=correlation_id or job_id,
        retry_count=0,
        max_retries=0,
        queue_name=ML_QUEUE,
        payload=dict(payload),
        created_at=now,
        updated_at=now,
    )
    try:
        # Preserve the caller's outer transaction if the uniqueness constraint
        # detects a race between two API processes.
        async with db.begin_nested():
            db.add(row)
            await db.flush()
    except IntegrityError:
        existing = await _active_job(db, definition["task_type"])
        from backend.core.task_history import task_history_manager
        raise MLJobConflict(
            task_history_manager._task_to_dict(existing) if existing else None
        )
    return {
        "accepted": True,
        "job_id": job_id,
        "task_id": row.id,
        "status": "scheduled",
        "task_type": row.task_type,
        "kind": kind,
    }


async def list_ml_jobs(*, statuses: Optional[List[str]] = None,
                       limit: int = 100) -> List[Dict[str, Any]]:
    from backend.core.task_history import task_history_manager
    jobs = await task_history_manager.list_queued_jobs(
        queue_name=ML_QUEUE,
        statuses=statuses,
        task_types=list(ML_TASK_TYPES),
        limit=limit,
    )
    for job in jobs:
        job["kind"] = job_kind(job.get("task_type"))
    return jobs


async def ml_worker_health(db: AsyncSession, *, lease_seconds: int) -> Dict[str, Any]:
    """Latest executor heartbeat, without exposing host/process identifiers."""
    from db_models import MLWorkerHeartbeat

    row = (await db.execute(
        select(MLWorkerHeartbeat).order_by(MLWorkerHeartbeat.heartbeat_at.desc()).limit(1)
    )).scalar_one_or_none()
    if row is None:
        return {"status": "offline", "heartbeat_at": None, "current_job_id": None}
    stale_before = datetime.utcnow() - timedelta(seconds=max(20, lease_seconds * 2))
    status = "stale" if row.heartbeat_at < stale_before else "healthy"
    return {
        "status": status,
        "heartbeat_at": row.heartbeat_at.isoformat() + "Z",
        "worker_state": row.status,
        "current_job_id": row.current_job_id,
    }

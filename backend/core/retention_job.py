"""
Retention Background Job Runner
===============================
Runs retention cleanup as a tracked background job instead of inside the
HTTP request. The API returns immediately with a job_id; the job row in
background_task_history carries progress and the final structured result.

Overlap protection is layered:
  1. a synchronous in-loop reservation here (closes the check-then-spawn race)
  2. retention_manager._run_lock (in-process)
  3. a Postgres advisory lock (cross-process)
"""

import asyncio
import logging
import uuid
from typing import Optional

from backend.core.task_history import task_history_manager
from backend.core.data_retention import retention_manager

logger = logging.getLogger(__name__)

# job_id reserved between the HTTP handler's availability check and the
# moment the spawned task actually acquires the run lock. Single event
# loop => setting it synchronously here is race-free.
_pending_job: Optional[str] = None


def retention_busy() -> bool:
    return _pending_job is not None or retention_manager._run_lock.locked()


async def launch_retention_job(
    dry_run: bool,
    created_by_user_id: Optional[int] = None,
    request_id: Optional[str] = None,
    correlation_id: Optional[str] = None,
    retry_count: int = 0,
) -> Optional[dict]:
    """Create the job record and spawn the runner. Returns
    {job_id, task_id, status} or None if a run is already in progress."""
    global _pending_job
    if retention_busy():
        return None

    job_id = f"retention-{uuid.uuid4().hex[:8]}"
    _pending_job = job_id

    task_id = await task_history_manager.create_job(
        job_id=job_id,
        task_type="data_retention",
        task_name="Data Retention " + ("Dry Run" if dry_run else "Cleanup"),
        description=("Counts what WOULD be deleted — deletes nothing." if dry_run
                     else "Deletes expired detections, faces and their image files."),
        created_by_user_id=created_by_user_id,
        request_id=request_id,
        correlation_id=correlation_id,
        notify_all_users=not dry_run,
    )
    if task_id < 0:
        _pending_job = None
        return None

    if retry_count:
        # best-effort: record how many times this chain has been retried
        try:
            from db_connection import db_manager
            from db_models import BackgroundTaskHistory
            from sqlalchemy import update
            async with db_manager.get_session() as db:
                await db.execute(update(BackgroundTaskHistory)
                                 .where(BackgroundTaskHistory.job_id == job_id)
                                 .values(retry_count=retry_count))
                await db.commit()
        except Exception:
            pass

    asyncio.create_task(_run_retention_job(job_id, dry_run))
    logger.info(f"[TASK] job_id={job_id} task_type=data_retention status=accepted dry_run={dry_run} task_id={task_id}")
    return {"job_id": job_id, "task_id": task_id, "status": "scheduled"}


async def _run_retention_job(job_id: str, dry_run: bool):
    global _pending_job
    try:
        await task_history_manager.mark_running(job_id)

        async def progress_cb(percent: int, details: dict):
            await task_history_manager.update_progress(job_id, percent, details)
            logger.info(f"[TASK] job_id={job_id} task_type=data_retention status=running progress={percent}")

        def cancel_check() -> bool:
            return task_history_manager.is_cancel_requested(job_id)

        result = await retention_manager.cleanup_old_data(
            dry_run=dry_run, job_id=job_id,
            progress_cb=progress_cb, cancel_check=cancel_check,
        )

        run_status = result.get("status")
        cancelled = run_status == "cancelled"
        success = run_status == "completed"
        error_code = None
        error_message = None
        if run_status == "skipped":
            error_code = "ALREADY_RUNNING"
            error_message = result.get("reason")
        elif run_status == "failed":
            error_code = "RETENTION_FAILED"
            error_message = "; ".join(str(f) for f in result.get("failures", [])[:3]) or "retention run failed"

        await task_history_manager.finish_job(
            job_id, success=success, result=result,
            error_code=error_code, error_message=error_message,
            cancelled=cancelled,
        )
    except Exception as e:
        logger.error(f"[TASK] job_id={job_id} task_type=data_retention status=failed error={e}", exc_info=True)
        try:
            await task_history_manager.finish_job(
                job_id, success=False, result=None,
                error_code="INTERNAL_ERROR", error_message=str(e),
            )
        except Exception:
            pass
    finally:
        if _pending_job == job_id:
            _pending_job = None

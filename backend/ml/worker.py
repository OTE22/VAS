"""Independent durable ML queue worker.

Run as its own container::

    python -m backend.ml.worker

The supervisor process owns the PostgreSQL lease and starts each expensive
operation in a child process.  CPU-bound sklearn and synchronous Parquet I/O
therefore cannot block the API or the lease heartbeat.  Jobs are serialized in
this first production topology to bound CPU, memory and artifact contention.
"""

import argparse
import asyncio
import logging
import os
import socket
import sys
from datetime import datetime, timedelta
from time import monotonic
from typing import Any, Dict, Optional

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from config import settings
from db_connection import db_manager
from db_models import BackgroundTaskHistory
from backend.core.task_history import task_history_manager
from backend.ml.job_service import ML_QUEUE


logger = logging.getLogger(__name__)
WORKER_ID = os.getenv("ML_WORKER_ID") or f"ml-worker:{socket.gethostname()}:{os.getpid()}"


def _seconds(name: str, default: float, minimum: float) -> float:
    value = getattr(settings, name, default)
    try:
        return max(minimum, float(value))
    except (TypeError, ValueError):
        return default


async def _ensure_db() -> None:
    if not getattr(db_manager, "_initialized", False):
        await db_manager.init_db()


async def _job_control(job_id: str) -> Optional[Dict[str, Any]]:
    async with db_manager.get_session() as db:
        row = (await db.execute(
            select(BackgroundTaskHistory).where(BackgroundTaskHistory.job_id == job_id)
        )).scalar_one_or_none()
        if row is None:
            return None
        return {
            "status": row.status,
            "cancel_requested": row.cancel_requested_at is not None,
            "lease_owner": row.lease_owner,
        }


async def _heartbeat(
    status: str,
    current_job_id: Optional[str] = None,
    *,
    register: bool = False,
) -> None:
    from db_models import MLWorkerHeartbeat

    now = datetime.utcnow()
    async with db_manager.get_session() as db:
        updates = {
            "status": status,
            "current_job_id": current_job_id,
            "heartbeat_at": now,
        }
        if register:
            # A stable deployment ID deliberately reuses one row across
            # container replacements. Refresh process metadata only at boot.
            updates.update({
                "hostname": socket.gethostname(),
                "process_id": os.getpid(),
                "started_at": now,
            })
        stmt = pg_insert(MLWorkerHeartbeat).values(
            worker_id=WORKER_ID, hostname=socket.gethostname(), process_id=os.getpid(),
            status=status, current_job_id=current_job_id,
            started_at=now, heartbeat_at=now,
        ).on_conflict_do_update(
            index_elements=["worker_id"],
            set_=updates,
        )
        await db.execute(stmt)
        await db.commit()


async def _terminate(process: asyncio.subprocess.Process, grace_seconds: float) -> None:
    if process.returncode is not None:
        return
    process.terminate()
    try:
        await asyncio.wait_for(process.wait(), timeout=grace_seconds)
    except asyncio.TimeoutError:
        process.kill()
        await process.wait()


async def _supervise(job: Dict[str, Any]) -> None:
    job_id = str(job["job_id"])
    lease_seconds = int(_seconds("ML_JOB_LEASE_SECONDS", 60, 20))
    heartbeat_seconds = min(
        _seconds("ML_JOB_HEARTBEAT_SECONDS", 10, 1),
        max(1.0, lease_seconds / 3.0),
    )
    terminate_grace = _seconds("ML_JOB_TERMINATE_GRACE_SECONDS", 15, 1)
    logger.info("[ML_WORKER] starting job_id=%s task_type=%s", job_id, job.get("task_type"))
    await _heartbeat("running", job_id)
    process = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "backend.ml.worker", "--execute-job", job_id
    )
    cancelled = False
    lease_lost = False
    while process.returncode is None:
        try:
            await asyncio.wait_for(process.wait(), timeout=heartbeat_seconds)
            break
        except asyncio.TimeoutError:
            pass
        control = await _job_control(job_id)
        await _heartbeat("running", job_id)
        if control is None:
            lease_lost = True
            await _terminate(process, terminate_grace)
            break
        if control["status"] in ("completed", "failed", "cancelled"):
            # Domain runner has committed the terminal state; let it finish
            # final logging without treating the cleared lease as a failure,
            # but never let a post-commit process hang occupy the worker.
            try:
                await asyncio.wait_for(process.wait(), timeout=terminate_grace)
            except asyncio.TimeoutError:
                await _terminate(process, terminate_grace)
            break
        if control["cancel_requested"]:
            cancelled = True
            await _terminate(process, terminate_grace)
            break
        renewed = await task_history_manager.renew_job_lease(
            job_id, lease_owner=WORKER_ID, lease_seconds=lease_seconds
        )
        if not renewed:
            lease_lost = True
            await _terminate(process, terminate_grace)
            break

    return_code = await process.wait()
    current = await task_history_manager.get_task_by_job_id(job_id)
    if current and current.get("status") == "running":
        if cancelled:
            await task_history_manager.finish_job(
                job_id, success=False, cancelled=True, error_code="CANCELLED",
                error_message="job cancelled by an administrator",
            )
        else:
            code = "WORKER_LEASE_LOST" if lease_lost else "ML_JOB_PROCESS_FAILED"
            await task_history_manager.finish_job(
                job_id, success=False, error_code=code,
                error_message=f"ML job process exited with status {return_code}",
            )
    logger.info("[ML_WORKER] finished job_id=%s exit_code=%s", job_id, return_code)
    await _heartbeat("idle")


async def _enqueue_scheduled_drift_if_due() -> None:
    """Replace the API-process drift loop with a durable scheduled command."""
    from backend.ml.job_service import enqueue_ml_job, MLJobConflict

    interval = _seconds("ML_DRIFT_CHECK_INTERVAL_HOURS", 24, 1) * 3600
    cutoff = datetime.utcnow() - timedelta(seconds=interval)
    async with db_manager.get_session() as db:
        recent = (await db.execute(
            select(BackgroundTaskHistory.id)
            .where(
                BackgroundTaskHistory.queue_name == ML_QUEUE,
                BackgroundTaskHistory.task_type == "ml_drift_check",
                BackgroundTaskHistory.created_at >= cutoff,
            )
            .limit(1)
        )).scalar_one_or_none()
        if recent is not None:
            return
        try:
            await enqueue_ml_job(
                db, kind="drift", payload={"source": "schedule"},
                description="Scheduled report-only ML drift check",
            )
            await db.commit()
            logger.info("[ML_WORKER] scheduled durable drift check")
        except MLJobConflict:
            await db.rollback()


async def run_worker(*, once: bool = False) -> None:
    await _ensure_db()
    poll_seconds = _seconds("ML_JOB_POLL_SECONDS", 2, 0.2)
    lease_seconds = int(_seconds("ML_JOB_LEASE_SECONDS", 60, 20))
    heartbeat_seconds = min(
        _seconds("ML_JOB_HEARTBEAT_SECONDS", 10, 1),
        max(1.0, lease_seconds / 3.0),
    )
    maintenance_seconds = _seconds("ML_JOB_MAINTENANCE_SECONDS", 30, 5)
    last_heartbeat = 0.0
    last_maintenance = 0.0
    logger.info("[ML_WORKER] ready worker_id=%s", WORKER_ID)
    await _heartbeat("idle", register=True)
    last_heartbeat = monotonic()
    while True:
        now = monotonic()
        if now - last_heartbeat >= heartbeat_seconds:
            await _heartbeat("idle")
            last_heartbeat = monotonic()
        if now - last_maintenance >= maintenance_seconds:
            await task_history_manager.fail_expired_queue_leases(queue_name=ML_QUEUE)
            await _enqueue_scheduled_drift_if_due()
            last_maintenance = monotonic()
        job = await task_history_manager.claim_next_queued_job(
            queue_name=ML_QUEUE, lease_owner=WORKER_ID, lease_seconds=lease_seconds
        )
        if job:
            await _supervise(job)
            last_heartbeat = monotonic()
        elif once:
            return
        else:
            await asyncio.sleep(poll_seconds)


def _parse_datetime(value: Optional[str]):
    if not value:
        return None
    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).replace(tzinfo=None)


async def execute_job(job_id: str) -> int:
    """Child-process dispatcher. Only leased/running ML jobs are executable."""
    await _ensure_db()
    job = await task_history_manager.get_queued_job_payload(job_id, queue_name=ML_QUEUE)
    if not job or job.get("status") != "running":
        logger.error("[ML_WORKER] refusing unleased job_id=%s", job_id)
        return 2
    payload = job.get("payload") or {}
    task_type = job.get("task_type")
    try:
        if task_type == "ml_training":
            from backend.ml import trainer
            busy = trainer.try_acquire_training(job_id)
            if busy is not None:
                raise RuntimeError(f"local training guard held by {busy}")
            await trainer.run_training_job(
                job_id,
                model_type=payload["model_type"],
                algorithm=payload.get("algorithm", "isolation_forest"),
                requested_by=payload.get("requested_by"),
                dataset_id=payload.get("dataset_id"),
                seed=payload.get("seed"),
                hyperparameters=payload.get("hyperparameters"),
                sampling_policy=payload.get("sampling_policy"),
            )
            final = await task_history_manager.get_task_by_job_id(job_id)
            return 0 if final and final.get("status") == "completed" else 2

        if task_type == "ml_feature_computation":
            from backend.ml.collector import run_collection
            async with db_manager.get_session() as db:
                async def progress(percent):
                    await task_history_manager.update_progress(
                        job_id, percent, details={"stage": "collecting_features"}
                    )
                result = await run_collection(
                    db, run_id=job_id,
                    full_rebuild=bool(payload.get("full_rebuild")),
                    progress_cb=progress,
                )
            await task_history_manager.finish_job(job_id, success=True, result=result)
            return 0

        if task_type == "ml_dataset_build":
            from backend.ml.dataset_builder import build_dataset
            from backend.ml.dataset_definitions import get_definition
            await task_history_manager.update_progress(
                job_id, 10, details={"stage": "extracting_dataset"}
            )
            definition = None
            if payload.get("definition"):
                definition = get_definition(
                    payload["definition"], payload.get("definition_version")
                )
            async with db_manager.get_session() as db:
                result = await build_dataset(
                    db,
                    name=payload["name"], kind=payload["kind"],
                    created_by=payload.get("created_by"), build_job_id=job_id,
                    definition=definition,
                    time_range_start=_parse_datetime(payload.get("time_range_start")),
                    time_range_end=_parse_datetime(payload.get("time_range_end")),
                    sampling_policy=payload.get("sampling_policy"),
                    split_strategy=payload.get("split_strategy"),
                )
            if result.get("status") == "failed":
                await task_history_manager.finish_job(
                    job_id, success=False, result=result,
                    error_code="DATASET_QUALITY_FAILED",
                    error_message="dataset extraction or quality gates failed",
                )
                return 2
            await task_history_manager.finish_job(job_id, success=True, result=result)
            return 0

        if task_type == "ml_dataset_hash_backfill":
            from backend.ml.dataset_builder import backfill_dataset_file_hashes
            await task_history_manager.update_progress(
                job_id, 10, details={"stage": "verifying_dataset_hashes"}
            )
            async with db_manager.get_session() as db:
                result = await backfill_dataset_file_hashes(db, job_id=job_id)
            await task_history_manager.finish_job(job_id, success=True, result=result)
            return 0

        if task_type == "ml_drift_check":
            from backend.ml.drift_service import drift_service
            await task_history_manager.update_progress(
                job_id, 10, details={"stage": "computing_drift"}
            )
            async with db_manager.get_session() as db:
                result = await drift_service.run_all(db, job_id=job_id)
            await task_history_manager.finish_job(job_id, success=True, result=result)
            return 0

        raise RuntimeError(f"unsupported ML task type {task_type!r}")
    except Exception as exc:
        logger.error("[ML_WORKER] job failed job_id=%s: %s", job_id, exc, exc_info=True)
        await task_history_manager.finish_job(
            job_id, success=False, error_code="ML_JOB_FAILED",
            error_message=str(exc)[:500],
        )
        return 2


def main() -> int:
    logging.basicConfig(
        level=getattr(logging, str(getattr(settings, "LOG_LEVEL", "INFO")).upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute-job")
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    if args.execute_job:
        return asyncio.run(execute_job(args.execute_job))
    asyncio.run(run_worker(once=args.once))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

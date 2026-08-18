"""
Incremental, checkpointed feature collection.

Event time vs processing time: the watermark advances on ``created_at``
(processing time — catches late-INSERTED rows), while every snapshot is
anchored on ``start_time`` (event time) with as_of = the event's own
timestamp, i.e. the state of the person JUST BEFORE that appearance.
Each run re-scans a late-grace window behind the watermark; snapshot
uniqueness makes reprocessing idempotent (ON CONFLICT DO NOTHING).

The job wrapper follows the retention-job template: task_history row +
staged progress + cancellation + in-process guard + DistributedLock.
"""

import asyncio
import logging
import uuid as uuid_mod
from datetime import datetime, timedelta
from typing import Any, Dict, Optional

from sqlalchemy import select, and_
from sqlalchemy.ext.asyncio import AsyncSession

from backend.ml.feature_store import feature_store
from config import settings

logger = logging.getLogger(__name__)

COLLECTOR_NAME = "person_appearance_collector"
_collection_lock = asyncio.Lock()


async def _get_or_create_checkpoint(db: AsyncSession):
    from db_models import MLCollectionCheckpoint
    row = (await db.execute(
        select(MLCollectionCheckpoint)
        .where(MLCollectionCheckpoint.collector_name == COLLECTOR_NAME)
    )).scalar_one_or_none()
    if row is None:
        row = MLCollectionCheckpoint(
            collector_name=COLLECTOR_NAME,
            late_grace_minutes=int(settings.ML_COLLECTOR_LATE_GRACE_MINUTES),
        )
        db.add(row)
        await db.flush()
    return row


async def run_collection(db: AsyncSession, *, run_id: Optional[str] = None,
                         full_rebuild: bool = False,
                         progress_cb=None, cancel_check=None) -> Dict[str, Any]:
    """One incremental collection pass. Returns honest stats."""
    run_id = run_id or f"mlcollect-{uuid_mod.uuid4().hex[:8]}"
    from db_models import IdentityAppearance

    checkpoint = await _get_or_create_checkpoint(db)
    grace = timedelta(minutes=int(checkpoint.late_grace_minutes or 120))
    if full_rebuild or checkpoint.watermark_event_time is None:
        window_start = None
    else:
        window_start = checkpoint.watermark_event_time - grace

    query = select(IdentityAppearance.id, IdentityAppearance.identity_id,
                   IdentityAppearance.start_time, IdentityAppearance.created_at)
    if window_start is not None:
        query = query.where(IdentityAppearance.created_at > window_start)
    query = query.order_by(IdentityAppearance.created_at, IdentityAppearance.id).limit(20000)
    rows = list((await db.execute(query)).all())

    snapshots_written = 0
    snapshots_deduplicated = 0
    affected_identities = set()
    for index, (row_id, identity_id, start_time, created_at) in enumerate(rows):
        if cancel_check and cancel_check():
            logger.info("[ML_OPS] collection cancelled run_id=%s at row %d", run_id, index)
            break
        result = await feature_store.compute_person_snapshot(
            db, str(identity_id), start_time, run_id=run_id, event_ts=start_time)
        snapshots_deduplicated += int(result["deduplicated"])
        snapshots_written += int(not result["deduplicated"])
        affected_identities.add(str(identity_id))
        if progress_cb and index % 50 == 0 and rows:
            await progress_cb(int(80 * index / len(rows)))

    # One "current state" snapshot per affected identity (minute-rounded so
    # repeated runs dedup) — this is what drift and inspection read.
    now_as_of = datetime.utcnow().replace(second=0, microsecond=0)
    for identity_id in affected_identities:
        result = await feature_store.compute_person_snapshot(
            db, identity_id, now_as_of, run_id=run_id)
        snapshots_deduplicated += int(result["deduplicated"])
        snapshots_written += int(not result["deduplicated"])

    if rows:
        last = rows[-1]
        checkpoint.watermark_event_time = last[3]  # created_at (processing time)
        checkpoint.watermark_id = last[0]
    checkpoint.last_run_id = run_id
    checkpoint.last_run_at = datetime.utcnow()
    checkpoint.rows_processed_total = (checkpoint.rows_processed_total or 0) + len(rows)
    checkpoint.extras = {
        "last_window_start": window_start.isoformat() + "Z" if window_start else None,
        "last_rows": len(rows),
    }
    await db.commit()

    stats = {
        "run_id": run_id,
        "rows_scanned": len(rows),
        "identities_affected": len(affected_identities),
        "snapshots_written": snapshots_written,
        "snapshots_deduplicated": snapshots_deduplicated,
        "watermark_event_time": (checkpoint.watermark_event_time.isoformat() + "Z"
                                 if checkpoint.watermark_event_time else None),
        "full_rebuild": bool(full_rebuild),
    }
    logger.info("[ML_OPS] collection complete %s", stats)
    return stats


async def launch_collection_job(*, created_by_user_id: Optional[int] = None,
                                full_rebuild: bool = False) -> Dict[str, Any]:
    """202-style launcher (retention-job template): task_history row +
    background task + in-process guard + cross-worker DistributedLock."""
    from backend.core.task_history import task_history_manager
    from backend.core.distributed_lock import DistributedLock

    if _collection_lock.locked():
        return {"status": "busy", "reason": "collection_already_running"}

    job_id = f"mlcollect-{uuid_mod.uuid4().hex[:8]}"
    dlock = DistributedLock("ml-feature-job", ttl_seconds=1800)
    if not await dlock.acquire(holder_label=job_id):
        return {"status": "busy", "reason": "collection_running_on_another_worker",
                "holder": dlock.holder_hint}

    task_id = await task_history_manager.create_job(
        job_id=job_id, task_type="ml_feature_computation",
        task_name="ML Feature Collection",
        description="Point-in-time feature snapshots from operational data",
        created_by_user_id=created_by_user_id)

    async def _run():
        from db_connection import db_manager
        async with _collection_lock:
            await task_history_manager.mark_running(job_id)
            try:
                async with db_manager.get_session() as db:
                    async def _progress(percent):
                        await task_history_manager.update_progress(job_id, percent)

                    stats = await run_collection(
                        db, run_id=job_id, full_rebuild=full_rebuild,
                        progress_cb=_progress,
                        cancel_check=lambda: task_history_manager.is_cancel_requested(job_id))
                await task_history_manager.finish_job(job_id, success=True, result=stats)
            except Exception as e:
                logger.error("[ML_OPS] collection job failed job_id=%s: %s",
                             job_id, e, exc_info=True)
                await task_history_manager.finish_job(
                    job_id, success=False, error_code="ML_COLLECTION_FAILED",
                    error_message=str(e)[:500])
            finally:
                await dlock.release()

    asyncio.create_task(_run())
    return {"status": "scheduled", "job_id": job_id, "task_id": task_id,
            "task_type": "ml_feature_computation"}

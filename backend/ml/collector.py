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

# Rows fetched per keyset batch. A run keeps fetching batches until the
# candidate window is exhausted (or cancelled) — a backlog larger than one
# batch is never silently left behind.
BATCH_ROWS = 20000

from sqlalchemy import select, func, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from backend.ml.feature_store import feature_store
from config import settings

logger = logging.getLogger(__name__)

COLLECTOR_NAME = "person_appearance_collector"
_collection_lock = asyncio.Lock()


# Identities per phase-2 chunk (one transaction each; cancel + progress +
# lock renewal between chunks).
CURRENT_STATE_CHUNK = 500


async def _get_or_create_checkpoint(db: AsyncSession):
    """The checkpoint row, LOCKED for this transaction (SELECT ... FOR UPDATE)
    so two collectors can never lose-update the watermark; creation is
    ON CONFLICT DO NOTHING so two first-ever runs do not collide."""
    from sqlalchemy.dialects.postgresql import insert as pg_insert
    from db_models import MLCollectionCheckpoint
    await db.execute(
        pg_insert(MLCollectionCheckpoint)
        .values(collector_name=COLLECTOR_NAME,
                late_grace_minutes=int(settings.ML_COLLECTOR_LATE_GRACE_MINUTES),
                rows_processed_total=0)
        .on_conflict_do_nothing(index_elements=["collector_name"]))
    return await _lock_checkpoint(db)


async def _lock_checkpoint(db: AsyncSession):
    from db_models import MLCollectionCheckpoint
    return (await db.execute(
        select(MLCollectionCheckpoint)
        .where(MLCollectionCheckpoint.collector_name == COLLECTOR_NAME)
        .with_for_update())).scalar_one()


async def _identities_missing_current_state(db: AsyncSession, phase2: Dict[str, Any]) -> set:
    """Reconciliation after a crash/cancel inside phase 2 of an earlier run:
    every identity that had an appearance in that run's window but got no
    current-state snapshot since that run started. Pure SQL set difference -
    nothing is guessed from counts."""
    from sqlalchemy import text as sa_text
    started = phase2.get("started_at")
    if not started:
        return set()
    started_dt = datetime.fromisoformat(str(started).rstrip("Z"))
    window_start = phase2.get("window_start")
    window_dt = datetime.fromisoformat(str(window_start).rstrip("Z")) if window_start else None
    sql = (
        "SELECT DISTINCT a.identity_id::text FROM identity_appearances a "
        "WHERE (CAST(:ws AS timestamp) IS NULL OR a.created_at > CAST(:ws AS timestamp)) "
        "AND NOT EXISTS (SELECT 1 FROM ml_feature_snapshots s "
        "                WHERE s.entity_type = 'person' AND s.entity_id = a.identity_id::text "
        "                AND s.event_timestamp IS NULL AND s.as_of_timestamp >= :started)")
    rows = (await db.execute(sa_text(sql), {"ws": window_dt, "started": started_dt})).all()
    return {str(r[0]) for r in rows}


async def run_collection(db: AsyncSession, *, run_id: Optional[str] = None,
                         full_rebuild: bool = False,
                         progress_cb=None, cancel_check=None, renew_cb=None) -> Dict[str, Any]:
    """One incremental collection pass. Returns honest stats.

    Durability contract: phase 1 (event snapshots) commits per keyset batch
    and advances the watermark under a row lock; phase 2 (one current-state
    snapshot per affected identity) commits per chunk and records its
    progress in checkpoint.extras["phase2"], so a crash or cancellation in
    phase 2 is RECONCILED by the next run (identities still lacking a
    current-state snapshot are recomputed) instead of being lost. `renew_cb`
    (optional, awaited between batches/chunks) lets the job wrapper extend
    its distributed lock."""
    run_id = run_id or f"mlcollect-{uuid_mod.uuid4().hex[:8]}"
    from db_models import IdentityAppearance

    checkpoint = await _get_or_create_checkpoint(db)
    grace = timedelta(minutes=int(checkpoint.late_grace_minutes or 120))
    if full_rebuild or checkpoint.watermark_event_time is None:
        window_start = None
    else:
        window_start = checkpoint.watermark_event_time - grace
    previous_extras = dict(checkpoint.extras or {})
    unfinished_phase2 = previous_extras.get("phase2") or {}
    reconcile = unfinished_phase2.get("status") == "in_progress"
    await db.commit()   # release the row lock; batches re-lock it to write

    columns = select(IdentityAppearance.id, IdentityAppearance.identity_id,
                     IdentityAppearance.start_time, IdentityAppearance.created_at)
    base = columns
    if window_start is not None:
        base = base.where(IdentityAppearance.created_at > window_start)
    # Honest progress needs the candidate count up front (a backlog can be
    # far larger than one batch).
    total_candidates = int((await db.execute(
        select(func.count()).select_from(base.subquery()))).scalar() or 0)

    snapshots_written = 0
    snapshots_deduplicated = 0
    affected_identities = set()
    rows_scanned = 0
    batches = 0
    cancelled = False
    cursor = None  # keyset (created_at, id) of the last processed row
    while not cancelled:
        query = base
        if cursor is not None:
            # Row-value comparison: PostgreSQL turns (created_at, id) > (c, i)
            # into a single ordered range on idx_appearance_created_at_id (the
            # OR/AND form is not recognised and re-sorts every batch). The
            # window predicate is implied once a cursor exists (the cursor came
            # from a row inside the window), and carrying both conditions makes
            # the planner fall back to a bitmap scan + sort - so it is dropped.
            query = columns.where(
                tuple_(IdentityAppearance.created_at, IdentityAppearance.id) > tuple_(cursor[0], cursor[1]))
        query = query.order_by(IdentityAppearance.created_at, IdentityAppearance.id).limit(BATCH_ROWS)
        rows = list((await db.execute(query)).all())
        if not rows:
            break
        batches += 1
        processed_in_batch = 0
        for row_id, identity_id, start_time, created_at in rows:
            if cancel_check and cancel_check():
                logger.info("[ML_OPS] collection cancelled run_id=%s at row %d", run_id, rows_scanned)
                cancelled = True
                break
            result = await feature_store.compute_person_snapshot(
                db, str(identity_id), start_time, run_id=run_id, event_ts=start_time)
            snapshots_deduplicated += int(result["deduplicated"])
            snapshots_written += int(not result["deduplicated"])
            affected_identities.add(str(identity_id))
            rows_scanned += 1
            processed_in_batch += 1
            cursor = (created_at, row_id)
            if progress_cb and rows_scanned % 50 == 0 and total_candidates:
                await progress_cb(int(80 * rows_scanned / total_candidates))
        # The watermark advances per batch and is committed, so a crash or a
        # cancellation keeps what was processed; the late-grace re-scan plus
        # snapshot uniqueness make any reprocessing idempotent.
        if processed_in_batch:
            checkpoint = await _lock_checkpoint(db)
            # monotonic: a concurrent (stale) writer can never move it back
            current = (checkpoint.watermark_event_time, checkpoint.watermark_id or 0)
            if checkpoint.watermark_event_time is None or cursor > current:
                checkpoint.watermark_event_time = cursor[0]  # created_at (processing time)
                checkpoint.watermark_id = cursor[1]
            checkpoint.rows_processed_total = (checkpoint.rows_processed_total or 0) + processed_in_batch
            await db.commit()
        if renew_cb:
            await renew_cb()
        if len(rows) < BATCH_ROWS:
            break

    # One "current state" snapshot per affected identity (minute-rounded so
    # repeated runs dedup) - this is what drift and inspection read.
    reconciled = 0
    if reconcile and not cancelled:
        missing = await _identities_missing_current_state(db, unfinished_phase2)
        reconciled = len(missing - affected_identities)
        affected_identities |= missing
        logger.warning("[ML_OPS] reconciling %d identit(ies) left without a current-state "
                       "snapshot by run %s", reconciled, unfinished_phase2.get("run_id"))
    # started_at IS the minute-floored as_of every current-state snapshot of
    # this phase carries, so "has a snapshot since started_at" is exact.
    now_as_of = datetime.utcnow().replace(second=0, microsecond=0)
    pending = sorted(affected_identities)
    phase2_state = {"status": "in_progress", "run_id": run_id,
                    "started_at": now_as_of.isoformat() + "Z",
                    "window_start": (window_start.isoformat() + "Z" if window_start else None),
                    "pending": len(pending), "total": len(pending)}
    checkpoint = await _lock_checkpoint(db)
    checkpoint.extras = {**previous_extras, "phase2": phase2_state}
    await db.commit()

    done = 0
    phase2_cancelled = False
    while pending and not cancelled:
        if cancel_check and cancel_check():
            logger.info("[ML_OPS] collection cancelled run_id=%s in phase 2 (%d identities pending)",
                        run_id, len(pending))
            cancelled = phase2_cancelled = True
            break
        chunk, pending = pending[:CURRENT_STATE_CHUNK], pending[CURRENT_STATE_CHUNK:]
        for identity_id in chunk:
            result = await feature_store.compute_person_snapshot(
                db, identity_id, now_as_of, run_id=run_id)
            snapshots_deduplicated += int(result["deduplicated"])
            snapshots_written += int(not result["deduplicated"])
        done += len(chunk)
        checkpoint = await _lock_checkpoint(db)
        phase2_state = {**phase2_state, "pending": len(pending)}
        checkpoint.extras = {**previous_extras, "phase2": phase2_state}
        await db.commit()   # one transaction per chunk: a crash keeps the rest
        if progress_cb and len(affected_identities):
            await progress_cb(80 + int(20 * done / len(affected_identities)))
        if renew_cb:
            await renew_cb()

    checkpoint = await _lock_checkpoint(db)
    checkpoint.last_run_id = run_id
    checkpoint.last_run_at = datetime.utcnow()
    phase2_state = {**phase2_state, "pending": len(pending),
                    "status": ("in_progress" if (pending or phase2_cancelled) else "complete")}
    checkpoint.extras = {
        "last_window_start": window_start.isoformat() + "Z" if window_start else None,
        "last_rows": rows_scanned,
        "last_candidates": total_candidates,
        "last_batches": batches,
        "last_cancelled": cancelled,
        "last_reconciled_identities": reconciled,
        "phase2": phase2_state,
    }
    await db.commit()
    try:
        from backend.ml import metrics as ml_metrics
        age = ((datetime.utcnow() - checkpoint.watermark_event_time).total_seconds()
               if checkpoint.watermark_event_time else None)
        ml_metrics.observe_collector(rows_scanned, age)
    except Exception:
        pass

    stats = {
        "run_id": run_id,
        "rows_scanned": rows_scanned,
        "candidate_rows": total_candidates,
        "batches": batches,
        "batch_rows": BATCH_ROWS,
        "cancelled": cancelled,
        "identities_affected": len(affected_identities),
        "current_state_pending": len(pending),
        "reconciled_identities": reconciled,
        "snapshots_written": snapshots_written,
        "snapshots_deduplicated": snapshots_deduplicated,
        "watermark_event_time": (checkpoint.watermark_event_time.isoformat() + "Z"
                                 if checkpoint.watermark_event_time else None),
        "full_rebuild": bool(full_rebuild),
    }
    logger.info("[ML_OPS] collection complete %s", stats)
    return stats


async def launch_collection_job(*, created_by_user_id: Optional[int] = None,
                                full_rebuild: bool = False,
                                request_id: Optional[str] = None) -> Dict[str, Any]:
    """202-style launcher (retention-job template): task_history row +
    background task + in-process guard + cross-worker DistributedLock."""
    from backend.core.task_history import task_history_manager
    from backend.core.distributed_lock import DistributedLock

    # In-process guard taken HERE (no suspension between the check and the
    # acquire), not inside the task: two launches in the same tick could
    # otherwise both pass the check before either task had started.
    if _collection_lock.locked():
        return {"status": "busy", "reason": "collection_already_running"}
    await _collection_lock.acquire()

    job_id = f"mlcollect-{uuid_mod.uuid4().hex[:8]}"
    dlock = DistributedLock("ml-feature-job", ttl_seconds=1800)
    if not await dlock.acquire(holder_label=job_id):
        _collection_lock.release()
        return {"status": "busy", "reason": "collection_running_on_another_worker",
                "holder": dlock.holder_hint}

    task_id = await task_history_manager.create_job(
        job_id=job_id, task_type="ml_feature_computation",
        task_name="ML Feature Collection",
        description="Point-in-time feature snapshots from operational data",
        created_by_user_id=created_by_user_id, request_id=request_id)

    async def _run():
        from db_connection import db_manager
        try:
            await task_history_manager.mark_running(job_id)
            try:
                async with db_manager.get_session() as db:
                    async def _progress(percent):
                        await task_history_manager.update_progress(job_id, percent)

                    async def _renew():
                        if not await dlock.renew():
                            logger.warning("[ML_OPS] collection lock lost job_id=%s (TTL expired); "
                                           "continuing - checkpoint row locks keep writes safe", job_id)

                    stats = await run_collection(
                        db, run_id=job_id, full_rebuild=full_rebuild,
                        progress_cb=_progress,
                        cancel_check=lambda: task_history_manager.is_cancel_requested(job_id),
                        renew_cb=_renew)
                if stats.get("cancelled"):
                    await task_history_manager.finish_job(
                        job_id, success=False, error_code="CANCELLED",
                        error_message="collection cancelled", cancelled=True, result=stats)
                else:
                    await task_history_manager.finish_job(job_id, success=True, result=stats)
            except Exception as e:
                logger.error("[ML_OPS] collection job failed job_id=%s: %s",
                             job_id, e, exc_info=True)
                await task_history_manager.finish_job(
                    job_id, success=False, error_code="ML_COLLECTION_FAILED",
                    error_message=str(e)[:500])
            finally:
                await dlock.release()
        finally:
            _collection_lock.release()

    asyncio.create_task(_run())
    return {"status": "scheduled", "job_id": job_id, "task_id": task_id,
            "task_type": "ml_feature_computation"}

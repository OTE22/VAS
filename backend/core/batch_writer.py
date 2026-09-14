"""
Batch Database Writer
=====================
Batches processed frames and persists each one through the ONE detection write
path (`backend/core/detection_evidence.persist_detection`):

    TX 1 (bulk)      ensure every pipeline of the batch exists
    per detection    ONE transaction: detection + faces + appearance + exact
                     embedding→detection link + counter (CORE, all-or-nothing)
                     + live-alert / watchlist alerts (OPTIONAL, independent
                     savepoints) → commit → broadcast `detection_alerts`

A failing detection is compensated (the embeddings that frame created are
removed), logged and counted; it never stops the rest of the batch and never
leaves a committed detection without its required evidence. The direct-write
path in image_processing uses the same function, so there is nothing to drift.
"""

import os
import sys
import asyncio
import logging
import time
from typing import Optional
from datetime import datetime

# Add parent directory to path
parent_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if parent_dir not in sys.path:
    sys.path.insert(0, parent_dir)

from config import settings
from backend.config import BATCH_WRITE_SIZE
from backend.core.circuit_breaker import db_circuit_breaker
from backend.core.metrics import metrics_db_operations
from db_connection import db_manager
from db_models import Pipeline
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError, DataError
from backend.core.detection_evidence import (
    persist_detection, broadcast_detection_alerts, compensate_failed_detection,
    EmbeddingLinkError,
)

logger = logging.getLogger(__name__)


class BatchDatabaseWriter:
    """
    Batch database writer with:
    - Short-lived transactions
    - No long locks
    - Safe under high concurrency
    """

    def __init__(self, batch_size: int = BATCH_WRITE_SIZE, flush_interval: float = None):
        self.batch_size = batch_size
        # Use optimized flush interval from settings if available
        if flush_interval is None:
            flush_interval = settings.BATCH_WRITE_INTERVAL
        self.flush_interval = flush_interval

        self._lock = asyncio.Lock()
        self._flush_task: Optional[asyncio.Task] = None
        self._queued_since_flush = 0

    @property
    def pending_detections(self):
        """Compatibility for the shutdown pending-count check; disk owns the queue."""
        from backend.core.detection_spool import root
        return [p for p in root().glob('*.json') if not p.name.endswith('.failed.json')]

    async def start(self):
        if self._flush_task and not self._flush_task.done():
            logger.warning("Batch DB writer already running; ignoring duplicate start()")
            return
        from backend.core.service_supervisor import supervised_loop
        # jitter=0: this is a sub-second hot loop; jittering a 2s flush
        # cadence buys nothing and complicates timing-sensitive tests.
        self._flush_task = asyncio.create_task(
            supervised_loop(
                "batch_writer",
                self.flush_interval,
                self.flush,
                error_backoff_base=self.flush_interval,
                jitter=0,
            ),
            name="batch_writer",
        )
        logger.info(
            f"Batch DB writer started "
            f"(batch_size={self.batch_size}, flush_interval={self.flush_interval}s)"
        )

    async def stop(self):
        if self._flush_task:
            self._flush_task.cancel()
            try:
                await self._flush_task
            except asyncio.CancelledError:
                pass
        await self.flush()
        logger.info("Batch DB writer stopped")

    async def add_detection(self, detection_data: dict):
        import uuid
        from backend.core import detection_spool
        detection_data["detection"].setdefault("uuid", str(uuid.uuid4()))
        # Once accepted, failure belongs to the retry queue, not the caller's
        # direct-write fallback (which would compensate pending embeddings).
        await asyncio.to_thread(detection_spool.enqueue, detection_data)
        self._queued_since_flush += 1
        # Preserve size-triggered flushing as well as the timer; high camera
        # traffic must not be capped at one batch per timer interval.
        if self._queued_since_flush >= self.batch_size and not self._lock.locked():
            try:
                await self.flush()
            except Exception:
                logger.exception("[BATCH] Immediate flush failed; accepted frames remain queued")

    async def flush(self):
        async with self._lock:
            self._queued_since_flush = 0
            await self._flush_internal()
            from backend.core.detection_spool import stats
            return await asyncio.to_thread(stats)

    async def _flush_internal(self):
        from backend.core import detection_spool
        batch = await asyncio.to_thread(detection_spool.pending, self.batch_size)
        if not batch:
            return
        if not await db_circuit_breaker.can_execute():
            raise RuntimeError("Database circuit open; detections retained on disk")
        failures = []
        for path, d in batch:
            started = time.time()
            try:
                async with asyncio.timeout(150):
                    async with db_manager.get_session() as db:
                        now = datetime.utcnow()
                        await db.execute(pg_insert(Pipeline).values(
                            pipeline_id=d["pipeline_id"], total_detections=0,
                            is_active=1, created_at=now, updated_at=now
                        ).on_conflict_do_nothing(index_elements=["pipeline_id"]))
                        outcome = await persist_detection(db, detection_data=d)
                    # Commit is complete. Replays after an ambiguous commit
                    # return the existing detection UUID without side effects.
                    from backend.core.appearance_events import publish_unknown_events
                    await publish_unknown_events(getattr(outcome, 'unknown_events', []))
                    await asyncio.to_thread(detection_spool.acknowledge, path)
                    if outcome.bundles:
                        await broadcast_detection_alerts(outcome.bundles,
                                                         location_name=d.get("location_name"))
                await db_circuit_breaker.call_succeeded()
            except asyncio.CancelledError:
                raise  # Disk entry survives; committed UUIDs safely deduplicate.
            except (EmbeddingLinkError, IntegrityError, DataError) as exc:
                await compensate_failed_detection(d)
                await asyncio.to_thread(detection_spool.quarantine, path, exc)
                failures.append(str(exc))
                logger.exception("[BATCH] Invalid evidence quarantined: %s", path.name)
            except Exception as exc:
                await db_circuit_breaker.call_failed()
                failures.append(str(exc))
                logger.exception("[BATCH] Write failed; frame retained for retry: %s", path.name)
                # Let other entries run first on the next cycle.
                if path.exists():
                    await asyncio.to_thread(os.utime, path, None)
            finally:
                metrics_db_operations.observe(time.time() - started)
        if failures:
            from backend.core.metrics import metrics_db_operation_failures
            if metrics_db_operation_failures:
                metrics_db_operation_failures.labels(reason="detection_core").inc(len(failures))
            raise RuntimeError(f"{len(failures)} detection write(s) failed; see retry queue/logs")

batch_writer = BatchDatabaseWriter()

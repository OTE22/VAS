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

        self.pending_detections: list[dict] = []
        self._lock = asyncio.Lock()
        self._flush_task: Optional[asyncio.Task] = None

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
        async with self._lock:
            self.pending_detections.append(detection_data)

            if len(self.pending_detections) >= self.batch_size:
                await self._flush_internal()

    async def flush(self):
        async with self._lock:
            await self._flush_internal()

    async def _flush_internal(self):
        if not self.pending_detections:
            return

        if not await db_circuit_breaker.can_execute():
            logger.warning("DB circuit open — dropping batch")
            self.pending_detections.clear()
            return

        batch = self.pending_detections
        self.pending_detections = []

        start_time = time.time()

        # Add timeout to prevent hanging connections
        DB_OPERATION_TIMEOUT = 150.0  # seconds

        try:
            async with asyncio.timeout(DB_OPERATION_TIMEOUT):
                # =====================================================
                # TX 1 — ensure pipelines exist (SHORT TX)
                # =====================================================
                # Race-safe UPSERT: concurrent workers first-sighting the same new
                # pipeline name must never collide on the unique index (the old
                # SELECT-then-INSERT dropped the whole batch on UniqueViolation).
                pipeline_ids = list({d["pipeline_id"] for d in batch})
                async with db_manager.get_session() as db:
                    now = datetime.utcnow()
                    stmt = pg_insert(Pipeline).values([
                        {
                            "pipeline_id": pid,
                            "total_detections": 0,
                            "is_active": 1,
                            "created_at": now,
                            "updated_at": now,
                        }
                        for pid in pipeline_ids
                    ]).on_conflict_do_nothing(index_elements=["pipeline_id"])
                    await db.execute(stmt)

                # =====================================================
                # per detection — ONE transaction each (core atomic), then
                # broadcast only what was committed
                # =====================================================
                persisted = 0
                for d in batch:
                    outcome = None
                    try:
                        async with db_manager.get_session() as db:
                            outcome = await persist_detection(db, detection_data=d)
                        # session exit committed the transaction
                    except EmbeddingLinkError as link_err:
                        from backend.core.metrics import metrics_db_operation_failures
                        if metrics_db_operation_failures:
                            metrics_db_operation_failures.labels(reason="detection_core").inc()
                        logger.error("[BATCH] detection NOT persisted (%s) pipeline=%s — embedding "
                                     "provenance inconsistent: %s", link_err.outcome.value,
                                     d.get("pipeline_id"), link_err)
                        await compensate_failed_detection(d)
                        continue
                    except Exception:
                        from backend.core.metrics import metrics_db_operation_failures
                        if metrics_db_operation_failures:
                            metrics_db_operation_failures.labels(reason="detection_core").inc()
                        logger.exception("[BATCH] detection NOT persisted (core failure) pipeline=%s",
                                         d.get("pipeline_id"))
                        await compensate_failed_detection(d)
                        continue
                    persisted += 1
                    if outcome.bundles:
                        await broadcast_detection_alerts(outcome.bundles,
                                                         location_name=d.get("location_name"))

            await db_circuit_breaker.call_succeeded()
            metrics_db_operations.observe(time.time() - start_time)

            logger.info(
                f"✅ flushed {persisted}/{len(batch)} detections "
                f"in {time.time() - start_time:.3f}s"
            )

        except asyncio.CancelledError:
            # Shutdown in progress, re-raise to allow proper cleanup
            logger.info("⚠️  Batch write cancelled (shutdown in progress)")
            self.pending_detections = batch + self.pending_detections  # Re-add to pending
            raise
        except asyncio.TimeoutError:
            await db_circuit_breaker.call_failed()
            # The histogram used to observe ONLY successes, so a database
            # slowdown made writes disappear from the latency data at exactly
            # the moment they mattered. Record the duration and the failure.
            metrics_db_operations.observe(time.time() - start_time)
            from backend.core.metrics import metrics_db_operation_failures
            if metrics_db_operation_failures:
                metrics_db_operation_failures.labels(reason="timeout").inc()
            logger.error(f"❌ Batch write timeout after {DB_OPERATION_TIMEOUT}s - database may be overloaded")
            # Don't re-add to pending as this might cause infinite retries
        except Exception as e:
            await db_circuit_breaker.call_failed()
            metrics_db_operations.observe(time.time() - start_time)
            from backend.core.metrics import metrics_db_operation_failures
            if metrics_db_operation_failures:
                metrics_db_operation_failures.labels(reason="error").inc()
            logger.exception("❌ Batch write failed")

batch_writer = BatchDatabaseWriter()


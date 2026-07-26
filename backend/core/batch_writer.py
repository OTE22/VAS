"""
Batch Database Writer
=====================
Batch database writer with short-lived transactions and no long locks.
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
from db_models import Pipeline, Detection, Face
from sqlalchemy import select, insert, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

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
            flush_interval = getattr(settings, 'BATCH_WRITE_INTERVAL', 2.0)
        self.flush_interval = flush_interval

        self.pending_detections: list[dict] = []
        self._lock = asyncio.Lock()
        self._flush_task: Optional[asyncio.Task] = None

    async def start(self):
        self._flush_task = asyncio.create_task(self._periodic_flush())
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
                # TX 2 — bulk insert detections + faces (SHORT TX)
                # =====================================================
                async with db_manager.get_session() as db:
                    detection_rows = []
                    face_rows = []

                    for d in batch:
                        detection_rows.append(d["detection"])

                    result = await db.execute(
                        insert(Detection).returning(Detection.id),
                        detection_rows
                    )
                    detection_ids = [r[0] for r in result.fetchall()]

                    for det_id, d in zip(detection_ids, batch):
                        for face in d["faces"]:
                            face_copy = face.copy()
                            face_copy["detection_id"] = det_id
                            # Remove internal fields that shouldn't be in DB
                            face_copy.pop("_identity", None)
                            face_copy.pop("_embedding", None)
                            # Keep identity_id and label_state if present
                            face_rows.append(face_copy)

                    if face_rows:
                        await db.execute(insert(Face), face_rows)
                    
                    # Store detection_id mapping for identity appearances
                    detection_id_map = {i: det_id for i, det_id in enumerate(detection_ids)}

                # =====================================================
                # TX 3 — create identity appearances (if identity system active)
                # =====================================================
                try:
                    from backend.core.identity_service import identity_service
                    from db_models import Identity, IdentityEmbedding
                    from sqlalchemy import update as sql_update, select as sql_select
                    
                    if identity_service:
                        async with db_manager.get_session() as db:
                            for batch_idx, d in enumerate(batch):
                                detection_id = detection_id_map.get(batch_idx)
                                if not detection_id:
                                    continue
                                
                                detection_timestamp = d["detection"].get("timestamp", datetime.utcnow())
                                
                                for face in d["faces"]:
                                    identity_id = face.get("identity_id")
                                    embedding = face.get("_embedding")
                                    
                                    if identity_id and embedding is not None:
                                        try:
                                            # Reload identity from database (it may be from different session)
                                            result = await db.execute(
                                                select(Identity).where(Identity.id == identity_id)
                                            )
                                            identity_obj = result.scalar_one_or_none()
                                            
                                            if identity_obj:
                                                # Get track_id if available (not stored in batch, so None for now)
                                                track_id = None
                                                
                                                # Get quality_score and similarity for best snapshot selection
                                                quality_score = None
                                                similarity = face.get("similarity", 0.0)
                                                
                                                # Try to get quality_score from the embedding
                                                if detection_id:
                                                    from db_models import IdentityEmbedding
                                                    emb_result = await db.execute(
                                                        select(IdentityEmbedding.quality).where(
                                                            IdentityEmbedding.identity_id == identity_obj.id,
                                                            IdentityEmbedding.detection_id == detection_id
                                                        ).order_by(IdentityEmbedding.created_at.desc()).limit(1)
                                                    )
                                                    quality_result = emb_result.scalar_one_or_none()
                                                    if quality_result:
                                                        quality_score = quality_result
                                                
                                                await identity_service.create_appearance(
                                                    identity=identity_obj,
                                                    pipeline_id=d["pipeline_id"],
                                                    track_id=track_id,
                                                    start_time=detection_timestamp,
                                                    best_snapshot_path=face.get("face_image_path"),
                                                    db=db,
                                                    quality_score=quality_score,
                                                    similarity=similarity
                                                )
                                                
                                                # Update embedding record with detection_id
                                                subquery = select(IdentityEmbedding.id).where(
                                                    IdentityEmbedding.identity_id == identity_obj.id,
                                                    IdentityEmbedding.detection_id.is_(None)
                                                ).order_by(IdentityEmbedding.created_at.desc()).limit(1).scalar_subquery()
                                                
                                                await db.execute(
                                                    sql_update(IdentityEmbedding).where(
                                                        IdentityEmbedding.id == subquery
                                                    ).values(
                                                        detection_id=detection_id
                                                    )
                                                )
                                        except Exception as identity_error:
                                            logger.error(f"[BATCH] Identity appearance creation error: {identity_error}")
                                            # Don't fail the whole batch
                except ImportError:
                    # Identity service not available, skip
                    pass
                except Exception as e:
                    logger.warning(f"[BATCH] Identity appearance creation failed: {e}")

                # =====================================================
                # TX 4 — update pipeline counters (SHORT TX)
                # =====================================================
                async with db_manager.get_session() as db:
                    for pid in pipeline_ids:
                        await db.execute(
                            update(Pipeline)
                            .where(Pipeline.pipeline_id == pid)
                            .values(
                                total_detections=Pipeline.total_detections + 1,
                                updated_at=datetime.utcnow()
                            )
                        )

            await db_circuit_breaker.call_succeeded()
            metrics_db_operations.observe(time.time() - start_time)

            logger.info(
                f"✅ BULK flushed {len(batch)} detections "
                f"in {time.time() - start_time:.3f}s"
            )

        except asyncio.CancelledError:
            # Shutdown in progress, re-raise to allow proper cleanup
            logger.info("⚠️  Batch write cancelled (shutdown in progress)")
            self.pending_detections = batch + self.pending_detections  # Re-add to pending
            raise
        except asyncio.TimeoutError:
            await db_circuit_breaker.call_failed()
            logger.error(f"❌ Batch write timeout after {DB_OPERATION_TIMEOUT}s - database may be overloaded")
            # Don't re-add to pending as this might cause infinite retries
        except Exception as e:
            await db_circuit_breaker.call_failed()
            logger.exception("❌ Batch write failed")

    async def _periodic_flush(self):
        while True:
            try:
                await asyncio.sleep(self.flush_interval)
                await self.flush()
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("Batch periodic flush error")


batch_writer = BatchDatabaseWriter()


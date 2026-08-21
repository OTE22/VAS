"""
Identity Retention Manager
==========================
Manages cleanup of old identity data, snapshots, and embeddings.
"""

import os
import sys
import asyncio
import logging
from datetime import datetime, timedelta
from typing import Optional

# Add parent directory to path
parent_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if parent_dir not in sys.path:
    sys.path.insert(0, parent_dir)

from config import settings
from db_connection import db_manager
from db_models import (
    Identity, IdentityAppearance, IdentityEmbedding, Face,
    IdentityType, IdentityStatus
)
from sqlalchemy import select, func, and_, delete as sql_delete, text
from backend.core.vector_index.access import remove_embedding_keys

# A camera-origin embedding (pipeline_id IS NOT NULL) is written and committed
# BEFORE its detection is persisted (identity resolution precedes the realtime
# broadcast; persistence happens on the batch flush). If the worker dies in
# between, no detection ever links it. Rows older than this grace with no
# detection_id are therefore unexplained camera evidence and are removed.
# 10 minutes: the batch flush has a 150 s timeout plus queue backlog; a
# 2-minute boundary would race legitimate in-flight frames. One constant —
# imported by the data-quality query and the tests — not an operator setting.
STALE_CAMERA_EMBEDDING_GRACE = timedelta(minutes=10)

logger = logging.getLogger(__name__)


class IdentityRetentionManager:
    """
    Manages retention and cleanup for identity data:
    - Delete old snapshots based on retention policy
    - Mark inactive identities
    - Keep only top-K best embeddings per identity
    - Clean up merged/inactive identities
    """
    
    def __init__(
        self,
        snapshot_retention_days: Optional[int] = None,
        embedding_retention_months: Optional[int] = None,
        inactive_threshold_days: Optional[int] = None,
        cleanup_interval_hours: Optional[int] = None,
        max_embeddings_per_identity: Optional[int] = None
    ):
        # Explicit ctor overrides (tests); otherwise LIVE properties below read
        # settings at each use, so admin changes apply at the next job run
        # without a restart (previously frozen at import into the singleton).
        self._snapshot_retention_days_override = snapshot_retention_days
        self._embedding_retention_months_override = embedding_retention_months
        self._inactive_threshold_days_override = inactive_threshold_days
        self._cleanup_interval_hours_override = cleanup_interval_hours
        self._max_embeddings_per_identity_override = max_embeddings_per_identity
        self._cleanup_task = None

    @property
    def snapshot_retention_days(self) -> int:
        return self._snapshot_retention_days_override or int(settings.SNAPSHOT_RETENTION_DAYS)

    @property
    def embedding_retention_months(self) -> int:
        return self._embedding_retention_months_override or int(settings.EMBEDDING_RETENTION_MONTHS)

    @property
    def inactive_threshold_days(self) -> int:
        return self._inactive_threshold_days_override or int(settings.INACTIVE_THRESHOLD_DAYS)

    @property
    def cleanup_interval_hours(self) -> int:
        return self._cleanup_interval_hours_override or int(settings.IDENTITY_CLEANUP_INTERVAL_HOURS)

    @property
    def max_embeddings_per_identity(self) -> int:
        return self._max_embeddings_per_identity_override or int(settings.MAX_EMBEDDINGS_PER_IDENTITY)
    
    async def start(self):
        """Start periodic cleanup"""
        if self._cleanup_task and not self._cleanup_task.done():
            logger.warning("Identity retention manager already running; ignoring duplicate start()")
            return
        from backend.core.service_supervisor import supervised_loop
        self._cleanup_task = asyncio.create_task(
            supervised_loop(
                "identity_retention",
                (self.cleanup_interval_hours * 3600) - 60,
                self._run_cycle,
                initial_delay=3600,
                error_backoff_base=3600,
            ),
            name="identity_retention",
        )
        logger.info(
            f"Identity retention manager started "
            f"(snapshot retention: {self.snapshot_retention_days} days, "
            f"embedding retention: {self.embedding_retention_months} months)"
        )

    async def stop(self):
        """Stop cleanup task"""
        if self._cleanup_task:
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except asyncio.CancelledError:
                pass
        logger.info("Identity retention manager stopped")

    async def _run_cycle(self):
        """One retention cycle (notify + 60s lead stay inside the cycle)."""
        try:
            from backend.core.background_task_notifier import background_task_notifier, TaskType
            from datetime import datetime, timedelta
            next_run_time = datetime.utcnow() + timedelta(seconds=60)
            await background_task_notifier.notify_task_starting(
                task_type=TaskType.IDENTITY_RETENTION,
                task_name="Identity Retention Cleanup",
                description=f"Cleaning up old identity snapshots (older than {self.snapshot_retention_days} days), marking inactive identities, and removing excess embeddings. This affects unknown faces that users can access.",
                estimated_duration="3-10 minutes",
                scheduled_time=next_run_time,
                notify_all_users=True  # Notify all users since this affects unknown faces they can see
            )
            await asyncio.sleep(60)  # Wait 1 minute before starting
        except Exception as e:
            logger.warning(f"[IDENTITY_RETENTION] Failed to send notification: {e}")

        await self.run_cleanup()  # Completion notification is sent inside run_cleanup()
    
    async def run_cleanup(self):
        """Run all cleanup operations"""
        logger.info("🔄 Starting identity retention cleanup...")
        start_time = datetime.utcnow()
        
        try:
            # 1. Delete old snapshots
            deleted_snapshots = await self._cleanup_old_snapshots()
            
            # 2. Mark inactive identities
            marked_inactive = await self._mark_inactive_identities()
            
            # 3. Clean up excess embeddings
            cleaned_embeddings = await self._cleanup_excess_embeddings()

            # 4. Crash-safe provenance: remove stale camera embeddings whose
            #    originating detection was never persisted.
            stale = await self.reconcile_orphan_camera_embeddings()

            # 5. Clean up merged identities (optional - keep for audit)
            # We'll keep merged identities for audit trail
            
            duration = (datetime.utcnow() - start_time).total_seconds()
            logger.info(
                f"✅ Identity cleanup completed: "
                f"{deleted_snapshots} snapshots deleted, "
                f"{marked_inactive} identities marked inactive, "
                f"{cleaned_embeddings} excess embeddings removed, "
                f"{stale.get('embeddings', 0)} stale camera embeddings reconciled "
                f"in {duration:.2f}s"
            )
            
            # Send completion notification
            try:
                from backend.core.background_task_notifier import background_task_notifier, TaskType
                await background_task_notifier.notify_task_completed(
                    task_type=TaskType.IDENTITY_RETENTION,
                    task_name="Identity Retention Cleanup",
                    success=True,
                    duration_seconds=duration,
                    details={
                        "deleted_snapshots": deleted_snapshots,
                        "marked_inactive": marked_inactive,
                        "cleaned_embeddings": cleaned_embeddings,
                        "stale_camera_embeddings": stale.get("embeddings", 0),
                    },
                    notify_all_users=True  # Notify all users since this affects unknown faces they can see
                )
            except Exception as e:
                logger.debug(f"[IDENTITY_RETENTION] Failed to send completion notification: {e}")
        except Exception as e:
            logger.error(f"Identity cleanup failed: {e}", exc_info=True)
    
    async def reconcile_orphan_camera_embeddings(self, grace: Optional[timedelta] = None) -> dict:
        """The ONE canonical stale-embedding reconciliation.

        Invariant: a camera-origin embedding (pipeline_id IS NOT NULL) either
        ends with an exact detection_id or is removed. Controlled failures are
        compensated inline by detection_evidence; this covers the crash case
        (worker died between the embedding commit and persist_detection).

        Candidates: pipeline_id IS NOT NULL AND detection_id IS NULL AND
        created_at < now - grace. Removal uses the canonical path — vector-index
        keys first (remove_embedding_keys), then the rows — so FAISS/pgvector
        state and the table stay consistent. Then an UNKNOWN identity that this
        left with zero embeddings, images, appearances and faces (i.e. it existed
        only because of the lost frame) is removed; anything referenced by
        history stays. Idempotent: a second run finds nothing.

        Runs at startup (after the vector index is up) and on every retention
        cycle. Never touches enrollment/preload rows (pipeline_id IS NULL) or
        rows younger than the grace.
        """
        grace = grace or STALE_CAMERA_EMBEDDING_GRACE
        boundary = datetime.utcnow() - grace
        removed = {"embeddings": 0, "identities": 0, "boundary": boundary.isoformat() + "Z"}
        try:
            async with db_manager.get_session() as db:
                rows = (await db.execute(
                    select(IdentityEmbedding.id, IdentityEmbedding.identity_id).where(
                        IdentityEmbedding.pipeline_id.isnot(None),
                        IdentityEmbedding.detection_id.is_(None),
                        IdentityEmbedding.created_at < boundary,
                    ).order_by(IdentityEmbedding.id)
                )).all()
                if not rows:
                    return removed
                ids = [int(r[0]) for r in rows]
                identity_ids = sorted({str(r[1]) for r in rows})
                await remove_embedding_keys(db, ids)
                res = await db.execute(text(
                    "DELETE FROM identity_embeddings WHERE id = ANY(CAST(:ids AS int[])) "
                    "AND detection_id IS NULL"), {"ids": ids})
                removed["embeddings"] = res.rowcount or 0
                for iid in identity_ids:
                    res = await db.execute(text("""
                        DELETE FROM identities i WHERE i.id = CAST(:iid AS uuid)
                          AND i.type::text = 'UNKNOWN'
                          AND NOT EXISTS (SELECT 1 FROM identity_embeddings e WHERE e.identity_id = i.id)
                          AND NOT EXISTS (SELECT 1 FROM identity_appearances a WHERE a.identity_id = i.id)
                          AND NOT EXISTS (SELECT 1 FROM faces f WHERE f.identity_id = i.id)
                          AND NOT EXISTS (SELECT 1 FROM identity_images g WHERE g.identity_id = i.id)
                    """), {"iid": iid})
                    removed["identities"] += res.rowcount or 0
                await db.commit()
            logger.warning(
                "[IDENTITY_RETENTION] reconciled %s stale camera embedding(s) older than %s "
                "(no persisted detection) and %s evidence-free identity(ies)",
                removed["embeddings"], grace, removed["identities"])
        except Exception as e:
            logger.error(f"[IDENTITY_RETENTION] stale camera embedding reconciliation failed: {e}", exc_info=True)
        return removed

    async def _cleanup_old_snapshots(self) -> int:
        """Delete snapshots older than retention policy"""
        cutoff_date = datetime.utcnow() - timedelta(days=self.snapshot_retention_days)
        deleted_count = 0
        
        try:
            async with db_manager.get_session() as db:
                # Get old appearances
                result = await db.execute(
                    select(IdentityAppearance).where(
                        IdentityAppearance.start_time < cutoff_date
                    )
                )
                old_appearances = result.scalars().all()
                
                # Enrollment gallery (storage/faces/) must NEVER be swept - snapshots
                # for KNOWN identities can point at their enrollment photos, and
                # deleting them silently emptied the gallery.
                from config import settings as _settings
                # No fallback: a relative default here would silently mis-scope
                # the "never sweep enrollment photos" guard below.
                _faces_dir = os.path.realpath(_settings.FACES_DIR)

                def _is_enrollment_photo(path: str) -> bool:
                    try:
                        return os.path.realpath(path).startswith(_faces_dir + os.sep)
                    except Exception:
                        return False

                for appearance in old_appearances:
                    if appearance.best_snapshot_path and os.path.exists(appearance.best_snapshot_path):
                        if _is_enrollment_photo(appearance.best_snapshot_path):
                            logger.debug(f"[IDENTITY_RETENTION] Skipping enrollment photo: {appearance.best_snapshot_path}")
                            continue
                        try:
                            os.remove(appearance.best_snapshot_path)
                            deleted_count += 1
                        except Exception as e:
                            logger.error(f"Failed to delete snapshot {appearance.best_snapshot_path}: {e}")

                    # Clear snapshot path in database
                    appearance.best_snapshot_path = None
                
                # Also clean up identity best_snapshot_path if it's old
                identity_result = await db.execute(
                    select(Identity).where(
                        and_(
                            Identity.best_snapshot_path.isnot(None),
                            Identity.last_seen_at < cutoff_date
                        )
                    )
                )
                old_identities = identity_result.scalars().all()
                
                for identity in old_identities:
                    if identity.best_snapshot_path and os.path.exists(identity.best_snapshot_path):
                        if _is_enrollment_photo(identity.best_snapshot_path):
                            logger.debug(f"[IDENTITY_RETENTION] Skipping enrollment photo: {identity.best_snapshot_path}")
                            continue
                        try:
                            os.remove(identity.best_snapshot_path)
                            deleted_count += 1
                        except Exception as e:
                            logger.error(f"Failed to delete identity snapshot {identity.best_snapshot_path}: {e}")

                    identity.best_snapshot_path = None
                
                await db.commit()
        
        except Exception as e:
            logger.error(f"Error cleaning up old snapshots: {e}", exc_info=True)
        
        return deleted_count
    
    async def _mark_inactive_identities(self) -> int:
        """Mark identities as inactive if not seen in threshold days"""
        cutoff_date = datetime.utcnow() - timedelta(days=self.inactive_threshold_days)
        marked_count = 0
        
        try:
            async with db_manager.get_session() as db:
                result = await db.execute(
                    select(Identity).where(
                        and_(
                            Identity.status == IdentityStatus.ACTIVE,
                            Identity.last_seen_at < cutoff_date
                        )
                    )
                )
                inactive_identities = result.scalars().all()
                
                for identity in inactive_identities:
                    identity.status = IdentityStatus.INACTIVE
                    marked_count += 1
                if inactive_identities:
                    from backend.core.identity_service import invalidate_merge_suggestions
                    await invalidate_merge_suggestions(
                        db, [i.id for i in inactive_identities], "identity retired to INACTIVE by retention")

                await db.commit()
        
        except Exception as e:
            logger.error(f"Error marking inactive identities: {e}", exc_info=True)
        
        return marked_count
    
    async def _cleanup_excess_embeddings(self) -> int:
        """Cap the CAMERA-derived embeddings per identity. Never the gallery.

        An identity accumulates vectors from two very different sources:

          * enrollment — one per identity_images row, deliberately chosen by an
            administrator, bounded already at MAX_IMAGES_PER_IDENTITY (1000);
          * camera     — created on their own, at whatever rate the cameras
            happen to see that person.

        This used to trim both together against one limit of 10, which meant a
        busy camera evicted the photos somebody had deliberately enrolled. The
        quality ordering did not protect them: enrollment rows carried
        `quality = NULL`, and `NULLS LAST` sorts nulls to exactly the end of
        the keep-order that the deletions are taken from — so the curated
        gallery was pruned FIRST, and a sharp camera frame legitimately
        outscores a deliberately enrolled profile angle even now that
        enrollment records a real score.

        Worse, it left the identity_images row in place. The photo stayed
        visible in the gallery while contributing nothing to recognition, with
        nothing to indicate that. This module already refuses to delete
        enrollment FILES (`_is_enrollment_photo` below); deleting their vectors
        contradicted that in the least visible way possible.

        WHAT COUNTS AS CAMERA-DERIVED
        `pipeline_id` is the schema's own marker: db_models.IdentityEmbedding
        documents "NULL = not from a camera (enrolled photo -> image_id, or
        preloaded gallery)", and scripts/backfill_identity_images.py selects
        enrollment rows with `pipeline_id IS NULL` for the same reason.

        image_id alone is NOT sufficient and must not be used on its own: a
        preloaded gallery vector, and any enrollment predating the
        identity_images table, legitimately has image_id NULL with pipeline_id
        NULL. Trimming on image_id alone would treat those as camera traffic
        and delete exactly the vectors the backfill script exists to rescue.

        So a row is pruned only when it is unambiguously camera-derived —
        pipeline_id present AND no gallery image. Anything else, including a
        malformed row carrying both, is left alone: for a cap, failing toward
        "keep" is the only safe direction.
        """
        removed_count = 0

        try:
            async with db_manager.get_session() as db:
                # Get all active identities
                result = await db.execute(
                    select(Identity).where(
                        Identity.status == IdentityStatus.ACTIVE
                    )
                )
                identities = result.scalars().all()

                for identity in identities:
                    # Unambiguously camera-derived only — see the docstring.
                    emb_result = await db.execute(
                        select(IdentityEmbedding).where(
                            IdentityEmbedding.identity_id == identity.id,
                            IdentityEmbedding.pipeline_id.isnot(None),
                            IdentityEmbedding.image_id.is_(None),
                        ).order_by(
                            IdentityEmbedding.quality.desc().nulls_last(),
                            IdentityEmbedding.created_at.desc()
                        )
                    )
                    embeddings = emb_result.scalars().all()
                    
                    if len(embeddings) > self.max_embeddings_per_identity:
                        # Keep top-K, remove the rest
                        to_remove = embeddings[self.max_embeddings_per_identity:]
                        
                        # Drop exactly the keys being deleted. The old code
                        # passed the IDENTITY id to remove_from_known/unknown,
                        # so trimming one surplus embedding evicted every
                        # vector that person had — including the ones it was
                        # deliberately keeping.
                        #
                        # strict=True, and the DELETE only follows a successful
                        # removal: swallowing an index failure here would delete
                        # the rows while the in-process index kept their keys,
                        # and a later search would resolve a key to nothing.
                        # Leaving the rows in place instead keeps the two
                        # consistent and lets the next cycle retry.
                        try:
                            await remove_embedding_keys(
                                db, [e.id for e in to_remove], strict=True)
                        except Exception as index_error:        # noqa: BLE001
                            logger.warning(
                                "[IDENTITY_RETENTION] index removal failed for "
                                "identity %s; keeping %d vector(s) so the "
                                "database and the index cannot diverge: %s",
                                identity.id, len(to_remove), index_error)
                            continue

                        for emb in to_remove:
                            await db.delete(emb)
                            removed_count += 1
                
                await db.commit()
                
                # No explicit snapshot here: the index is derived state and the
                # manager's autosave owns snapshot timing. Forcing a save on
                # every retention pass is what produced hundreds of redundant
                # writes, and a missed one costs nothing — reconciliation
                # re-derives the index from PostgreSQL.
        
        except Exception as e:
            logger.error(f"Error cleaning up excess embeddings: {e}", exc_info=True)
        
        return removed_count
    
    async def cleanup_merged_identities(self, older_than_days: int = 365):
        """
        Optionally clean up old merged identities (for audit trail, usually keep them).
        This is optional and can be called manually.
        """
        cutoff_date = datetime.utcnow() - timedelta(days=older_than_days)
        
        try:
            async with db_manager.get_session() as db:
                result = await db.execute(
                    select(Identity).where(
                        and_(
                            Identity.status == IdentityStatus.MERGED,
                            Identity.updated_at < cutoff_date
                        )
                    )
                )
                old_merged = result.scalars().all()
                
                # For audit purposes, we usually keep merged identities
                # But we can mark them for archival
                logger.info(f"Found {len(old_merged)} old merged identities (keeping for audit)")
        
        except Exception as e:
            logger.error(f"Error cleaning up merged identities: {e}", exc_info=True)


# Global instance
identity_retention_manager = IdentityRetentionManager()


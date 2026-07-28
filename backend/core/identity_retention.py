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
from sqlalchemy import select, func, and_, delete as sql_delete
from backend.core.identity_index import identity_index

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
        return self._snapshot_retention_days_override or int(getattr(settings, 'SNAPSHOT_RETENTION_DAYS', 90))

    @property
    def embedding_retention_months(self) -> int:
        return self._embedding_retention_months_override or int(getattr(settings, 'EMBEDDING_RETENTION_MONTHS', 12))

    @property
    def inactive_threshold_days(self) -> int:
        return self._inactive_threshold_days_override or int(getattr(settings, 'INACTIVE_THRESHOLD_DAYS', 180))

    @property
    def cleanup_interval_hours(self) -> int:
        return self._cleanup_interval_hours_override or int(getattr(settings, 'IDENTITY_CLEANUP_INTERVAL_HOURS', 24))

    @property
    def max_embeddings_per_identity(self) -> int:
        return self._max_embeddings_per_identity_override or int(getattr(settings, 'MAX_EMBEDDINGS_PER_IDENTITY', 10))
    
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
            
            # 4. Clean up merged identities (optional - keep for audit)
            # We'll keep merged identities for audit trail
            
            duration = (datetime.utcnow() - start_time).total_seconds()
            logger.info(
                f"✅ Identity cleanup completed: "
                f"{deleted_snapshots} snapshots deleted, "
                f"{marked_inactive} identities marked inactive, "
                f"{cleaned_embeddings} excess embeddings removed "
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
                        "cleaned_embeddings": cleaned_embeddings
                    },
                    notify_all_users=True  # Notify all users since this affects unknown faces they can see
                )
            except Exception as e:
                logger.debug(f"[IDENTITY_RETENTION] Failed to send completion notification: {e}")
        except Exception as e:
            logger.error(f"Identity cleanup failed: {e}", exc_info=True)
    
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
                _faces_dir = os.path.realpath(getattr(_settings, 'FACES_DIR', './storage/faces'))

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
                
                await db.commit()
        
        except Exception as e:
            logger.error(f"Error marking inactive identities: {e}", exc_info=True)
        
        return marked_count
    
    async def _cleanup_excess_embeddings(self) -> int:
        """
        Keep only top-K best embeddings per identity.
        Removes lower quality embeddings beyond the limit.
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
                    # Get embeddings ordered by quality (best first)
                    emb_result = await db.execute(
                        select(IdentityEmbedding).where(
                            IdentityEmbedding.identity_id == identity.id
                        ).order_by(
                            IdentityEmbedding.quality.desc().nulls_last(),
                            IdentityEmbedding.created_at.desc()
                        )
                    )
                    embeddings = emb_result.scalars().all()
                    
                    if len(embeddings) > self.max_embeddings_per_identity:
                        # Keep top-K, remove the rest
                        to_remove = embeddings[self.max_embeddings_per_identity:]
                        
                        for emb in to_remove:
                            # Remove from FAISS index if possible
                            if emb.faiss_id is not None and identity_index:
                                try:
                                    if emb.faiss_index_type == 'known':
                                        identity_index.remove_from_known(str(identity.id))
                                    else:
                                        identity_index.remove_from_unknown(str(identity.id))
                                except Exception as e:
                                    logger.warning(f"Failed to remove from FAISS: {e}")
                            
                            # Delete from database
                            await db.delete(emb)
                            removed_count += 1
                
                await db.commit()
                
                # Save FAISS index after cleanup
                if identity_index:
                    identity_index.save()
        
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


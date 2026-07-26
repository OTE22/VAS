"""
Data Retention Manager
======================
Manages automatic cleanup of old data.
"""

import os
import sys
import asyncio
import logging
from datetime import datetime, timedelta

# Add parent directory to path
parent_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if parent_dir not in sys.path:
    sys.path.insert(0, parent_dir)

from config import settings
from backend.core.metrics import metrics_cleanup_operations
from db_connection import db_manager
from db_models import Detection, Face
from sqlalchemy import select, delete as sa_delete, func, text as sa_text

logger = logging.getLogger(__name__)

# Postgres advisory-lock key: prevents two retention runs (scheduled + manual,
# or multiple processes) from executing simultaneously.
RETENTION_ADVISORY_LOCK_KEY = 823451


def _delete_files_sync(paths, protected_root):
    """Blocking file deletion — runs in the default executor, never on the loop.

    Returns (deleted_count, freed_bytes, missing_count, failures). Missing
    files are counted (not failures) and never abort the run. Anything under
    protected_root (the enrollment gallery) or outside STORAGE_DIR is refused
    (path traversal guard).
    """
    deleted, freed, missing, failures = 0, 0, 0, []
    storage_root = os.path.realpath(settings.STORAGE_DIR)
    for path in paths:
        try:
            real = os.path.realpath(path)
            if real.startswith(protected_root + os.sep):
                continue  # enrollment photo — source of truth for KNOWN identities
            if not real.startswith(storage_root + os.sep):
                failures.append(f"refused (outside storage root): {path}")
                continue
            if not os.path.exists(real):
                missing += 1  # already gone — tolerated, cleanup continues
                continue
            size = os.path.getsize(real)
            os.remove(real)
            deleted += 1
            freed += size
        except Exception as e:
            failures.append(f"{path}: {e}")
    return deleted, freed, missing, failures


def _stat_files_sync(paths):
    """Blocking existence/size scan for DRY RUNS — touches nothing.

    Returns (existing_count, missing_count, estimated_bytes).
    """
    existing, missing, size_total = 0, 0, 0
    for path in paths:
        try:
            if path and os.path.exists(path):
                existing += 1
                size_total += os.path.getsize(path)
            else:
                missing += 1
        except Exception:
            missing += 1
    return existing, missing, size_total


class DataRetentionManager:
    """Manages automatic cleanup of old data.

    Settings are read at the START OF EACH RUN (not frozen at import), so
    admin changes to DATA_RETENTION_DAYS / CLEANUP_INTERVAL_HOURS apply at
    the next job run without a restart.
    """

    def __init__(self):
        self._cleanup_task = None
        self._run_lock = asyncio.Lock()      # in-process overlap guard
        self.last_run_at = None
        self.last_result = None              # structured result of the last run

    # --- live setting reads (apply_mode = next_job_run) ---------------------
    @property
    def retention_days(self) -> int:
        return int(getattr(settings, 'DATA_RETENTION_DAYS', 30))

    @property
    def cleanup_interval_hours(self) -> int:
        return int(getattr(settings, 'CLEANUP_INTERVAL_HOURS', 24))

    async def start(self):
        """Start periodic cleanup"""
        self._cleanup_task = asyncio.create_task(self._periodic_cleanup())
        logger.info(f"Data retention manager started (retention: {self.retention_days} days)")

    async def stop(self):
        """Stop cleanup task"""
        if self._cleanup_task:
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except asyncio.CancelledError:
                pass
        logger.info("Data retention manager stopped")

    async def _periodic_cleanup(self):
        """Run cleanup periodically (interval re-read every cycle)"""
        await asyncio.sleep(60)

        while True:
            try:
                # Send notification 1 minute before cleanup starts
                try:
                    from backend.core.background_task_notifier import background_task_notifier, TaskType
                    next_run_time = datetime.utcnow() + timedelta(seconds=60)
                    await background_task_notifier.notify_task_starting(
                        task_type=TaskType.DATA_RETENTION,
                        task_name="Data Retention Cleanup",
                        description=f"Removing old detections and face images older than {self.retention_days} days. This affects all users as it deletes detection records and images.",
                        estimated_duration="5-15 minutes",
                        scheduled_time=next_run_time,
                        notify_all_users=True
                    )
                    await asyncio.sleep(60)
                except Exception as e:
                    logger.warning(f"[RETENTION] Failed to send notification: {e}")

                await self.cleanup_old_data()

                # Interval read LIVE each cycle so admin changes apply next loop
                await asyncio.sleep((self.cleanup_interval_hours * 3600) - 60)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Cleanup error: {e}")
                await asyncio.sleep(3600)  # Retry in 1 hour

    async def cleanup_old_data(self, dry_run: bool = False, job_id: str = None,
                               progress_cb=None, cancel_check=None) -> dict:
        """Delete (or, with dry_run=True, only COUNT) expired data.

        Args:
            dry_run:      selection + counting only, deletes NOTHING
            job_id:       background-job id for [RETENTION] log correlation
            progress_cb:  async callable(percent, details) — called per batch
            cancel_check: callable() -> bool — checked between batches
                          (cooperative cancellation; a cancelled run reports
                          status='cancelled' with partial counts)

        Returns a structured result:
        {status, dry_run, job_id, retention_days, cutoff, rows_scanned,
         candidate_rows, rows_deleted, files_deleted, missing_files,
         bytes_freed, batch_count, failures, duration_seconds,
         extra: {search_history_deleted, task_history_deleted, audit_logs_deleted}}
        Dry runs additionally report: candidate_files, existing_files,
        estimated_freed_bytes, sample_candidate_ids.
        """
        import time
        start_time = time.time()
        jid = job_id or "-"

        # In-process overlap guard: never two runs at once
        if self._run_lock.locked():
            logger.warning(f"[RETENTION] job_id={jid} run skipped — another retention run is in progress")
            return {"status": "skipped", "reason": "already_running", "dry_run": dry_run, "job_id": job_id}

        async with self._run_lock:
            retention_days = self.retention_days  # read ONCE at run start
            cutoff_date = datetime.utcnow() - timedelta(days=retention_days)
            # Safety floor: regardless of configuration, never touch records
            # newer than 24 hours (protects against a fat-fingered retention=0)
            safety_floor = datetime.utcnow() - timedelta(days=1)
            effective_cutoff = min(cutoff_date, safety_floor)

            result = {
                "status": "completed", "dry_run": dry_run, "job_id": job_id,
                "retention_days": retention_days,
                "cutoff": effective_cutoff.isoformat(),
                "rows_scanned": 0, "candidate_rows": 0, "rows_deleted": 0,
                "files_deleted": 0, "missing_files": 0, "bytes_freed": 0,
                "batch_count": 0, "failures": [],
                "extra": {},
            }

            # Legacy lifecycle for the periodic scheduled run (no job wrapper):
            # promote the open 'scheduled' history row to 'running'.
            if job_id is None and not dry_run:
                try:
                    from backend.core.task_history import task_history_manager
                    await task_history_manager.record_task_started(
                        task_type="data_retention", task_name="Data Retention Cleanup",
                        notify_all_users=True)
                except Exception:
                    pass

            logger.info(f"[RETENTION] job_id={jid} {'DRY RUN' if dry_run else 'Run'} starting: cutoff={effective_cutoff} (days={retention_days})")
            loop = asyncio.get_running_loop()
            faces_dir = os.path.realpath(getattr(settings, 'FACES_DIR', os.path.join(settings.STORAGE_DIR, 'faces')))

            try:
                # Cross-process overlap guard (Postgres advisory lock) on a
                # DEDICATED connection: advisory locks are connection-bound,
                # and the ORM session's connection can rotate through the pool
                # across the per-batch commits — locking through the session
                # can therefore unlock on the WRONG connection and leak the
                # lock. Closing this connection always releases the lock.
                lock_conn = await db_manager.engine.connect()
                try:
                    got_lock = (await lock_conn.execute(
                        sa_text("SELECT pg_try_advisory_lock(:k)"), {"k": RETENTION_ADVISORY_LOCK_KEY}
                    )).scalar()
                    if not got_lock:
                        await lock_conn.close()
                        logger.warning(f"[RETENTION] job_id={jid} run skipped — advisory lock held by another process")
                        return {"status": "skipped", "reason": "advisory_lock_held", "dry_run": dry_run, "job_id": job_id}
                except Exception:
                    await lock_conn.close()
                    raise

                async with db_manager.get_session() as db:
                    try:
                        total = (await db.execute(select(func.count(Detection.id)))).scalar() or 0
                        candidates = (await db.execute(
                            select(func.count(Detection.id)).where(Detection.timestamp < effective_cutoff)
                        )).scalar() or 0
                        result["rows_scanned"] = total
                        result["candidate_rows"] = candidates

                        if dry_run:
                            # Selection + counting only — NOTHING is deleted
                            path_rows = await db.execute(
                                select(Face.face_image_path)
                                .join(Detection, Face.detection_id == Detection.id)
                                .where(Detection.timestamp < effective_cutoff,
                                       Face.face_image_path.isnot(None))
                                .limit(5000)  # bounded scan for the estimate
                            )
                            paths = [r[0] for r in path_rows.all()]
                            existing, missing, est_bytes = await loop.run_in_executor(
                                None, _stat_files_sync, paths
                            )
                            sample_rows = await db.execute(
                                select(Detection.id)
                                .where(Detection.timestamp < effective_cutoff)
                                .order_by(Detection.id).limit(10)
                            )
                            result["candidate_files"] = len(paths)
                            result["existing_files"] = existing
                            result["missing_files"] = missing
                            result["estimated_freed_bytes"] = est_bytes
                            result["sample_candidate_ids"] = [r[0] for r in sample_rows.all()]
                        else:
                            batch_size = 1000
                            while True:
                                if cancel_check and cancel_check():
                                    result["status"] = "cancelled"
                                    logger.warning(f"[RETENTION] job_id={jid} CANCELLED after {result['rows_deleted']} rows")
                                    break

                                id_rows = await db.execute(
                                    select(Detection.id)
                                    .where(Detection.timestamp < effective_cutoff)
                                    .limit(batch_size)
                                )
                                ids = [r[0] for r in id_rows.all()]
                                if not ids:
                                    break

                                # Face image paths for this batch (explicit query —
                                # no async lazy-loading of detection.faces)
                                path_rows = await db.execute(
                                    select(Face.face_image_path)
                                    .where(Face.detection_id.in_(ids), Face.face_image_path.isnot(None))
                                )
                                paths = [r[0] for r in path_rows.all()]

                                # Files first (off-loop), then rows (DB cascades faces;
                                # identity_embeddings.detection_id goes NULL via FK)
                                f_deleted, f_freed, f_missing, f_failures = await loop.run_in_executor(
                                    None, _delete_files_sync, paths, faces_dir
                                )
                                result["files_deleted"] += f_deleted
                                result["bytes_freed"] += f_freed
                                result["missing_files"] += f_missing
                                result["failures"].extend(f_failures[:20])

                                await db.execute(sa_delete(Detection).where(Detection.id.in_(ids)))
                                await db.commit()
                                result["rows_deleted"] += len(ids)
                                result["batch_count"] += 1
                                logger.info(f"[RETENTION] job_id={jid} batch deleted: {len(ids)} detections (total {result['rows_deleted']})")

                                if progress_cb and candidates:
                                    try:
                                        percent = min(99, int(result["rows_deleted"] / candidates * 100))
                                        await progress_cb(percent, {
                                            "processed_rows": result["rows_deleted"],
                                            "total_rows": candidates,
                                        })
                                    except Exception:
                                        pass

                                if len(ids) < batch_size:
                                    break

                        # Previously-documented-but-never-enforced retentions
                        if result["status"] != "cancelled":
                            result["extra"] = await self._cleanup_auxiliary(db, dry_run)

                        if not dry_run and result["status"] != "cancelled":
                            await loop.run_in_executor(None, self._cleanup_empty_directories_sync)
                    finally:
                        try:
                            await lock_conn.execute(sa_text("SELECT pg_advisory_unlock(:k)"), {"k": RETENTION_ADVISORY_LOCK_KEY})
                        except Exception:
                            pass
                        finally:
                            # Closing the connection releases the advisory
                            # lock even if the unlock statement failed.
                            try:
                                await lock_conn.close()
                            except Exception:
                                pass

                # Detection history changed — cached map renders are stale
                if not dry_run and result["rows_deleted"] > 0:
                    try:
                        from backend.core.cache_manager import cache_manager
                        invalidated = await cache_manager.invalidate_prefix("map:")
                        result["extra"]["cache_keys_invalidated"] = invalidated
                    except Exception as e:
                        logger.debug(f"[RETENTION] cache invalidation failed: {e}")

                result["duration_seconds"] = round(time.time() - start_time, 2)
                self.last_run_at = datetime.utcnow()
                self.last_result = result

                logger.info(
                    "[RETENTION] job_id=%s dry_run=%s cutoff=%s candidate_rows=%d deleted_rows=%d deleted_files=%d missing_files=%d freed_bytes=%d duration_seconds=%.2f failures=%d status=%s",
                    jid, dry_run, result["cutoff"],
                    result["candidate_rows"], result["rows_deleted"], result["files_deleted"],
                    result["missing_files"], result["bytes_freed"],
                    result["duration_seconds"], len(result["failures"]), result["status"],
                )

                if not dry_run:
                    metrics_cleanup_operations.inc()
                # Notifier records/closes the legacy history row — only for the
                # periodic path. Job runs (job_id set) manage their own row.
                if not dry_run and job_id is None:
                    try:
                        from backend.core.background_task_notifier import background_task_notifier, TaskType
                        await background_task_notifier.notify_task_completed(
                            task_type=TaskType.DATA_RETENTION,
                            task_name="Data Retention Cleanup",
                            success=True,
                            duration_seconds=result["duration_seconds"],
                            details={
                                "deleted_detections": result["rows_deleted"],
                                "deleted_files": result["files_deleted"],
                                "freed_space_mb": round(result["bytes_freed"] / (1024 * 1024), 2),
                            },
                            notify_all_users=True
                        )
                    except Exception as e:
                        logger.debug(f"[RETENTION] Failed to send completion notification: {e}")

                return result

            except Exception as e:
                # FAILURES ARE RECORDED, not swallowed (previously a failed run
                # left no trace and looked like success)
                logger.error(f"[RETENTION] job_id={jid} run FAILED error_code=RETENTION_FAILED: {e}", exc_info=True)
                result["status"] = "failed"
                result["failures"].append(str(e))
                result["duration_seconds"] = round(time.time() - start_time, 2)
                self.last_run_at = datetime.utcnow()
                self.last_result = result
                # Close the legacy history row for the periodic path
                if job_id is None and not dry_run:
                    try:
                        from backend.core.task_history import task_history_manager
                        await task_history_manager.record_task_completed(
                            task_type="data_retention", task_name="Data Retention Cleanup",
                            success=False, duration_seconds=result["duration_seconds"],
                            details={"error": str(e)[:500]}, notify_all_users=True)
                    except Exception:
                        pass
                return result

    async def _cleanup_auxiliary(self, db, dry_run: bool) -> dict:
        """Retentions that were documented but never enforced anywhere:
        search history, background-task history, audit logs."""
        extra = {"search_history_deleted": 0, "task_history_deleted": 0, "audit_logs_deleted": 0}
        now = datetime.utcnow()

        try:
            sh_days = int(getattr(settings, 'SEARCH_HISTORY_RETENTION_DAYS', 90))
            sh_cutoff = now - timedelta(days=sh_days)
            if dry_run:
                extra["search_history_deleted"] = (await db.execute(sa_text(
                    "SELECT count(*) FROM search_history WHERE created_at < :c"), {"c": sh_cutoff})).scalar() or 0
            else:
                r = await db.execute(sa_text(
                    "DELETE FROM search_history WHERE created_at < :c"), {"c": sh_cutoff})
                extra["search_history_deleted"] = r.rowcount or 0
        except Exception as e:
            logger.warning(f"[RETENTION] search_history cleanup failed: {e}")

        try:
            th_cutoff = now - timedelta(days=30)
            if dry_run:
                extra["task_history_deleted"] = (await db.execute(sa_text(
                    "SELECT count(*) FROM background_task_history WHERE created_at < :c"), {"c": th_cutoff})).scalar() or 0
            else:
                r = await db.execute(sa_text(
                    "DELETE FROM background_task_history WHERE created_at < :c"), {"c": th_cutoff})
                extra["task_history_deleted"] = r.rowcount or 0
        except Exception as e:
            logger.warning(f"[RETENTION] task_history cleanup failed: {e}")

        try:
            al_days = int(getattr(settings, 'AUDIT_LOG_RETENTION_DAYS', 180))
            al_cutoff = now - timedelta(days=al_days)
            deleted = 0
            for table in ("chatbot_audit_log", "identity_audit_log", "settings_audit_log"):
                if dry_run:
                    deleted += (await db.execute(sa_text(
                        f"SELECT count(*) FROM {table} WHERE created_at < :c"), {"c": al_cutoff})).scalar() or 0
                else:
                    r = await db.execute(sa_text(
                        f"DELETE FROM {table} WHERE created_at < :c"), {"c": al_cutoff})
                    deleted += r.rowcount or 0
            extra["audit_logs_deleted"] = deleted
        except Exception as e:
            logger.warning(f"[RETENTION] audit-log cleanup failed: {e}")

        if not dry_run:
            await db.commit()
        return extra

    def _cleanup_empty_directories_sync(self):
        """Remove empty pipeline directories (blocking — call via executor)"""
        if not os.path.exists(settings.STORAGE_DIR):
            return

        for pipeline_dir in os.listdir(settings.STORAGE_DIR):
            dir_path = os.path.join(settings.STORAGE_DIR, pipeline_dir)

            if os.path.isdir(dir_path) and not os.listdir(dir_path):
                try:
                    os.rmdir(dir_path)
                    logger.info(f"Removed empty directory: {dir_path}")
                except Exception as e:
                    logger.error(f"Failed to remove directory {dir_path}: {e}")

    # Storage-stats cache: the full os.walk of the storage tree can take seconds
    # on large trees — never run it on the event loop, never more than once per
    # TTL, and never concurrently (single-flight lock).
    _storage_stats_cache: dict = None
    _storage_stats_cached_at: float = 0.0
    _STORAGE_STATS_TTL = 60.0
    _storage_stats_lock = None  # created lazily (needs a running loop)

    def _walk_storage_sync(self) -> dict:
        """Blocking storage walk — must run in an executor."""
        total_size = 0
        file_count = 0

        for root, dirs, files in os.walk(settings.STORAGE_DIR):
            for file in files:
                file_path = os.path.join(root, file)
                try:
                    total_size += os.path.getsize(file_path)
                    file_count += 1
                except Exception:
                    pass

        max_storage_gb = getattr(settings, 'MAX_STORAGE_GB', 100)

        return {
            "total_size_mb": total_size / (1024 * 1024),
            "total_size_gb": total_size / (1024 * 1024 * 1024),
            "file_count": file_count,
            "max_storage_gb": max_storage_gb,
            "usage_percent": min(100, (total_size / (1024 * 1024 * 1024)) / max_storage_gb * 100) if max_storage_gb > 0 else 0
        }

    async def get_storage_stats(self) -> dict:
        """Get storage statistics (cached 60s, walk runs off-loop, single-flight)"""
        import asyncio
        import time as _time

        now = _time.time()
        if self._storage_stats_cache is not None and now - self._storage_stats_cached_at < self._STORAGE_STATS_TTL:
            return self._storage_stats_cache

        if DataRetentionManager._storage_stats_lock is None:
            DataRetentionManager._storage_stats_lock = asyncio.Lock()

        async with DataRetentionManager._storage_stats_lock:
            # Another caller may have refreshed while we waited
            now = _time.time()
            if self._storage_stats_cache is not None and now - self._storage_stats_cached_at < self._STORAGE_STATS_TTL:
                return self._storage_stats_cache

            loop = asyncio.get_running_loop()
            stats = await loop.run_in_executor(None, self._walk_storage_sync)
            DataRetentionManager._storage_stats_cache = stats
            DataRetentionManager._storage_stats_cached_at = _time.time()
            return stats


retention_manager = DataRetentionManager()


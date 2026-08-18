"""
Log Cleanup Manager
===================
Manages automatic cleanup of old log files based on retention policy.
"""

import os
import sys
import asyncio
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List, Tuple, Optional

# Add parent directory to path
parent_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if parent_dir not in sys.path:
    sys.path.insert(0, parent_dir)

from config import settings

logger = logging.getLogger(__name__)


class LogCleanupManager:
    """
    Manages automatic cleanup of old log files.
    Removes log entries older than LOGS_LIFE_TIME_HOURS.
    """
    
    def __init__(self, retention_hours: int = None):
        """
        Initialize log cleanup manager.

        Args:
            retention_hours: explicit override; otherwise the value is a LIVE
                property read from settings each run (admin changes to
                LOGS_LIFE_TIME_HOURS apply at the next cleanup, no restart)
        """
        self._retention_hours_override = retention_hours
        self._cleanup_task = None
        self.log_dir = Path(settings.LOG_DIR)

    @property
    def retention_hours(self) -> int:
        if self._retention_hours_override is not None:
            return self._retention_hours_override
        return int(settings.LOGS_LIFE_TIME_HOURS)
    
    async def start(self):
        """Start periodic log cleanup"""
        if self._cleanup_task and not self._cleanup_task.done():
            logger.warning("Log cleanup manager already running; ignoring duplicate start()")
            return
        from backend.core.service_supervisor import supervised_loop
        # Cadence preserved: first run after 10 minutes, then every 6 hours
        # (minus the 60s notification lead inside the cycle); 1h backoff on
        # error was the old flat retry and is now the backoff BASE.
        self._cleanup_task = asyncio.create_task(
            supervised_loop(
                "log_cleanup",
                (6 * 3600) - 60,
                self._run_cycle,
                initial_delay=600,
                error_backoff_base=3600,
            ),
            name="log_cleanup",
        )
        logger.info(f"Log cleanup manager started (retention: {self.retention_hours} hours)")

    async def stop(self):
        """Stop cleanup task"""
        if self._cleanup_task:
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except asyncio.CancelledError:
                pass
        logger.info("Log cleanup manager stopped")

    async def _run_cycle(self):
        """One cleanup cycle: notify, wait the lead minute, clean, report.

        The notification and its 60s lead stay INSIDE the cycle so the
        task-history behavior (and its tests) are unchanged by supervision.
        """
        # Send notification 1 minute before cleanup starts
        try:
            from backend.core.background_task_notifier import background_task_notifier, TaskType
            next_run_time = datetime.utcnow() + timedelta(seconds=60)
            await background_task_notifier.notify_task_starting(
                task_type=TaskType.LOG_CLEANUP,
                task_name="Log Cleanup",
                description=f"Removing log entries older than {self.retention_hours} hours from app.log, error.log, and access.log files",
                estimated_duration="1-3 minutes",
                scheduled_time=next_run_time
            )
            await asyncio.sleep(60)  # Wait 1 minute before starting
        except Exception as e:
            logger.warning(f"[LOG_CLEANUP] Failed to send notification: {e}")

        import time
        start_time = time.time()
        result = await self.cleanup_old_logs()
        duration = time.time() - start_time
        deleted_lines, freed_space_mb = result

        # Send completion notification
        try:
            from backend.core.background_task_notifier import background_task_notifier, TaskType
            await background_task_notifier.notify_task_completed(
                task_type=TaskType.LOG_CLEANUP,
                task_name="Log Cleanup",
                success=True,
                duration_seconds=duration,
                details={
                    "deleted_lines": deleted_lines,
                    "freed_space_mb": round(freed_space_mb, 2)
                }
            )
        except Exception as e:
            logger.debug(f"[LOG_CLEANUP] Failed to send completion notification: {e}")
    
    async def cleanup_old_logs(self) -> Tuple[int, float]:
        """Apply the retention window by deleting whole ROTATED log files.

        It never touches the ACTIVE file. That file is held open by the
        RotatingFileHandler in utils/logging.py, which tracks its own write
        offset; the previous implementation read the whole file, filtered the
        lines, and rewrote it with open(..., 'w'). Two things went wrong with
        that:

          * the handler's next write lands at its remembered offset in a file
            that just got shorter, producing NUL padding or losing records;
          * it fights the handler's own maxBytes rotation, so the two mechanisms
            take turns undoing each other.

        Deleting a rotated file is atomic from the handler's point of view — it
        has no descriptor open on app.log.N — and it is what every other log
        retention system does. A rotated file's mtime is the time of its LAST
        record, so a file whose mtime predates the cutoff contains nothing worth
        keeping.

        Returns (files_deleted, freed_space_mb) — the tuple shape is unchanged
        for existing callers, but the first element now counts FILES, not lines.
        """
        if not self.log_dir.exists():
            logger.warning(f"Log directory not found: {self.log_dir}")
            return 0, 0.0

        cutoff_time = datetime.now(timezone.utc) - timedelta(hours=self.retention_hours)
        logger.info(
            "🔄 Starting log cleanup - deleting rotated files last written before "
            f"{cutoff_time} ({self.retention_hours}h retention)"
        )

        from utils.logging import active_log_path, rotated_log_paths

        active = os.path.realpath(active_log_path())
        # rotated_log_paths() is the SAME configured set the logger writes and
        # /api/logs reads, so retention can never act on a file outside it.
        candidates = [p for p in rotated_log_paths()
                      if os.path.realpath(p) != active]

        def _delete_expired_sync():
            removed = 0
            freed_mb = 0.0
            for path in candidates:
                try:
                    stat = os.stat(path)
                    modified = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc)
                    if modified >= cutoff_time:
                        continue
                    size_mb = stat.st_size / (1024 * 1024)
                    os.remove(path)
                    removed += 1
                    freed_mb += size_mb
                    logger.info(
                        f"✅ Deleted rotated log {os.path.basename(path)} "
                        f"(last written {modified}, freed {size_mb:.2f} MB)"
                    )
                except FileNotFoundError:
                    continue                       # rotated away underneath us
                except PermissionError as e:
                    logger.error(f"Permission denied deleting log file {path}: {e}")
                except Exception as e:             # noqa: BLE001
                    logger.error(f"Error deleting log file {path}: {e}", exc_info=True)
            return removed, freed_mb

        loop = asyncio.get_running_loop()
        deleted_files, freed_space_mb = await loop.run_in_executor(None, _delete_expired_sync)

        if deleted_files:
            logger.info(
                f"✅ Log cleanup completed: deleted {deleted_files} rotated file(s), "
                f"freed {freed_space_mb:.2f} MB"
            )
        else:
            logger.info(
                "ℹ️ Log cleanup completed: no rotated logs past retention "
                f"(retention: {self.retention_hours}h, cutoff: {cutoff_time})"
            )

        return deleted_files, freed_space_mb

    
    def _extract_timestamp_from_line(self, line: str) -> Optional[datetime]:
        """
        Extract timestamp from a log line.
        Supports multiple formats:
        - Gunicorn: [2026-01-03 18:03:08 +0000]
        - Python: 2026-01-03 18:03:08,123
        
        Always returns timezone-aware datetime (UTC) for consistent comparison.
        """
        import re
        
        # Try Gunicorn format: [2026-01-03 18:03:08 +0000]
        gunicorn_match = re.search(r'\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}[^\]]+)\]', line)
        if gunicorn_match:
            timestamp_str = gunicorn_match.group(1)
            try:
                # Try with timezone
                if '+' in timestamp_str or '-' in timestamp_str[-6:]:
                    return datetime.strptime(timestamp_str, "%Y-%m-%d %H:%M:%S %z")
                else:
                    # No timezone info - assume UTC
                    dt = datetime.strptime(timestamp_str, "%Y-%m-%d %H:%M:%S")
                    return dt.replace(tzinfo=timezone.utc)
            except:
                pass
        
        # Try Python format: 2026-01-03 18:03:08,123
        python_match = re.search(r'(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}(?:,\d+)?)', line)
        if python_match:
            timestamp_str = python_match.group(1)
            try:
                if ',' in timestamp_str:
                    dt = datetime.strptime(timestamp_str, "%Y-%m-%d %H:%M:%S,%f")
                else:
                    dt = datetime.strptime(timestamp_str, "%Y-%m-%d %H:%M:%S")
                # Add UTC timezone info for consistent comparison
                return dt.replace(tzinfo=timezone.utc)
            except:
                pass
        
        return None


# Global instance
log_cleanup_manager = LogCleanupManager()


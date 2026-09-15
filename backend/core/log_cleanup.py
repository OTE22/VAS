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
        from backend.core.service_supervisor import supervised_loop, durable_initial_delay
        initial_delay = await durable_initial_delay(
            "log_cleanup", 600, 6 * 3600, notification_lead_seconds=60)
        self._cleanup_task = asyncio.create_task(
            supervised_loop(
                "log_cleanup",
                (6 * 3600) - 60,
                self._run_cycle,
                initial_delay=initial_delay,
                jitter=0,
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
                description=f"Removing expired closed log files and diagnostic artifacts; application rotations retained for {self.retention_hours} hours",
                estimated_duration="1-3 minutes",
                scheduled_time=next_run_time
            )
            await asyncio.sleep(60)  # Wait 1 minute before starting
        except Exception as e:
            logger.warning(f"[LOG_CLEANUP] Failed to send notification: {e}")

        import time
        start_time = time.time()
        error = None
        try:
            deleted_files, freed_space_mb = await self.cleanup_old_logs()
        except Exception as exc:
            error = exc
            deleted_files = getattr(self, "last_result", {}).get("deleted_files", 0)
            freed_space_mb = getattr(self, "last_result", {}).get("freed_space_mb", 0)
        duration = time.time() - start_time
        from backend.core.background_task_notifier import background_task_notifier, TaskType
        await background_task_notifier.notify_task_completed(
            task_type=TaskType.LOG_CLEANUP, task_name="Log Cleanup",
            success=error is None, duration_seconds=duration,
            details={**getattr(self, "last_result", {}),
                     "deleted_files": deleted_files,
                     "freed_space_mb": round(freed_space_mb, 2),
                     "error": str(error)[:500] if error else None})
        if error:
            raise error

    async def preview(self):
        from backend.core.log_retention import clean_owned_log
        return await asyncio.to_thread(clean_owned_log, self.log_dir, self.retention_hours, dry_run=True)

    async def cleanup_old_logs(self) -> Tuple[int, float]:
        """Keep the existing tuple API; expose full outcomes through last_result."""
        from backend.core.log_retention import clean_owned_log
        try:
            self.last_result = await asyncio.to_thread(clean_owned_log, self.log_dir, self.retention_hours)
        except Exception as exc:
            self.last_result = {'failures': [str(exc)], 'deleted_records': 0,
                                'deleted_files': 0, 'freed_space_mb': 0.0}
            raise
        if self.last_result["failures"]:
            raise RuntimeError("; ".join(self.last_result["failures"][:3]))
        return self.last_result["deleted_files"], self.last_result["freed_space_mb"]

    
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

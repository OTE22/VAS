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
        return int(getattr(settings, 'LOGS_LIFE_TIME_HOURS', 48))
    
    async def start(self):
        """Start periodic log cleanup"""
        self._cleanup_task = asyncio.create_task(self._periodic_cleanup())
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
    
    async def _periodic_cleanup(self):
        """Run cleanup periodically"""
        # Run first cleanup after 10 minutes (instead of 1 hour)
        await asyncio.sleep(600)
        
        while True:
            try:
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
                
                # Run cleanup every 6 hours (subtract 60 seconds for notification lead time)
                await asyncio.sleep((6 * 3600) - 60)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Log cleanup error: {e}", exc_info=True)
                await asyncio.sleep(3600)  # Retry in 1 hour on error
    
    async def cleanup_old_logs(self) -> Tuple[int, float]:
        """
        Clean up old log entries from log files.
        Removes log lines older than retention_hours.
        
        Returns:
            Tuple of (deleted_lines_count, freed_space_mb)
        """
        if not self.log_dir.exists():
            logger.warning(f"Log directory not found: {self.log_dir}")
            return 0, 0.0
        
        # Use timezone-aware datetime to properly compare with timestamps in log files
        cutoff_time = datetime.now(timezone.utc) - timedelta(hours=self.retention_hours)
        logger.info(f"🔄 Starting log cleanup - removing entries older than {cutoff_time} ({self.retention_hours} hours)")
        
        deleted_lines = 0
        freed_space_mb = 0.0
        
        # List of log files to process
        log_files = [
            self.log_dir / "app.log",
            self.log_dir / "error.log",
            self.log_dir / "access.log",
        ]
        
        # The full read/filter/rewrite of potentially large log files is
        # BLOCKING — run it per-file in the default executor so the cleanup
        # never stalls the event loop (previously it ran inline in async code).
        loop = asyncio.get_running_loop()

        def _clean_one_file_sync(log_file):
            file_deleted = 0
            file_freed_mb = 0.0
            try:
                file_size_before = log_file.stat().st_size
                with open(log_file, 'r', encoding='utf-8', errors='ignore') as f:
                    lines = f.readlines()
                if not lines:
                    return 0, 0.0

                kept_lines = []
                lines_without_timestamp = 0
                for line in lines:
                    line_time = self._extract_timestamp_from_line(line)
                    if line_time:
                        if line_time >= cutoff_time:
                            kept_lines.append(line)
                        else:
                            file_deleted += 1
                    else:
                        # If we can't parse timestamp, keep the line (safer)
                        kept_lines.append(line)
                        lines_without_timestamp += 1

                if len(kept_lines) < len(lines):
                    with open(log_file, 'w', encoding='utf-8', errors='ignore') as f:
                        f.writelines(kept_lines)
                    file_size_after = log_file.stat().st_size
                    file_freed_mb = (file_size_before - file_size_after) / (1024 * 1024)
                    logger.info(
                        f"✅ Cleaned {log_file.name}: removed {len(lines) - len(kept_lines)} old lines, "
                        f"kept {len(kept_lines)} lines ({lines_without_timestamp} without timestamps kept), "
                        f"freed {file_freed_mb:.2f} MB"
                    )
            except PermissionError as e:
                logger.error(f"Permission denied cleaning log file {log_file}: {e}")
            except Exception as e:
                logger.error(f"Error cleaning log file {log_file}: {e}", exc_info=True)
            return file_deleted, file_freed_mb

        for log_file in log_files:
            if not log_file.exists():
                continue
            file_deleted, file_freed = await loop.run_in_executor(None, _clean_one_file_sync, log_file)
            deleted_lines += file_deleted
            freed_space_mb += file_freed
        
        if deleted_lines > 0:
            logger.info(
                f"✅ Log cleanup completed: "
                f"removed {deleted_lines} old log lines, "
                f"freed {freed_space_mb:.2f} MB"
            )
        else:
            logger.info(
                f"ℹ️ Log cleanup completed: no old logs to remove "
                f"(retention period: {self.retention_hours} hours, cutoff: {cutoff_time})"
            )
        
        return deleted_lines, freed_space_mb
    
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


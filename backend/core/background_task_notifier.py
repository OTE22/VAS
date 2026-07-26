"""
Background Task Notification Service
====================================
Sends real-time notifications to admins about background tasks starting.
Production-scale implementation with WebSocket broadcasting.
"""

import os
import sys
import asyncio
import logging
from datetime import datetime, timedelta
from typing import Optional, Dict, Any
from enum import Enum

# Add parent directory to path
parent_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if parent_dir not in sys.path:
    sys.path.insert(0, parent_dir)

from config import settings

logger = logging.getLogger(__name__)


class TaskType(str, Enum):
    """Types of background tasks"""
    LOG_CLEANUP = "log_cleanup"
    DATA_RETENTION = "data_retention"
    IDENTITY_CLUSTERING = "identity_clustering"
    FAISS_REPAIR = "faiss_repair"
    IDENTITY_RETENTION = "identity_retention"
    LIVE_ALERT_CLEANUP = "live_alert_cleanup"
    WATCHLIST_CLEANUP = "watchlist_cleanup"
    FACE_TRACKER_CLEANUP = "face_tracker_cleanup"
    BATCH_WRITER_FLUSH = "batch_writer_flush"
    RELATIONSHIP_CALCULATION = "relationship_calculation"


class BackgroundTaskNotifier:
    """
    Production-scale background task notification service.
    Sends WebSocket notifications to admins before tasks start.
    """
    
    def __init__(self, enabled: bool = True, notification_lead_time_seconds: int = 60):
        """
        Initialize notifier.
        
        Args:
            enabled: Enable/disable notifications (default: True)
            notification_lead_time_seconds: Seconds before task start to send notification (default: 60 = 1 minute)
        """
        self.enabled = enabled
        self.notification_lead_time = notification_lead_time_seconds
        self._ws_manager = None
    
    def set_ws_manager(self, ws_manager):
        """Set WebSocket manager instance"""
        self._ws_manager = ws_manager
    
    async def notify_task_starting(
        self,
        task_type: TaskType,
        task_name: str,
        description: str,
        estimated_duration: Optional[str] = None,
        scheduled_time: Optional[datetime] = None,
        notify_all_users: bool = False
    ):
        """
        Send notification that a background task is about to start.
        
        Args:
            task_type: Type of task (TaskType enum)
            task_name: Human-readable task name
            description: What the task does
            estimated_duration: Estimated duration (e.g., "2-5 minutes")
            scheduled_time: When the task will start (default: now + lead_time)
        """
        if not self.enabled:
            return
        
        if not self._ws_manager:
            logger.warning("[TASK_NOTIFIER] WebSocket manager not set, skipping notification")
            return
        
        # Calculate when task will start
        if scheduled_time is None:
            scheduled_time = datetime.utcnow() + timedelta(seconds=self.notification_lead_time)
        
        # Format scheduled time
        scheduled_str = scheduled_time.strftime("%Y-%m-%d %H:%M:%S UTC")
        
        # Create notification message
        notification = {
            "type": "background_task_notification",
            "data": {
                "task_type": task_type.value,
                "task_name": task_name,
                "description": description,
                "scheduled_time": scheduled_str,
                "scheduled_timestamp": scheduled_time.isoformat(),
                "estimated_duration": estimated_duration or "Varies",
                "notification_sent_at": datetime.utcnow().isoformat(),
                "starts_in_seconds": self.notification_lead_time
            }
        }
        
        try:
            # Store in task history
            try:
                from backend.core.task_history import task_history_manager
                await task_history_manager.record_task_scheduled(
                    task_type=task_type.value,
                    task_name=task_name,
                    description=description,
                    scheduled_time=scheduled_time,
                    notify_all_users=notify_all_users
                )
            except Exception as e:
                logger.debug(f"[TASK_NOTIFIER] Failed to record task in history: {e}")
            
            # Broadcast to all users if notify_all_users is True, otherwise admin-only
            await self._ws_manager.broadcast(notification, admin_only=not notify_all_users)
            logger.info(
                f"[TASK_NOTIFIER] ✅ Sent notification: {task_name} starting in {self.notification_lead_time}s "
                f"({scheduled_str})"
            )
        except Exception as e:
            logger.error(f"[TASK_NOTIFIER] ❌ Failed to send notification: {e}", exc_info=True)
    
    async def notify_task_completed(
        self,
        task_type: TaskType,
        task_name: str,
        success: bool = True,
        duration_seconds: Optional[float] = None,
        details: Optional[Dict[str, Any]] = None,
        notify_all_users: bool = False
    ):
        """
        Send notification that a background task has completed.
        
        Args:
            task_type: Type of task
            task_name: Human-readable task name
            success: Whether task completed successfully
            duration_seconds: How long the task took
            details: Additional details about the task results
        """
        if not self.enabled:
            return
        
        if not self._ws_manager:
            return
        
        duration_str = f"{duration_seconds:.1f} seconds" if duration_seconds else "Unknown"
        
        notification = {
            "type": "background_task_completed",
            "data": {
                "task_type": task_type.value,
                "task_name": task_name,
                "success": success,
                "duration": duration_str,
                "completed_at": datetime.utcnow().isoformat(),
                "details": details or {}
            }
        }
        
        try:
            # Store in task history
            try:
                from backend.core.task_history import task_history_manager
                await task_history_manager.record_task_completed(
                    task_type=task_type.value,
                    task_name=task_name,
                    success=success,
                    duration_seconds=duration_seconds,
                    details=details,
                    notify_all_users=notify_all_users
                )
            except Exception as e:
                logger.debug(f"[TASK_NOTIFIER] Failed to record task completion in history: {e}")
            
            # Broadcast to all users if notify_all_users is True, otherwise admin-only
            await self._ws_manager.broadcast(notification, admin_only=not notify_all_users)
            logger.debug(f"[TASK_NOTIFIER] Sent completion notification: {task_name}")
        except Exception as e:
            logger.error(f"[TASK_NOTIFIER] Failed to send completion notification: {e}")


# Global instance
background_task_notifier = BackgroundTaskNotifier(
    enabled=getattr(settings, 'BACKGROUND_TASK_NOTIFICATIONS_ENABLED', True),
    notification_lead_time_seconds=getattr(settings, 'BACKGROUND_TASK_NOTIFICATION_LEAD_TIME_SECONDS', 60)
)


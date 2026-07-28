"""
System Metrics Collector
========================
Collects and saves system metrics to database periodically.
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
from db_connection import db_manager
from db_models import Pipeline, Detection, Face, SystemMetrics
from sqlalchemy import select, func
from backend.core.processing_queue import processing_queue

# System metrics imports
try:
    import psutil
    PSUTIL_AVAILABLE = True
except ImportError:
    PSUTIL_AVAILABLE = False

logger = logging.getLogger(__name__)


class SystemMetricsCollector:
    """Collects and saves system metrics to database periodically"""

    def __init__(self, collection_interval: int = 60):
        """
        Args:
            collection_interval: Interval in seconds between metric collections (default: 60s)
        """
        self.collection_interval = collection_interval
        self._collector_task = None

    async def start(self):
        """Start periodic metrics collection"""
        if self._collector_task and not self._collector_task.done():
            logger.warning("System metrics collector already running; ignoring duplicate start()")
            return
        if PSUTIL_AVAILABLE:
            # Prime the non-blocking CPU sampler: with interval=None the FIRST
            # call always returns 0.0; priming here makes cycle 1 meaningful.
            try:
                psutil.cpu_percent(interval=None)
            except Exception:
                pass
        from backend.core.service_supervisor import supervised_loop
        self._collector_task = asyncio.create_task(
            supervised_loop(
                "system_metrics",
                self.collection_interval,
                self.collect_and_save_metrics,
                initial_delay=10,
                error_backoff_base=self.collection_interval,
            ),
            name="system_metrics",
        )
        logger.info(f"System metrics collector started (interval: {self.collection_interval}s)")

    async def stop(self):
        """Stop metrics collection task"""
        if self._collector_task:
            self._collector_task.cancel()
            try:
                await self._collector_task
            except asyncio.CancelledError:
                pass
        logger.info("System metrics collector stopped")

    async def collect_and_save_metrics(self):
        """Collect current metrics and save to database"""
        try:
            # Get queue stats
            queue_stats = await processing_queue.get_stats()

            # Use proper session for read queries
            async with db_manager.get_session() as db:
                pipelines_result = await db.execute(
                    select(func.count(Pipeline.id)).where(Pipeline.is_active == 1)
                )
                active_pipelines_count = pipelines_result.scalar() or 0

                faces_result = await db.execute(
                    select(func.count(Face.id))
                )
                total_faces_count = faces_result.scalar() or 0

                # Get average processing time (from recent detections)
                avg_time_result = await db.execute(
                    select(func.avg(Detection.processing_time_ms))
                    .where(Detection.timestamp >= datetime.utcnow() - timedelta(seconds=self.collection_interval))
                )
                avg_processing_time = avg_time_result.scalar()

            # Get system resource usage (CPU, Memory) if psutil available
            if PSUTIL_AVAILABLE:
                try:
                    cpu_percent = psutil.cpu_percent(interval=None)
                    memory = psutil.virtual_memory()
                    memory_percent = memory.percent
                    disk_usage_gb = psutil.disk_usage(settings.STORAGE_DIR).used / (1024**3) if os.path.exists(settings.STORAGE_DIR) else 0
                except Exception as e:
                    logger.error(f"System metrics collection error: {e}")
                    cpu_percent = None
                    memory_percent = None
                    disk_usage_gb = None
            else:
                cpu_percent = None
                memory_percent = None
                disk_usage_gb = None

            # Export process RSS (host-wide percent cannot reveal THIS
            # process leaking). /proc read because psutil is not installed
            # in the production container.
            try:
                from backend.core import metrics as _m
                if getattr(_m, "metrics_process_rss", None):
                    with open("/proc/self/status", encoding="ascii") as _f:
                        for _line in _f:
                            if _line.startswith("VmRSS"):
                                _m.metrics_process_rss.set(int(_line.split()[1]) * 1024)
                                break
            except (OSError, Exception):
                pass

            # Create metrics record in separate transaction
            async with db_manager.get_session() as db:
                # Create metrics record
                metrics_record = SystemMetrics(
                    timestamp=datetime.utcnow(),
                    queue_size=queue_stats["queue_size"],
                    processing_count=queue_stats["processing"],
                    total_received=queue_stats["total_received"],
                    total_processed=queue_stats["total_processed"],
                    total_skipped=queue_stats["total_skipped"],
                    avg_processing_time_ms=avg_processing_time,
                    active_pipelines=active_pipelines_count,
                    total_faces_detected=total_faces_count,
                    cpu_percent=cpu_percent,
                    memory_percent=memory_percent,
                    disk_usage_gb=disk_usage_gb,
                )

                db.add(metrics_record)
                await db.commit()

                logger.debug(
                    f"[METRICS] Saved: queue={queue_stats['queue_size']}, "
                    f"processing={queue_stats['processing']}, "
                    f"pipelines={active_pipelines_count}, "
                    f"faces={total_faces_count}"
                )

        except Exception as e:
            # Re-raise: the supervised loop owns failure accounting/backoff.
            # Swallowing here made every failure invisible.
            logger.error(f"Failed to collect/save metrics: {e}")
            raise


metrics_collector = SystemMetricsCollector(collection_interval=60)  # Collect every 60 seconds


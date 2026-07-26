"""
Processing Queue
==================
"""

import os
import sys
import asyncio
import logging
import time
from typing import Dict, Optional, Any, Set, List, Tuple
from collections import defaultdict, deque
from enum import Enum

# Add parent directory to path
parent_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if parent_dir not in sys.path:
    sys.path.insert(0, parent_dir)

from config import settings
from backend.config import *
from backend.core.metrics import *

logger = logging.getLogger(__name__)


class ProcessingQueue:
    """High-performance async queue with concurrency control and pipeline batching"""

    def __init__(self, max_size: int = 1000, max_concurrent: int = 100):
        self.max_size = max_size
        # Queue holds BATCH items; the real per-image bound is enforced in add()
        # via pending_items, so maxsize here is just a hard safety net.
        self.queue = asyncio.Queue(maxsize=max_size)
        self.semaphore = asyncio.Semaphore(max_concurrent)

        # Honest backpressure accounting: images enqueued (inside batch items)
        # but not yet dequeued by a worker, plus images still buffered in
        # pipeline_batches awaiting flush.
        self.pending_items = 0

        # Pipeline-specific batching for efficiency
        self.pipeline_batches: Dict[str, List[dict]] = defaultdict(list)
        self.batch_size = getattr(settings, 'PIPELINE_BATCH_SIZE', 5)
        self.batch_timeout = 0.5  # Flush batches after 0.5 seconds even if not full
        self._batch_lock = asyncio.Lock()
        self._batch_timestamps: Dict[str, float] = {}  # Track when batch started

        # Stats
        self.total_received = 0
        self.total_processed = 0
        self.total_skipped = 0
        self.processing_count = 0

        self._lock = asyncio.Lock()

    async def add(self, item: dict) -> bool:
        """Add item to queue with pipeline batching.

        HONEST BACKPRESSURE: the per-image bound is enforced HERE, before the
        item is buffered — a False return means the caller (webhook) should
        reject with 503 instead of silently dropping the frame later at flush.
        """
        pipeline_id = item.get('pipeline_id', 'unknown')
        current_time = time.time()

        async with self._batch_lock:
            # Enforce bound BEFORE accepting the item (counts queued + buffered)
            if self.pending_items >= self.max_size:
                async with self._lock:
                    self.total_skipped += 1
                logger.warning(
                    f"Queue full ({self.pending_items}/{self.max_size} pending)! "
                    f"Rejected item from pipeline: {pipeline_id}"
                )
                return False

            async with self._lock:
                self.total_received += 1

            # Initialize batch timestamp if this is first item
            if pipeline_id not in self._batch_timestamps:
                self._batch_timestamps[pipeline_id] = current_time

            self.pipeline_batches[pipeline_id].append(item)
            self.pending_items += 1
            batch_age = current_time - self._batch_timestamps[pipeline_id]

            # Flush batch if full OR if timeout reached (prevents delays)
            should_flush = (
                len(self.pipeline_batches[pipeline_id]) >= self.batch_size or
                batch_age >= self.batch_timeout
            )

            if should_flush:
                batch = self.pipeline_batches[pipeline_id]
                self.pipeline_batches[pipeline_id] = []
                if pipeline_id in self._batch_timestamps:
                    del self._batch_timestamps[pipeline_id]

                try:
                    # Add batch as single item (will be processed together)
                    batch_item = {
                        "pipeline_id": pipeline_id,
                        "batch": batch,
                        "is_batch": True,
                        "timestamp": current_time,
                    }
                    self.queue.put_nowait(batch_item)
                    metrics_queue_size.set(self.pending_items)
                    return True
                except asyncio.QueueFull:
                    # Should not happen (pending_items bound is stricter), but stay safe
                    self.pending_items -= len(batch)
                    async with self._lock:
                        self.total_skipped += len(batch)
                    logger.warning(f"Queue full! Dropped batch of {len(batch)} items from pipeline: {pipeline_id}")
                    return False

        # Item accepted into a batch; will be flushed when full or on timeout
        return True
    
    async def flush_batches(self):
        """Flush any remaining batched items (called periodically)"""
        current_time = time.time()
        async with self._batch_lock:
            pipelines_to_flush = []
            
            # Check for timeout batches
            for pipeline_id, batch in list(self.pipeline_batches.items()):
                if batch:
                    batch_start = self._batch_timestamps.get(pipeline_id, current_time)
                    if current_time - batch_start >= self.batch_timeout:
                        pipelines_to_flush.append(pipeline_id)
            
            # Flush timeout batches
            for pipeline_id in pipelines_to_flush:
                batch = self.pipeline_batches[pipeline_id]
                if batch:
                    try:
                        batch_item = {
                            "pipeline_id": pipeline_id,
                            "batch": batch,
                            "is_batch": True,
                            "timestamp": current_time,
                        }
                        self.queue.put_nowait(batch_item)
                        self.pipeline_batches[pipeline_id] = []
                        if pipeline_id in self._batch_timestamps:
                            del self._batch_timestamps[pipeline_id]
                        metrics_queue_size.set(self.pending_items)
                    except asyncio.QueueFull:
                        self.pipeline_batches[pipeline_id] = []
                        if pipeline_id in self._batch_timestamps:
                            del self._batch_timestamps[pipeline_id]
                        self.pending_items -= len(batch)
                        async with self._lock:
                            self.total_skipped += len(batch)
                        logger.warning(f"Queue full! Dropped batch flush of {len(batch)} items from pipeline: {pipeline_id}")

    async def get(self) -> dict:
        """Get item from queue (decrements the pending-image count)"""
        item = await self.queue.get()
        count = len(item.get("batch", [])) if item.get("is_batch") else 1
        async with self._batch_lock:
            self.pending_items = max(0, self.pending_items - count)
            metrics_queue_size.set(self.pending_items)
        return item

    async def get_stats(self) -> dict:
        """Get queue statistics"""
        async with self._lock:
            return {
                "queue_size": self.pending_items,
                "processing": self.processing_count,
                "total_received": self.total_received,
                "total_skipped": self.total_skipped,
                "total_processed": self.total_processed,
                "max_size": self.max_size,
            }


processing_queue = ProcessingQueue(
    max_size=getattr(settings, 'MAX_QUEUE_SIZE', 1000),
    max_concurrent=getattr(settings, 'MAX_CONCURRENT_REQUESTS', 100)
)

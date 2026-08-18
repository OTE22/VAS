"""
Queue Worker Service
====================
Background worker to process queued requests.
"""

import os
import sys
import asyncio
import logging
import time

# Add parent directory to path
parent_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if parent_dir not in sys.path:
    sys.path.insert(0, parent_dir)

import base64

from backend.core import processing_queue
from backend.core.metrics import metrics_worker_active, metrics_processing_time
from backend.services.image_processing import process_image_async, INFERENCE_POOL
from backend.routes.utils import validate_image_content
from config import settings

logger = logging.getLogger(__name__)


def _decode_validate_sync(item: dict):
    """Decode + validate a queued frame (runs in the inference thread pool).

    The webhook enqueues the raw b64 string so its handler never blocks the
    event loop; all CPU work (b64decode, magic-byte validation, optional debug
    save) happens here, off-loop. Returns decoded bytes or None if invalid.
    """
    b64 = item.get("image_b64")
    if not b64:
        return item.get("image_bytes")  # legacy items queued before restart

    try:
        image_bytes = base64.b64decode(b64)
    except Exception as e:
        logger.warning(f"[WORKER] Image b64 decode error (request {item.get('request_id')}): {e}")
        return None

    if not validate_image_content(image_bytes):
        logger.warning(
            f"[WORKER] Invalid image content in request {item.get('request_id')}, "
            f"image {item.get('image_index')}"
        )
        return None

    if settings.SAVE_WEBHOOK_IMAGES:
        try:
            from backend.routes.webhook import _save_webhook_image
            _save_webhook_image(
                image_bytes,
                item.get("pipeline_id", "unknown"),
                item.get("request_id", "unknown"),
                item.get("image_index", 0),
            )
        except Exception as e:
            logger.warning(f"[WORKER] Failed to save webhook image: {e}")

    return image_bytes


async def _process_queued_item(item: dict, worker_id: int):
    """Decode (off-loop) then run the recognition pipeline for one queued frame."""
    loop = asyncio.get_running_loop()
    image_bytes = await loop.run_in_executor(INFERENCE_POOL, _decode_validate_sync, item)
    if image_bytes is None:
        return None

    logger.info("[WORKER] pipeline_id=%s location_name=%r", item["pipeline_id"], item.get("location_name"))
    return await process_image_async(
        image_bytes,
        item["pipeline_id"],
        item.get("predictions", []),
        use_batch_write=True,
        send_realtime_updates=True,
        worker_id=worker_id,
        location_name=item.get("location_name"),
    )


async def queue_worker(worker_id: int):
    """Background worker to process queued requests (supports batching)"""
    logger.info(f"[WORKER-{worker_id}] Started")

    while True:
        try:
            # Get item from queue
            item = await processing_queue.get()

            # Acquire semaphore for concurrency control
            async with processing_queue.semaphore:
                processing_queue.processing_count += 1
                metrics_worker_active.labels(worker_id=str(worker_id)).set(1)

                try:
                    # Handle batched items (multiple images from same pipeline)
                    if item.get("is_batch", False):
                        batch = item.get("batch", [])
                        pipeline_id = item.get("pipeline_id", "unknown")
                        
                        logger.debug(f"[WORKER-{worker_id}] Processing batch of {len(batch)} images from pipeline {pipeline_id}")
                        
                        # Process batch concurrently (decode + inference happen in
                        # the inference pool, bounded by its semaphores)
                        tasks = [_process_queued_item(batch_item, worker_id) for batch_item in batch]
                        results = await asyncio.gather(*tasks, return_exceptions=True)
                        
                        successful = sum(1 for r in results if r and not isinstance(r, Exception))
                        processing_queue.total_processed += successful
                        processing_queue.total_skipped += len(batch) - successful
                        
                        logger.info(f"[WORKER-{worker_id}] ✅ Batch completed: {pipeline_id} ({successful}/{len(batch)} successful)")
                    else:
                        # Single item processing
                        result = await _process_queued_item(item, worker_id)

                        if result:
                            processing_queue.total_processed += 1
                            logger.debug(f"[WORKER-{worker_id}] ✅ Completed: {result['pipeline_id']} ({result.get('new_faces_count', 0)} new faces)")
                        else:
                            processing_queue.total_skipped += 1

                except Exception as e:
                    logger.error(f"[WORKER-{worker_id}] Error: {e}", exc_info=True)
                    if item.get("is_batch", False):
                        processing_queue.total_skipped += len(item.get("batch", []))
                    else:
                        processing_queue.total_skipped += 1

                finally:
                    processing_queue.processing_count -= 1
                    metrics_worker_active.labels(worker_id=str(worker_id)).set(0)
                    processing_queue.queue.task_done()
                    metrics_processing_time.observe(time.time() - item.get("timestamp", time.time()))

        except Exception as e:
            logger.error(f"[WORKER-{worker_id}] Fatal error: {e}", exc_info=True)
            await asyncio.sleep(1)


"""
Cache Metrics Service
=====================
Background task to update cache performance metrics.
"""

import os
import sys
import asyncio
import logging
import random

# Add parent directory to path
parent_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if parent_dir not in sys.path:
    sys.path.insert(0, parent_dir)

from backend.core import production_cache_manager, face_recognition_cache
from backend.core.metrics import (
    metrics_cache_hit_rate, metrics_cache_size,
    metrics_cache_write_behind, metrics_cache_circuit_state
)

logger = logging.getLogger(__name__)


async def update_cache_metrics():
    """
    Background task to update cache performance metrics
    Runs every 30 seconds
    """
    while True:
        try:
            await asyncio.sleep(30)  # Update every 30 seconds

            if not production_cache_manager._enabled:
                continue

            # Get cache statistics
            cache_stats = await production_cache_manager.get_stats()
            face_cache_stats = await face_recognition_cache.get_performance_stats() if hasattr(face_recognition_cache, 'get_performance_stats') else {}

            # Calculate hit rates
            total_hits = cache_stats['hits']['local'] + cache_stats['hits']['redis']
            total_requests = total_hits + cache_stats['misses']

            if total_requests > 0:
                local_hit_rate = (cache_stats['hits']['local'] / total_requests) * 100
                redis_hit_rate = (cache_stats['hits']['redis'] / total_requests) * 100
                overall_hit_rate = (total_hits / total_requests) * 100
            else:
                local_hit_rate = redis_hit_rate = overall_hit_rate = 0

            # Update Prometheus metrics
            metrics_cache_hit_rate.labels(type='local').set(local_hit_rate)
            metrics_cache_hit_rate.labels(type='redis').set(redis_hit_rate)
            metrics_cache_hit_rate.labels(type='overall').set(overall_hit_rate)
            metrics_cache_size.labels(type='local').set(cache_stats['local_cache_size'])
            metrics_cache_write_behind.inc(cache_stats.get('write_behind_queue_size', 0))

            # Circuit breaker state
            circuit_state_map = {
                "closed": 0,
                "open": 1,
                "half_open": 2
            }
            cb_state = cache_stats['circuit_breaker']['state']
            metrics_cache_circuit_state.set(circuit_state_map.get(cb_state, 3))

            # Log cache performance periodically
            if random.random() < 0.1:  # 10% chance to log
                logger.debug(
                    f"[CACHE METRICS] Hits: local={cache_stats['hits']['local']}, "
                    f"redis={cache_stats['hits']['redis']}, "
                    f"misses={cache_stats['misses']}, "
                    f"hit_rate={overall_hit_rate:.1f}%"
                )

        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Cache metrics update error: {e}")
            await asyncio.sleep(60)  # Wait longer on error


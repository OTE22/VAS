"""
Production Cache Manager
========================
Enterprise-grade cache manager with multi-layer caching.
"""

import os
import sys
import asyncio
import logging
import time
import hashlib
import json
import random
from collections import deque
import numpy as np

# Add parent directory to path
parent_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if parent_dir not in sys.path:
    sys.path.insert(0, parent_dir)

from backend.core.circuit_breaker import CircuitBreaker
from backend.core.metrics import metrics_cache_hits, metrics_cache_misses

logger = logging.getLogger(__name__)


class ProductionCacheManager:
    """
    Enterprise-grade cache manager with:
    - Multi-layer caching (in-memory + Redis)
    - Cache warming
    - Write-behind updates
    - Circuit breaker pattern
    - Graceful degradation
    """

    def __init__(self, redis_client=None, local_cache_size: int = 10000):
        self.redis_client = redis_client
        self._enabled = redis_client is not None

        # Local LRU cache for hot items (reduces Redis load)
        self.local_cache = {}
        self.local_cache_order = deque(maxlen=local_cache_size)
        self.local_cache_size = local_cache_size

        # Circuit breaker for Redis
        self.circuit_breaker = CircuitBreaker(
            failure_threshold=5,
            timeout=30,
            half_open_timeout=60
        )

        # Statistics
        self.stats = {
            'hits': {'local': 0, 'redis': 0},
            'misses': 0,
            'errors': 0,
            'write_behind': 0
        }

        # Cache version for schema migrations
        self.cache_version = "v2"

        # Background tasks
        self._write_behind_queue = asyncio.Queue(maxsize=10000)
        self._write_behind_task = None
        self._warming_task = None
        
        # Lock for thread-safe stats access
        self._lock = asyncio.Lock()

        logger.info(f"ProductionCacheManager initialized (local_cache: {local_cache_size})")

    async def start(self):
        """Start background workers"""
        if self._enabled:
            self._write_behind_task = asyncio.create_task(self._write_behind_worker())
            self._warming_task = asyncio.create_task(self._cache_warming_worker())
            logger.info("ProductionCacheManager background workers started")

    async def stop(self):
        """Stop background workers gracefully"""
        if self._write_behind_task:
            self._write_behind_task.cancel()
        if self._warming_task:
            self._warming_task.cancel()
        logger.info("ProductionCacheManager stopped")

    # ==================== CORE CACHE METHODS ====================

    async def get_with_fallback(self, key: str, fallback_func=None, fallback_args=None, ttl: int = 300):
        """
        Production-grade cache get with fallback
        Implements: Local cache → Redis → Fallback function → Cache population
        """
        # 1. Check local cache (fastest)
        if key in self.local_cache:
            value, expiry = self.local_cache[key]
            if expiry > time.time():
                self.stats['hits']['local'] += 1
                metrics_cache_hits.inc()
                return value

        # 2. Check Redis (if enabled and circuit breaker allows)
        redis_value = None
        if self._enabled and await self.circuit_breaker.can_execute():
            try:
                redis_value = await self._safe_redis_get(key)
                if redis_value:
                    # Parse Redis value (includes metadata)
                    value, metadata = self._deserialize(redis_value)
                    
                    # If value is a tuple (face match), store it properly in local cache
                    if isinstance(value, tuple):
                        self._set_local_cache(key, value, ttl)
                    else:
                        # Store in local cache as-is
                        self._set_local_cache(key, value, ttl)

                    self.stats['hits']['redis'] += 1
                    metrics_cache_hits.inc()
                    logger.debug(f"[CACHE] Redis hit for key: {key[:50]}... (type: {type(value)})")
                    return value
            except Exception as e:
                self.stats['errors'] += 1
                logger.warning(f"[CACHE] Redis get error (falling back): {e}")
                await self.circuit_breaker.call_failed()

        # 3. Use fallback function if provided
        if fallback_func and callable(fallback_func):
            try:
                # Execute fallback (e.g., database query)
                if fallback_args:
                    result = await fallback_func(*fallback_args) if asyncio.iscoroutinefunction(fallback_func) else fallback_func(*fallback_args)
                else:
                    result = await fallback_func() if asyncio.iscoroutinefunction(fallback_func) else fallback_func()

                # 4. Populate cache asynchronously (write-behind)
                if result is not None:
                    asyncio.create_task(self._async_set(key, result, ttl))
                    logger.debug(f"[CACHE] Cache miss - populating cache for key: {key[:50]}... (result type: {type(result)})")
                else:
                    logger.debug(f"[CACHE] Cache miss - no result to cache for key: {key[:50]}...")

                self.stats['misses'] += 1
                metrics_cache_misses.inc()
                return result
            except Exception as e:
                logger.error(f"[CACHE] Fallback function error: {e}")

        # 5. Complete cache miss
        self.stats['misses'] += 1
        metrics_cache_misses.inc()
        return None

    async def set_with_ttl_jitter(self, key: str, value, base_ttl: int = 300, jitter_percent: float = 0.1):
        """
        Set cache with TTL jitter to prevent cache stampedes
        """
        # Add jitter to prevent all keys expiring at once
        jitter = base_ttl * jitter_percent
        actual_ttl = base_ttl + random.uniform(-jitter, jitter)

        await self._async_set(key, value, int(actual_ttl))

    # `get_face_match` used to live here — a specialized cache in front of the
    # legacy display-name FaceDatabase's search. It went with that chain: the
    # store it cached was write-never under pgvector, so every cached entry was
    # "no match".

    async def cache_pipeline_stats(self, pipeline_id: str, stats: dict, ttl: int = 60):
        """
        Cache pipeline statistics with short TTL
        """
        if not self._enabled:
            return

        cache_key = f"{self.cache_version}:stats:pipeline:{pipeline_id}"
        await self._async_set(cache_key, stats, ttl)

    async def get_pipeline_stats(self, pipeline_id: str, fallback_func=None):
        """
        Get cached pipeline statistics
        """
        if not self._enabled:
            return await fallback_func() if fallback_func else None

        cache_key = f"{self.cache_version}:stats:pipeline:{pipeline_id}"
        return await self.get_with_fallback(
            key=cache_key,
            fallback_func=fallback_func,
            ttl=60
        )

    # ==================== PRIVATE METHODS ====================

    def _generate_embedding_hash(self, embedding: np.ndarray) -> str:
        """Generate fast hash for embedding"""
        # Use first 8 bytes for speed (collisions acceptable for cache)
        return hashlib.md5(embedding.tobytes()).hexdigest()[:16]

    def _set_local_cache(self, key: str, value, ttl: int):
        """Set value in local LRU cache"""
        if len(self.local_cache) >= self.local_cache_size:
            # Remove oldest
            oldest = self.local_cache_order.popleft()
            if oldest in self.local_cache:
                del self.local_cache[oldest]

        expiry = time.time() + ttl
        self.local_cache[key] = (value, expiry)
        self.local_cache_order.append(key)

    async def _safe_redis_get(self, key: str):
        """Safe Redis get with timeout"""
        try:
            return await asyncio.wait_for(self.redis_client.get(key), timeout=1.0)
        except asyncio.TimeoutError:
            logger.warning(f"[CACHE] Redis get timeout for key: {key}")
            return None
        except Exception as e:
            raise e

    async def _async_set(self, key: str, value, ttl: int):
        """
        Asynchronous cache set (write-behind pattern)
        """
        if not self._enabled:
            return

        try:
            # Add to write-behind queue
            self._write_behind_queue.put_nowait((key, value, ttl))
            self.stats['write_behind'] += 1
        except asyncio.QueueFull:
            # Queue full, set directly (blocking)
            await self._direct_set(key, value, ttl)

    async def _direct_set(self, key: str, value, ttl: int):
        """Direct Redis set"""
        try:
            serialized = self._serialize(value)
            await self.redis_client.setex(key, ttl, serialized)

            # Also update local cache
            self._set_local_cache(key, value, ttl)
            
            logger.debug(f"[CACHE] ✅ Cached key: {key[:50]}... (TTL: {ttl}s, value type: {type(value)})")
        except Exception as e:
            logger.warning(f"[CACHE] Direct set error: {e}", exc_info=True)

    def _serialize(self, value) -> str:
        """Serialize value with metadata"""
        if isinstance(value, tuple) and len(value) == 2:
            # Face match result: (name, similarity)
            name, similarity = value
            # Ensure name is string and similarity is float
            name_str = str(name) if name is not None else "Unknown"
            similarity_float = float(similarity) if similarity is not None else 0.0
            return f"face:{name_str}:{similarity_float:.6f}"
        elif isinstance(value, dict):
            # JSON for complex objects
            return json.dumps({
                'data': value,
                'version': self.cache_version,
                'timestamp': time.time()
            })
        else:
            # Default string serialization
            return str(value)

    def _deserialize(self, serialized: str):
        """Deserialize cached value"""
        if serialized.startswith('face:'):
            # Face match result: "face:name:similarity"
            parts = serialized[5:].rsplit(":", 1)  # Split from right to handle names with colons
            if len(parts) == 2:
                name, similarity_str = parts
                try:
                    return (name, float(similarity_str)), {'type': 'face_match'}
                except ValueError:
                    logger.warning(f"[CACHE] Failed to deserialize face match: {serialized}")
                    return serialized, {'type': 'face_match', 'error': 'deserialization_failed'}
            else:
                # Fallback: return as-is
                return serialized[5:], {'type': 'face_match'}
        elif serialized.startswith('{'):
            # JSON object
            data = json.loads(serialized)
            return data['data'], {'version': data.get('version'), 'timestamp': data.get('timestamp')}
        else:
            # Plain string
            return serialized, {}

    # ==================== BACKGROUND WORKERS ====================

    async def _write_behind_worker(self):
        """Background worker for write-behind cache updates"""
        logger.info("[CACHE] Write-behind worker started")

        while True:
            try:
                key, value, ttl = await self._write_behind_queue.get()

                try:
                    await self._direct_set(key, value, ttl)
                except Exception as e:
                    logger.warning(f"[CACHE] Write-behind error: {e}")

                self._write_behind_queue.task_done()

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[CACHE] Write-behind worker error: {e}")
                await asyncio.sleep(1)

    async def _cache_warming_worker(self):
        """Warm cache with frequently accessed items"""
        logger.info("[CACHE] Warming worker started")

        while True:
            try:
                # Run every 5 minutes
                await asyncio.sleep(300)

                # Here you could implement cache warming logic
                # Example: Pre-cache hot embeddings, pipeline stats, etc.

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[CACHE] Warming worker error: {e}")
                await asyncio.sleep(60)

    # ==================== MONITORING ====================

    async def get_stats(self) -> dict:
        """Get cache statistics"""
        async with self._lock:
            return {
                'enabled': self._enabled,
                'circuit_breaker': await self.circuit_breaker.get_status(),
                'hits': self.stats['hits'],
                'misses': self.stats['misses'],
                'errors': self.stats['errors'],
                'write_behind_queue_size': self._write_behind_queue.qsize() if self._write_behind_queue else 0,
                'local_cache_size': len(self.local_cache),
                'local_cache_capacity': self.local_cache_size,
                'cache_version': self.cache_version
            }


production_cache_manager = ProductionCacheManager(redis_client=None)


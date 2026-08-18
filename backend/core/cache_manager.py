"""
Redis Cache Manager
========================
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

try:
    import redis.asyncio as redis
    REDIS_AVAILABLE = True
except ImportError:
    REDIS_AVAILABLE = False


# =====================================================
class CacheManager:
    """Redis cache manager for face recognition results"""

    def __init__(self):
        self.redis_client: Optional[redis.Redis] = None
        self._enabled = CACHE_ENABLED

    async def initialize(self):
        """Initialize Redis connection"""
        if not self._enabled:
            logger.info("Cache disabled (Redis not available)")
            return

        try:
            self.redis_client = await redis.from_url(
                settings.REDIS_URL,
                encoding="utf-8",
                decode_responses=True,
                max_connections=settings.REDIS_MAX_CONNECTIONS if hasattr(settings, 'REDIS_MAX_CONNECTIONS') else 10,
                socket_keepalive=True,
                socket_connect_timeout=5,
                retry_on_timeout=True,
            )
            await self.redis_client.ping()
            logger.info("✅ Redis cache initialized")
        except Exception as e:
            logger.error(f"Redis initialization failed: {e}")
            self._enabled = False

    async def get(self, key: str) -> Optional[str]:
        """Get value from cache"""
        if not self._enabled or not self.redis_client:
            return None

        try:
            value = await self.redis_client.get(key)
            if value:
                metrics_cache_hits.inc()
            else:
                metrics_cache_misses.inc()
            return value
        except Exception as e:
            logger.error(f"Cache get error: {e}")
            return None

    async def set(self, key: str, value: str, ttl: int = None):
        """Set value in cache"""
        if not self._enabled or not self.redis_client:
            return

        try:
            ttl = ttl or settings.CACHE_TTL  # Default 5 minutes
            await self.redis_client.setex(key, ttl, value)
        except Exception as e:
            logger.error(f"Cache set error: {e}")

    async def delete(self, key: str):
        """Delete value from cache"""
        if not self._enabled or not self.redis_client:
            return

        try:
            await self.redis_client.delete(key)
        except Exception as e:
            logger.error(f"Cache delete error: {e}")

    async def invalidate_prefix(self, prefix: str, max_keys: int = 10000) -> int:
        """Delete all keys under a prefix (bounded SCAN — never KEYS *).

        Used after retention deletes rows so cached renders built from the
        deleted data (e.g. map:*) don't outlive it. Returns keys deleted.
        """
        if not self._enabled or not self.redis_client or not prefix:
            return 0
        deleted = 0
        try:
            batch = []
            async for key in self.redis_client.scan_iter(match=f"{prefix}*", count=500):
                batch.append(key)
                if len(batch) >= 500:
                    deleted += await self.redis_client.delete(*batch)
                    batch = []
                if deleted >= max_keys:
                    break
            if batch:
                deleted += await self.redis_client.delete(*batch)
            if deleted:
                logger.info(f"Cache invalidated: {deleted} key(s) under '{prefix}'")
        except Exception as e:
            logger.error(f"Cache invalidate_prefix error: {e}")
        return deleted

    async def health_check(self) -> bool:
        """Check if cache is healthy"""
        if not self._enabled:
            return False

        try:
            if self.redis_client:
                await self.redis_client.ping()
                return True
        except:
            pass
        return False

    async def close(self):
        """Close Redis connection"""
        if self.redis_client:
            await self.redis_client.close()
            logger.info("Redis connection closed")


cache_manager = CacheManager()


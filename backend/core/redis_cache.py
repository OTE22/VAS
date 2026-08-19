"""
Redis Cache Service
===================
High-performance caching service using Redis for fast page loads.
Caches initial data for dashboard and unknown faces page.
"""

import os
import sys
import json
import logging
import hashlib
from typing import Optional, Any, Dict, List
from datetime import datetime, timedelta

# Add parent directory to path
parent_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if parent_dir not in sys.path:
    sys.path.insert(0, parent_dir)

from config import settings

logger = logging.getLogger(__name__)

# Try to import Redis
try:
    import redis.asyncio as redis
    REDIS_AVAILABLE = True
except ImportError:
    REDIS_AVAILABLE = False
    redis = None
    logger.warning("[REDIS_CACHE] Redis not available - caching disabled")


class RedisCacheService:
    """
    Redis-based caching service for fast page loads.
    
    Caches:
    - Dashboard initial data (KNOWN faces)
    - Unknown faces initial data
    - User-specific pipeline data
    
    Cache key format — ONE shape, built by `_generate_cache_key`:

        cache:{cache_type}:{user_part}:{params_hash}

    where `user_part` is `user_{id}` or `all`, and `params_hash` is the first
    12 hex of an md5 over every keyword argument. Every parameter that changes
    the answer must be passed in, because nothing else distinguishes entries.

    This docstring previously described a different, richer layout
    (`dashboard:initial:{user_id}:{pipeline_hash}:{display_hours}`) that no
    code produced. The per-user invalidation globs were written against that
    fiction and therefore matched nothing — see `_user_scoped_pattern`.
    """
    
    def __init__(self):
        self.redis_client: Optional[redis.Redis] = None
        self._initialized = False
        self._enabled = False
        
    async def initialize(self) -> bool:
        """Initialize Redis connection"""
        if not REDIS_AVAILABLE:
            logger.warning("[REDIS_CACHE] Redis not available - caching disabled")
            return False
        
        if self._initialized:
            return self._enabled
        
        try:
            redis_url = settings.REDIS_URL
            cache_ttl = settings.CACHE_TTL  # Default 1 hour
            
            from backend.security.redaction import redact_url
            logger.info(f"[REDIS_CACHE] 🔗 Initializing Redis cache (URL: {redact_url(redis_url)}, TTL: {cache_ttl}s)")
            
            self.redis_client = await redis.from_url(
                redis_url,
                encoding="utf-8",
                decode_responses=False,  # We'll handle JSON encoding ourselves
                socket_keepalive=True,
                socket_connect_timeout=5,
                retry_on_timeout=True,
                max_connections=settings.REDIS_MAX_CONNECTIONS,
            )
            
            # Test connection
            await self.redis_client.ping()
            
            self._enabled = True
            self._initialized = True
            logger.info("[REDIS_CACHE] ✅ Redis cache initialized successfully")
            return True
            
        except Exception as e:
            logger.error(f"[REDIS_CACHE] ❌ Failed to initialize Redis cache: {e}", exc_info=True)
            self._enabled = False
            self._initialized = True
            return False
    
    def _generate_cache_key(self, cache_type: str, user_id: Optional[int], **kwargs) -> str:
        """Generate a cache key from parameters"""
        # Create a hash of variable parameters
        params_str = json.dumps(kwargs, sort_keys=True, default=str)
        params_hash = hashlib.md5(params_str.encode()).hexdigest()[:12]
        
        user_part = f"user_{user_id}" if user_id else "all"
        return f"cache:{cache_type}:{user_part}:{params_hash}"
    
    async def get(self, key: str) -> Optional[Any]:
        """Get value from cache"""
        if not self._enabled or not self.redis_client:
            return None
        
        try:
            cached_data = await self.redis_client.get(key)
            if cached_data:
                data = json.loads(cached_data)
                logger.debug(f"[REDIS_CACHE] ✅ Cache HIT: {key}")
                return data
            else:
                logger.debug(f"[REDIS_CACHE] ❌ Cache MISS: {key}")
                return None
        except Exception as e:
            logger.warning(f"[REDIS_CACHE] Error getting cache key {key}: {e}")
            return None
    
    async def set(self, key: str, value: Any, ttl: Optional[int] = None) -> bool:
        """Set value in cache with TTL"""
        if not self._enabled or not self.redis_client:
            return False
        
        try:
            if ttl is None:
                ttl = settings.CACHE_TTL  # Default 1 hour
            
            serialized = json.dumps(value, default=str)
            await self.redis_client.setex(key, ttl, serialized)
            logger.debug(f"[REDIS_CACHE] 💾 Cached: {key} (TTL: {ttl}s)")
            return True
        except Exception as e:
            logger.warning(f"[REDIS_CACHE] Error setting cache key {key}: {e}")
            return False
    
    async def delete(self, pattern: str) -> int:
        """Delete cache keys matching pattern (supports wildcards)"""
        if not self._enabled or not self.redis_client:
            return 0
        
        try:
            deleted = 0
            async for key in self.redis_client.scan_iter(match=pattern):
                await self.redis_client.delete(key)
                deleted += 1
            logger.info(f"[REDIS_CACHE] 🗑️  Deleted {deleted} cache keys matching pattern: {pattern}")
            return deleted
        except Exception as e:
            logger.warning(f"[REDIS_CACHE] Error deleting cache pattern {pattern}: {e}")
            return 0
    
    def _user_scoped_pattern(self, cache_type: str, user_id: Optional[int]) -> str:
        """Glob for one user's entries of a cache type, or all of them.

        Must track `_generate_cache_key` exactly. Both per-user invalidations
        used to ask for `cache:{type}:*:user_{id}:*`, which has one segment too
        many: real keys are `cache:{type}:user_{id}:{hash}`, so the glob matched
        NOTHING and a targeted invalidation silently cleared nothing at all. It
        went unnoticed because every caller passes user_id=None and takes the
        global path.

        `is not None`, not truthiness: user_id=0 would otherwise fall through
        and wipe every user's entries instead of that one's.
        """
        if user_id is not None:
            return f"cache:{cache_type}:user_{user_id}:*"
        return f"cache:{cache_type}:*"

    async def invalidate_dashboard_cache(self, user_id: Optional[int] = None) -> int:
        """Invalidate all dashboard cache entries for a user (or all users if None)"""
        return await self.delete(self._user_scoped_pattern("dashboard", user_id))

    async def invalidate_unknown_cache(self, user_id: Optional[int] = None) -> int:
        """Invalidate all unknown faces cache entries for a user (or all users if None)"""
        return await self.delete(self._user_scoped_pattern("unknown", user_id))
    
    async def invalidate_all(self) -> int:
        """Invalidate all cache entries"""
        return await self.delete("cache:*")
    
    async def get_dashboard_cache_key(self, user_id: Optional[int], pipeline_ids: Optional[List[str]], display_hours: int) -> str:
        """Generate cache key for dashboard initial data"""
        pipeline_hash = hashlib.md5(
            json.dumps(sorted(pipeline_ids) if pipeline_ids else [], sort_keys=True).encode()
        ).hexdigest()[:12]
        
        return self._generate_cache_key(
            "dashboard",
            user_id,
            pipelines=pipeline_hash,
            display_hours=display_hours
        )
    
    async def get_unknown_cache_key(self, user_id: Optional[int], page: int, page_size: int, filters: Dict) -> str:
        """Generate cache key for unknown faces initial data"""
        return self._generate_cache_key(
            "unknown",
            user_id,
            page=page,
            page_size=page_size,
            **filters
        )
    
    async def close(self):
        """Close Redis connection"""
        if self.redis_client:
            try:
                await self.redis_client.close()
                logger.info("[REDIS_CACHE] 🔌 Redis connection closed")
            except Exception as e:
                logger.warning(f"[REDIS_CACHE] Error closing Redis connection: {e}")


# Global cache service instance
redis_cache_service = RedisCacheService()



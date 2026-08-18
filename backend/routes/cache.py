"""
Cache Management Routes
=======================
Routes for cache management and monitoring.
"""

import os
import sys
import logging
from datetime import datetime
from fastapi import APIRouter, Depends, BackgroundTasks, HTTPException  # noqa: F401
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

# Add parent directory to path
parent_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if parent_dir not in sys.path:
    sys.path.insert(0, parent_dir)

from db_connection import get_db
from backend.utils.pagination import resolve_page, resolve_page_size
from db_models import Detection, User
from backend.core import production_cache_manager
from backend.core.circuit_breaker import CircuitState
from backend.auth.auth_service import require_role

logger = logging.getLogger(__name__)


# Cache administration + diagnostics expose Redis internals (server version,
# memory, key counts) and let a caller wipe cache keys. Administrator-only,
# enforced at the router so no route here can be unauthenticated. (Previously
# every /api/cache/* route was open: POST /api/cache/clear?pattern=* let any
# anonymous caller flush the whole cache.)
router = APIRouter(
    tags=["Cache"],
    dependencies=[Depends(require_role(["admin"]))],
)


@router.get("/api/cache/stats")
async def get_cache_stats():
    """Get detailed cache statistics.

    `face_cache` is retained as a literal None for response-shape stability:
    the FaceRecognitionCache it reported on cached lookups against the retired
    display-name FaceDatabase, which was write-never under pgvector — a cache
    accelerating an always-empty answer.
    """
    return {
        "cache_manager": await production_cache_manager.get_stats(),
        "face_cache": None,
        "timestamp": datetime.utcnow().isoformat()
    }


@router.post("/api/cache/warm/{pipeline_id}")
async def warm_cache_for_pipeline(pipeline_id: str, limit: int = None, db: AsyncSession = Depends(get_db)):
    """Warm cache for a specific pipeline"""
    # Bounds from configuration; this used to accept any limit a caller sent.
    limit = resolve_page_size(limit, field="limit")
    # Get recent detections
    result = await db.execute(
        select(Detection)
        .where(Detection.pipeline_id == pipeline_id)
        .order_by(Detection.timestamp.desc())
        .limit(limit)
    )
    detections = result.scalars().all()

    warmed = 0
    for detection in detections:
        # Cache detection metadata. Written directly through the production
        # cache — the FaceRecognitionCache wrapper this used to call was a
        # one-line delegation to exactly this, plus the retired legacy lookup.
        cache_key = (f"{production_cache_manager.cache_version}:detection:"
                     f"{pipeline_id}:{detection.id}")
        await production_cache_manager.set_with_ttl_jitter(
            cache_key,
            {"faces": [{"name": f.name, "similarity": f.similarity}
                       for f in detection.faces]},
            base_ttl=300,
        )
        warmed += 1

    return {
        "status": "success",
        "pipeline_id": pipeline_id,
        "detections_warmed": warmed,
        "message": f"Warmed cache for {warmed} detections"
    }


@router.post("/api/cache/clear")
async def clear_cache(background_tasks: BackgroundTasks, pattern: str = "*"):
    """Clear cache (with safety limits)"""
    if not production_cache_manager._enabled:
        raise HTTPException(status_code=400, detail="Cache not enabled")

    # Add safety: only allow clearing specific patterns
    allowed_patterns = ["stats:*", "detection:*", "face:*"]
    if pattern not in allowed_patterns and pattern != "*":
        raise HTTPException(
            status_code=400,
            detail=f"Pattern not allowed. Allowed: {allowed_patterns}"
        )

    async def safe_clear():
        try:
            keys = await production_cache_manager.redis_client.keys(pattern)
            if keys:
                await production_cache_manager.redis_client.delete(*keys)
                logger.info(f"Cleared {len(keys)} cache keys with pattern: {pattern}")
        except Exception as e:
            logger.error(f"Cache clear error: {e}")

    background_tasks.add_task(safe_clear)

    return {
        "status": "clearing_started",
        "pattern": pattern,
        "message": "Cache clear initiated in background"
    }


@router.get("/api/cache/health")
async def cache_health():
    """Detailed cache health check"""
    cache_stats = await production_cache_manager.get_stats()

    healthy = (
        production_cache_manager._enabled and
        production_cache_manager.circuit_breaker.state == CircuitState.CLOSED
    )

    return {
        "healthy": healthy,
        "enabled": production_cache_manager._enabled,
        "circuit_breaker": cache_stats['circuit_breaker'],
        "local_cache": {
            "size": cache_stats['local_cache_size'],
            "capacity": cache_stats['local_cache_capacity'],
            "usage_percent": (cache_stats['local_cache_size'] / cache_stats['local_cache_capacity'] * 100) if cache_stats['local_cache_capacity'] > 0 else 0
        },
        "performance": None,  # was the retired FaceRecognitionCache's stats
    }


@router.get("/api/cache/redis/test")
async def test_redis_connection():
    """
    Test Redis connection and functionality
    Returns detailed diagnostics about Redis status
    """
    from backend.core import cache_manager
    from config import settings
    import time
    
    diagnostics = {
        "redis_configured": False,
        "redis_available": False,
        "redis_connected": False,
        "redis_ping": False,
        "redis_url": None,
        "test_write": False,
        "test_read": False,
        "test_ttl": False,
        "redis_info": None,
        "errors": []
    }
    
    try:
        # Check configuration
        # Redacted: this diagnostics payload is returned over HTTP, and the
        # URL carries the Redis password once authentication is enabled.
        from backend.security.redaction import redact_url
        diagnostics["redis_url"] = redact_url(settings.REDIS_URL or '')
        diagnostics["redis_configured"] = diagnostics["redis_url"] is not None
        
        if not diagnostics["redis_configured"]:
            diagnostics["errors"].append("REDIS_URL not configured in settings")
            return diagnostics
        
        # Check if Redis library is available
        try:
            import redis.asyncio as redis
            diagnostics["redis_available"] = True
        except ImportError:
            diagnostics["errors"].append("redis library not installed (pip install redis)")
            return diagnostics
        
        # Check cache_manager status
        diagnostics["redis_connected"] = cache_manager._enabled and cache_manager.redis_client is not None
        
        if not diagnostics["redis_connected"]:
            diagnostics["errors"].append("Cache manager not initialized or Redis client is None")
            # Try to initialize
            try:
                await cache_manager.initialize()
                diagnostics["redis_connected"] = cache_manager._enabled and cache_manager.redis_client is not None
            except Exception as e:
                diagnostics["errors"].append(f"Failed to initialize cache manager: {str(e)}")
                return diagnostics
        
        # Test Redis ping
        try:
            if cache_manager.redis_client:
                await cache_manager.redis_client.ping()
                diagnostics["redis_ping"] = True
        except Exception as e:
            diagnostics["errors"].append(f"Redis ping failed: {str(e)}")
            return diagnostics
        
        # Test write operation
        test_key = f"test:connection:{int(time.time())}"
        test_value = "test_value_12345"
        try:
            await cache_manager.set(test_key, test_value, ttl=10)
            diagnostics["test_write"] = True
        except Exception as e:
            diagnostics["errors"].append(f"Redis write test failed: {str(e)}")
            return diagnostics
        
        # Test read operation
        try:
            read_value = await cache_manager.get(test_key)
            if read_value == test_value:
                diagnostics["test_read"] = True
            else:
                diagnostics["errors"].append(f"Redis read test failed: expected '{test_value}', got '{read_value}'")
        except Exception as e:
            diagnostics["errors"].append(f"Redis read test failed: {str(e)}")
        
        # Test TTL
        try:
            if cache_manager.redis_client:
                ttl = await cache_manager.redis_client.ttl(test_key)
                if ttl > 0:
                    diagnostics["test_ttl"] = True
        except Exception as e:
            diagnostics["errors"].append(f"Redis TTL test failed: {str(e)}")
        
        # Get Redis info
        try:
            if cache_manager.redis_client:
                info = await cache_manager.redis_client.info()
                diagnostics["redis_info"] = {
                    "redis_version": info.get("redis_version"),
                    "used_memory_human": info.get("used_memory_human"),
                    "used_memory_peak_human": info.get("used_memory_peak_human"),
                    "connected_clients": info.get("connected_clients"),
                    "total_commands_processed": info.get("total_commands_processed"),
                    "keyspace": info.get("db0"),  # Keyspace info
                    "uptime_in_seconds": info.get("uptime_in_seconds"),
                    "uptime_in_days": info.get("uptime_in_days")
                }
        except Exception as e:
            diagnostics["errors"].append(f"Failed to get Redis info: {str(e)}")
        
        # Clean up test key
        try:
            await cache_manager.delete(test_key)
        except:
            pass
        
    except Exception as e:
        diagnostics["errors"].append(f"Unexpected error: {str(e)}")
        logger.error(f"Redis test error: {e}", exc_info=True)
    
    return diagnostics


@router.get("/api/cache/redis/stats")
async def get_redis_stats():
    """
    Get detailed Redis statistics and cache usage
    """
    from backend.core import cache_manager
    
    stats = {
        "cache_manager": await production_cache_manager.get_stats(),
        "face_cache": None,  # retired with the legacy FaceDatabase chain
        "redis_connection": {
            "enabled": cache_manager._enabled,
            "connected": cache_manager.redis_client is not None,
            "healthy": await cache_manager.health_check() if cache_manager._enabled else False
        }
    }
    
    # Get Redis info if available
    if cache_manager._enabled and cache_manager.redis_client:
        try:
            info = await cache_manager.redis_client.info()
            stats["redis_server"] = {
                "version": info.get("redis_version"),
                "memory": {
                    "used": info.get("used_memory_human"),
                    "peak": info.get("used_memory_peak_human"),
                    "max": info.get("maxmemory_human") if info.get("maxmemory") else "unlimited"
                },
                "clients": info.get("connected_clients"),
                "commands": info.get("total_commands_processed"),
                "keyspace": info.get("db0"),  # Shows keys, expires, etc.
                "uptime_days": info.get("uptime_in_days")
            }
            
            # Count keys by pattern
            try:
                face_keys = await cache_manager.redis_client.keys("v2:face:*")
                stats_keys = await cache_manager.redis_client.keys("v2:stats:*")
                detection_keys = await cache_manager.redis_client.keys("v2:detection:*")
                
                stats["cache_keys"] = {
                    "face_matches": len(face_keys),
                    "pipeline_stats": len(stats_keys),
                    "detections": len(detection_keys),
                    "total": len(face_keys) + len(stats_keys) + len(detection_keys)
                }
            except Exception as e:
                stats["cache_keys"] = {"error": str(e)}
                
        except Exception as e:
            stats["redis_server"] = {"error": str(e)}
    
    return stats

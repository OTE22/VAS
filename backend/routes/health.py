"""
Health Check Routes
===================
Routes for health checks and monitoring.
"""

import os
import sys
import logging
from datetime import datetime
from fastapi import APIRouter, HTTPException

# Add parent directory to path
parent_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if parent_dir not in sys.path:
    sys.path.insert(0, parent_dir)

from config import settings
from db_connection import db_manager
from backend.config import FACE_TRACKING_ENABLED, CACHE_ENABLED
from backend.core import (
    cache_manager, model_manager, processing_queue,
    face_tracker, retention_manager, db_circuit_breaker, ws_manager
)

logger = logging.getLogger(__name__)

router = APIRouter()

# SQL Agent state lives in backend.lifespan and is assigned during startup.
# Import the MODULE (not the names) so we read the live values at request time —
# a `from ... import name` here would snapshot the pre-startup values forever.
try:
    import backend.lifespan as _lifespan
except ImportError:
    _lifespan = None


@router.get("/health/live")
async def liveness_check():
    """LIVENESS: zero I/O, answers instantly if the event loop is alive.

    This is what the container healthcheck and frontend pollers should hit —
    it never touches the DB/Redis/models, so it cannot flap when a dependency
    is slow, and its latency directly measures event-loop responsiveness
    (p99 target < 100ms even under full inference load).
    """
    return {"status": "alive", "timestamp": datetime.utcnow().isoformat()}


@router.get("/health/ready")
async def readiness_check():
    """READINESS: parallel dependency checks, each bounded to 2 seconds.

    Returns 503 when a REQUIRED dependency (database, models) is down so load
    balancers / orchestrators can stop routing traffic; optional dependencies
    (cache, tracker) only mark the response as degraded.
    """
    import asyncio
    from fastapi.responses import JSONResponse

    async def _bounded(name, coro_fn, required):
        try:
            result = await asyncio.wait_for(coro_fn(), timeout=2.0)
            return name, {"healthy": bool(result), "required": required}
        except asyncio.TimeoutError:
            return name, {"healthy": False, "required": required, "error": "timeout (2s)"}
        except Exception as e:
            return name, {"healthy": False, "required": required, "error": str(e)}

    async def _check_models():
        return model_manager.health_check()

    async def _check_cache():
        if not CACHE_ENABLED:
            return True
        return await cache_manager.health_check()

    async def _check_queue():
        stats = await processing_queue.get_stats()
        return stats["processing"] < getattr(settings, 'MAX_CONCURRENT_REQUESTS', 100) * 2

    results = dict(await asyncio.gather(
        _bounded("database", db_manager.health_check, required=True),
        _bounded("models", _check_models, required=True),
        _bounded("cache", _check_cache, required=False),
        _bounded("queue", _check_queue, required=False),
    ))

    required_ok = all(v["healthy"] for v in results.values() if v["required"])
    all_ok = all(v["healthy"] for v in results.values())
    status = "ready" if all_ok else ("degraded" if required_ok else "not_ready")

    # Background-service supervision. Pure in-memory registry read — zero I/O,
    # no timeout needed. Reported under its OWN top-level key (components has a
    # fixed {healthy, required} shape consumers assert on), and it NEVER
    # affects the 503 branch: a dead cleanup loop must not make the load
    # balancer pull the whole API. It degrades the status so operators see it.
    background_summary = None
    try:
        from backend.core.service_supervisor import get_service_health, stale_services

        services = get_service_health()
        degraded_names = sorted(
            name for name, s in services.items()
            if s["status"] not in ("running", "starting")
        )
        stale_names = sorted(stale_services())
        background_summary = {
            "total": len(services),
            "degraded": degraded_names,
            "stale": stale_names,
        }
        if (degraded_names or stale_names) and status == "ready":
            status = "degraded"
    except Exception:
        pass  # supervision must never break readiness reporting

    body = {
        "status": status,
        "timestamp": datetime.utcnow().isoformat(),
        "components": results,
    }
    if background_summary is not None:
        body["background_services"] = background_summary
    return JSONResponse(status_code=200 if required_ok else 503, content=body)


@router.get("/health")
async def health_check():
    """Health check endpoint for monitoring"""
    try:
        # Check database health
        db_healthy = await db_manager.health_check()

        # Check cache health
        cache_healthy = await cache_manager.health_check() if CACHE_ENABLED else True

        # Check models health
        models_healthy = model_manager.health_check()

        # Check queue health
        queue_healthy = True
        try:
            queue_stats = await processing_queue.get_stats()
            queue_healthy = queue_stats["processing"] < getattr(settings, 'MAX_CONCURRENT_REQUESTS', 100) * 2
        except:
            queue_healthy = False

        # Check face tracker health
        tracker_healthy = True
        if FACE_TRACKING_ENABLED:
            try:
                await face_tracker.get_stats()
            except:
                tracker_healthy = False

        # Check SQL Agent health (read live state set by lifespan startup)
        SQL_AGENT_AVAILABLE = getattr(_lifespan, "SQL_AGENT_AVAILABLE", False)
        sql_agent_instance = getattr(_lifespan, "sql_agent_instance", None)
        sql_agent_healthy = False
        sql_agent_status = "not_available"
        if SQL_AGENT_AVAILABLE:
            if sql_agent_instance is not None:
                try:
                    if hasattr(sql_agent_instance, 'test_connection'):
                        # test_connection is synchronous DB I/O - run it off the event loop
                        import asyncio
                        sql_agent_healthy = await asyncio.get_running_loop().run_in_executor(
                            None, sql_agent_instance.test_connection
                        )
                    else:
                        sql_agent_healthy = True
                    sql_agent_status = "healthy" if sql_agent_healthy else "unhealthy"
                except:
                    sql_agent_status = "error"
            else:
                sql_agent_status = "not_initialized"
        else:
            sql_agent_status = "not_available"

        # Check vector backend health
        vector_backend = getattr(settings, 'VECTOR_BACKEND', 'faiss').lower()
        vector_backend_healthy = True
        vector_backend_status = "healthy"
        
        if vector_backend == 'pgvector':
            try:
                from backend.core.identity_index_pgvector import identity_index_pgvector
                if identity_index_pgvector:
                    # Simple check - if db is healthy, pgvector should work
                    vector_backend_status = "healthy" if db_healthy else "unhealthy"
                    vector_backend_healthy = db_healthy
                else:
                    vector_backend_status = "not_initialized"
                    vector_backend_healthy = False
            except Exception as e:
                vector_backend_status = f"error: {str(e)}"
                vector_backend_healthy = False
        else:
            # FAISS - check if identity index is available
            try:
                from backend.core.identity_index import identity_index
                if identity_index:
                    vector_backend_status = "healthy"
                    vector_backend_healthy = True
                else:
                    vector_backend_status = "not_initialized"
                    vector_backend_healthy = False
            except Exception as e:
                vector_backend_status = f"error: {str(e)}"
                vector_backend_healthy = False

        # Overall status
        all_healthy = db_healthy and cache_healthy and models_healthy and queue_healthy and tracker_healthy and vector_backend_healthy
        overall_status = "healthy" if all_healthy else "degraded"

        return {
            "status": overall_status,
            "version": getattr(settings, 'VERSION', '5.1'),
            "timestamp": datetime.utcnow().isoformat(),
            "components": {
                "database": "healthy" if db_healthy else "unhealthy",
                "cache": "healthy" if cache_healthy else "unhealthy" if CACHE_ENABLED else "disabled",
                "models": "healthy" if models_healthy else "unhealthy",
                "queue": "healthy" if queue_healthy else "unhealthy",
                "face_tracker": "healthy" if tracker_healthy else "unhealthy" if FACE_TRACKING_ENABLED else "disabled",
                "sql_agent": sql_agent_status,
                "vector_backend": vector_backend_status,
            },
            "vector_backend_type": vector_backend,
            "queue_size": processing_queue.queue.qsize(),
            "processing": processing_queue.processing_count,
            "websocket_connections": len(ws_manager.active_connections),
            "circuit_breaker": db_circuit_breaker.state.value,
        }
    except Exception as e:
        logger.error(f"Health check error: {e}")
        return {
            "status": "unhealthy",
            "error": str(e),
            "timestamp": datetime.utcnow().isoformat()
        }


@router.get("/health/detailed")
async def detailed_health_check():
    """Detailed health check for all components"""
    try:
        checks = {}

        # Background-service supervision: full per-service dump (status,
        # last_success, last_error, consecutive_failures, restarts) — the
        # detailed view is where operators diagnose WHICH loop is sick.
        try:
            from backend.core.service_supervisor import get_service_health, stale_services
            services = get_service_health()
            checks["background_services"] = {
                "healthy": all(s["status"] in ("running", "starting") for s in services.values())
                           and not stale_services(),
                "services": services,
            }
        except Exception as bg_error:
            checks["background_services"] = {"healthy": False, "error": str(bg_error)}

        # Database health
        db_healthy = await db_manager.health_check()
        checks["database"] = {
            "healthy": db_healthy,
            "connections": db_manager.get_connection_stats()
        }

        # Redis health
        if CACHE_ENABLED:
            cache_healthy = await cache_manager.health_check()
            checks["redis"] = {
                "healthy": cache_healthy,
                "enabled": True
            }
        else:
            checks["redis"] = {"enabled": False}

        # Face tracker health
        checks["face_tracker"] = {
            "enabled": FACE_TRACKING_ENABLED,
            "healthy": True if not FACE_TRACKING_ENABLED else await face_tracker.get_stats() is not None
        }

        # Model health
        checks["models"] = {
            "healthy": model_manager.health_check(),
            "initialized": model_manager._initialized
        }

        # Queue health
        queue_stats = await processing_queue.get_stats()
        checks["queue"] = {
            "healthy": queue_stats["processing"] < getattr(settings, 'MAX_CONCURRENT_REQUESTS', 100) * 2,
            "stats": queue_stats
        }

        # Storage health
        storage_stats = await retention_manager.get_storage_stats()
        checks["storage"] = {
            "healthy": storage_stats["usage_percent"] < 95,
            "stats": storage_stats
        }

        # Overall health
        overall_healthy = all(
            check.get("healthy", True) if isinstance(check, dict) else check
            for check in checks.values()
        )

        return {
            "healthy": overall_healthy,
            "timestamp": datetime.utcnow().isoformat(),
            "components": checks
        }
    except Exception as e:
        logger.error(f"Detailed health check error: {e}")
        return {
            "healthy": False,
            "error": str(e),
            "timestamp": datetime.utcnow().isoformat()
        }


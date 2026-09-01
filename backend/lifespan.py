"""
Application Lifespan
====================
Production-grade application lifecycle management.
"""

import os
import sys
import asyncio
import logging
import time
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.concurrency import run_in_threadpool

# Add parent directory to path
parent_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if parent_dir not in sys.path:
    sys.path.insert(0, parent_dir)

from config import settings
from db_connection import db_manager
from backend.config import (
    FACE_TRACKING_ENABLED, FACE_TRACKING_WINDOW_SECONDS,
    CACHE_ENABLED, BATCH_WRITE_SIZE
)
from backend.core import (
    cache_manager, production_cache_manager, model_manager,
    face_tracker, batch_writer, retention_manager, metrics_collector,
    processing_queue
)
from backend.core.metrics import metrics_queue_size
from backend.services.queue_worker import queue_worker
from backend.services.cache_metrics import update_cache_metrics

# utils/performance_config.apply_optimized_config() used to run here. It read
# 15 values off `settings` and wrote them back into os.environ — after
# Settings() had already been constructed, so they could never reach `settings`
# and only misled later readers. Its GPU/CPU "optimized defaults" were
# unreachable for the same reason: every key it looked up is a declared field,
# so the default branch never ran. Sizing now comes from configuration alone.

logger = logging.getLogger(__name__)

# SQL Agent imports (will be set during startup)
SQL_AGENT_AVAILABLE = False
SQL_AGENT_ROUTER_AVAILABLE = False
sql_agent_instance = None
set_sql_agent_instance = None


async def _emergency_shutdown(initialized_components: list, worker_tasks: list = None):
    """Emergency shutdown when startup fails"""
    logger.error("🚨 EMERGENCY SHUTDOWN INITIATED")

    # Stop components in reverse order
    for component in reversed(initialized_components):
        try:
            if component == "database" and hasattr(db_manager, 'close'):
                await db_manager.close()
            elif component == "redis_cache" and hasattr(cache_manager, 'close'):
                await cache_manager.close()
            elif component == "production_cache" and hasattr(production_cache_manager, 'stop'):
                await production_cache_manager.stop()
            elif component == "websocket_redis":
                from backend.core.websocket_manager import ws_manager
                if ws_manager._redis_listener_task and not ws_manager._redis_listener_task.done():
                    ws_manager._redis_listener_task.cancel()
                    try:
                        await ws_manager._redis_listener_task
                    except asyncio.CancelledError:
                        pass
                if ws_manager.redis_pubsub:
                    await ws_manager.redis_pubsub.unsubscribe()
                if ws_manager.redis_client:
                    await ws_manager.redis_client.close()
            elif component == "batch_writer" and hasattr(batch_writer, 'stop'):
                await batch_writer.stop()
            elif component == "map_availability":
                from backend.core import map_availability
                await map_availability.stop_refresh_loop()
            elif component.startswith("workers_") and worker_tasks:
                for task in worker_tasks:
                    task.cancel()
        except Exception as e:
            logger.error(f"Failed to shutdown {component}: {e}")

    logger.error("🛑 Emergency shutdown completed")


def _asyncio_exception_handler(loop, context):
    """Log unhandled task exceptions instead of losing them to GC.

    Without a loop-level handler, a bare create_task whose coroutine dies
    surfaces only as "Task exception was never retrieved" — at garbage
    collection time, long after the fact, with no service attribution. This
    logs immediately with the task name. It never re-raises: a background
    task's death must not take the API process with it.
    """
    exception = context.get("exception")
    if isinstance(exception, asyncio.CancelledError):
        return  # normal shutdown traffic
    task = context.get("task")
    logger.error(
        "unhandled_task_exception task=%s type=%s msg=%s",
        task.get_name() if task else "?",
        type(exception).__name__ if exception else "?",
        context.get("message", ""),
        exc_info=exception,
    )


async def _bounded_stop(component_name: str, awaitable, timeout: float = 10.0) -> str:
    """Await one component's stop/close with a deadline.

    17 of 19 components used to stop UNBOUNDED — any one of them hanging
    stalled the entire shutdown (and gunicorn would eventually SIGABRT the
    worker). Timeouts are recorded and aggregated, never fatal: shutdown
    always proceeds to the next component.
    """
    try:
        await asyncio.wait_for(awaitable, timeout=timeout)
        return "success"
    except asyncio.TimeoutError:
        logger.error(f"    ❌ {component_name} stop timed out after {timeout:.0f}s")
        return "timeout"
    except asyncio.CancelledError:
        raise
    except Exception as e:
        logger.error(f"    ❌ {component_name} stop failed: {e}")
        return f"error: {e}"


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Production-grade application lifecycle management with Redis container support
    """

    startup_time = time.time()
    startup_success = False
    initialized_components = []
    worker_tasks = []
    cache_metrics_task = None
    # Hoisted so the shutdown stop-list can see them: these two were started
    # in Phase 3.x but never appeared in components_to_stop — they were only
    # killed by the blanket all-tasks cancel at the very end.
    batch_flush_task = None
    loop_lag_task = None

    # ==================== STARTUP PHASE ====================
    try:
        # Loop-level exception attribution, installed before ANY task exists.
        asyncio.get_running_loop().set_exception_handler(_asyncio_exception_handler)

        logger.info("=" * 70)
        # Version comes from config.VERSION — this line used to hardcode
        # "v5.1" while config said 5.0.0, which is its own kind of drift.
        logger.info(f"🚀 Starting Face Recognition Service v{settings.VERSION}")
        from backend.core.runtime_fingerprint import log_fingerprint
        log_fingerprint(logger)
        logger.info(f"📊 Face Tracking: {'ENABLED' if FACE_TRACKING_ENABLED else 'DISABLED'}")
        logger.info(f"💾 Redis Cache: {'ENABLED' if CACHE_ENABLED else 'DISABLED'}")
        logger.info("=" * 70)

        # ==================== PHASE 1: Infrastructure ====================
        logger.info("📦 Phase 1: Initializing Infrastructure...")

        # 1.0 Database Migrations (run before database init)
        logger.info("")
        logger.info("  🔄 Running database migrations...")
        logger.info("  " + "=" * 60)
        try:
            from backend.utils.migrations import (
                run_migrations_async_detailed,
                should_fail_startup,
            )

            migration_result = await run_migrations_async_detailed(settings.MIGRATIONS_MODE)
            if migration_result.ok:
                logger.info(
                    "  ✅ Database migrations OK (%s, revision=%s)",
                    migration_result.outcome.value, migration_result.current_revision,
                )
                initialized_components.append("migrations")
            elif should_fail_startup(migration_result):
                # Serving against an unverified schema silently corrupts data
                # and returns wrong answers. Refuse — in every environment;
                # there is no permissive mode for schema state.
                logger.error(
                    "  ❌ Database migrations not satisfied: %s — %s",
                    migration_result.outcome.value, migration_result.detail,
                )
                raise RuntimeError(
                    f"database migrations not satisfied: {migration_result.outcome.value}"
                )
        except RuntimeError:
            raise
        except Exception as e:
            logger.error(f"  ❌ Exception during migration check: {type(e).__name__}: {e}")
            logger.exception("  Migration exception details:")
            raise
        logger.info("  " + "=" * 60)
        logger.info("")

        # 1.1 Database (init_db verifies the exact Alembic head — fail-closed;
        #     the application never runs Base.metadata.create_all())
        logger.info("  🔄 Initializing database...")
        try:
            await db_manager.init_db()
            logger.info("  ✅ Database initialized")
            initialized_components.append("database")
        except Exception as e:
            logger.error(f"  ❌ Database initialization failed: {e}")
            raise

        # 1.1.1 Runtime settings hydration.
        #
        # Runs here — immediately after the database is reachable and before
        # ANY component is constructed — because hydration is what makes an
        # admin's saved setting real. Every later phase (Redis sizing, the
        # vector index geometry, the model manager, every background job)
        # captures values off `settings` as it initializes; a value applied
        # after its consumer has read it is applied too late to matter.
        # This used to sit after the pgvector index was already built.
        logger.info("  🔄 Hydrating runtime settings from database...")
        try:
            from backend.core import runtime_settings as _rt_settings
            async with db_manager.get_session() as _settings_db:
                hydrated = await _rt_settings.hydrate_from_db(_settings_db)
            logger.info(f"  ✅ Runtime settings hydrated from database ({hydrated} applied)")
        except Exception as hydrate_err:
            logger.warning(f"  ⚠️  Settings hydration failed (using env/defaults): {hydrate_err}")

        # 1.1.1b ML feature-definition contract — FAIL CLOSED in every
        # environment. Definitions are seeded only by Alembic (frozen literal);
        # the runtime inventory must match the table exactly. A mismatch means
        # code changed without its migration: refuse to serve feature-less ML.
        from backend.ml.feature_store import feature_store as _feature_store
        async with db_manager.get_session() as _fs_db:
            _fs_check = await _feature_store.verify_definitions(_fs_db)
        logger.info(f"  ✅ ML feature definitions verified ({_fs_check['verified']} definitions)")

        # 1.1.2 Webhook body-size alignment check (AFTER hydration, so a
        # DB-stored admin override is included). The settings API refuses new
        # oversized values; a pre-existing one must not brick boot, so this is
        # a loud warning, not a gate. "Expected" because it reads the nginx
        # config baked into this image - the running nginx mounts the host
        # file, so the live limit is confirmed by boundary tests, not here.
        try:
            from backend.core.runtime_settings import expected_nginx_webhook_body_limit_mb
            _nginx_mb = expected_nginx_webhook_body_limit_mb()
            _recv_mb = int(settings.WEBHOOK_MAX_BODY_MB)
            if _nginx_mb is not None and _recv_mb > _nginx_mb:
                logger.error(
                    f"  ❌ CONFIG DRIFT: WEBHOOK_MAX_BODY_MB={_recv_mb} exceeds the expected "
                    f"nginx client_max_body_size={_nginx_mb}m on the webhook route - nginx "
                    f"rejects bodies over {_nginx_mb}MB before this service ever sees them. "
                    f"Raise nginx or lower WEBHOOK_MAX_BODY_MB.")
            elif _nginx_mb is not None:
                logger.info(
                    f"  ✅ Webhook body limits aligned: receiver {_recv_mb}MB <= "
                    f"expected nginx {_nginx_mb}MB")
        except Exception as _align_err:
            logger.warning(f"  ⚠️  Webhook body-size alignment check skipped: {_align_err}")

        # 1.2 Redis Cache (for container)
        logger.info("  🔄 Initializing Redis cache (container)...")
        try:
            # Get Redis URL from settings or environment
            redis_url = settings.REDIS_URL
            from backend.security.redaction import redact_url
            logger.info(f"  📍 Connecting to Redis at: {redact_url(redis_url)}")

            await cache_manager.initialize()
            if cache_manager._enabled:
                logger.info("  ✅ Redis cache connected")
                initialized_components.append("redis_cache")
            else:
                logger.warning("  ⚠️  Redis cache disabled - using in-memory only")
        except Exception as e:
            logger.error(f"  ❌ Redis cache initialization failed: {e}")
            # Non-critical - continue without cache
            cache_manager._enabled = False
        
        # 1.2.1 Redis Cache Service (for page caching)
        logger.info("  🔄 Initializing Redis cache service (page caching)...")
        try:
            from backend.core.redis_cache import redis_cache_service
            cache_enabled = await redis_cache_service.initialize()
            if cache_enabled:
                logger.info("  ✅ Redis cache service initialized (page caching enabled)")
                initialized_components.append("redis_cache_service")

                # Drop dashboard/unknown payloads cached by a PREVIOUS build.
                #
                # Those blobs are whole serialized WebSocket messages, held for
                # up to CACHE_TTL (dashboard) / 30 h (unknown), and replayed
                # verbatim. An entry written before the timezone-aware wire
                # format landed still carries naive timestamps, so the browser
                # would keep printing "[TIME] Legacy naive timestamp received"
                # for a day and a half after a correct deploy — with nothing in
                # the running code to blame. Cheap to rebuild, so evict on
                # every startup rather than reason about which build wrote what.
                try:
                    dropped = (await redis_cache_service.invalidate_dashboard_cache()
                               + await redis_cache_service.invalidate_unknown_cache())
                    if dropped:
                        logger.info("  🧹 Evicted %d cached dashboard/unknown "
                                    "payload(s) from a previous build", dropped)
                except Exception as exc:      # noqa: BLE001 — never block startup
                    logger.warning("  ⚠️  Could not evict stale cached payloads: %s", exc)
            else:
                logger.warning("  ⚠️  Redis cache service disabled - page caching will be slower")
        except Exception as e:
            logger.error(f"  ❌ Redis cache service initialization failed: {e}")
            # Non-critical - continue without page caching

        # 1.3 Production Cache Manager
        logger.info("  🔄 Initializing production cache manager...")
        try:
            # Pass Redis client if available
            if cache_manager._enabled and hasattr(cache_manager, 'redis_client'):
                production_cache_manager.redis_client = cache_manager.redis_client
                production_cache_manager._enabled = True
                await production_cache_manager.start()
                logger.info("  ✅ Production cache manager started")
                initialized_components.append("production_cache")
            else:
                logger.warning("  ⚠️  Production cache disabled (no Redis)")
        except Exception as e:
            logger.error(f"  ❌ Production cache failed: {e}")
            # Non-critical

        # ==================== PHASE 2: Machine Learning Models ====================
        logger.info("🧠 Phase 2: Loading ML Models...")

        # 2.1 Face recognition models (synchronous, run in thread pool)
        logger.info("  🔄 Loading face detection models...")
        
        # Detect GPU and log status
        try:
            from utils.gpu_detection import detect_gpu
            has_gpu, gpu_type, gpu_info = detect_gpu()
            if has_gpu:
                logger.info(f"  🚀 GPU detected: {gpu_type} - {gpu_info.get('name', 'Unknown')}")
            else:
                logger.info("  💻 No GPU detected, using CPU")
        except Exception as e:
            logger.warning(f"  ⚠️  Could not detect GPU: {e}")
        
        try:
            # Run model initialization in thread pool (it's CPU/GPU intensive)
            await run_in_threadpool(model_manager.initialize)

            # Verify models
            if model_manager.detector and model_manager.recognizer:
                logger.info(f"  ✅ Models loaded (detector: {settings.DETECTION_MODEL})")
                initialized_components.append("models")
            else:
                raise RuntimeError("Models not properly initialized")

            # GPU readiness. Provider discovery alone proves nothing: a session
            # can register CUDA and still fail on first execution. This runs a
            # real inference through the loaded sessions, and in GPU mode with
            # ALLOW_CPU_FALLBACK=false it aborts startup rather than serving at
            # a fraction of the expected throughput with no outward symptom.
            from backend.core.gpu_runtime import verify_gpu_readiness

            await run_in_threadpool(
                verify_gpu_readiness,
                settings,
                sessions=[
                    ("SCRFD", getattr(model_manager.detector, "session", None)),
                    ("ArcFace", getattr(model_manager.recognizer, "session", None)),
                ],
            )
        except Exception as e:
            logger.error(f"  ❌ Model loading failed: {e}")
            raise  # Critical failure

        # The FaceRecognitionCache wiring that used to sit here is gone with the
        # legacy FaceDatabase chain: it cached lookups against a store that was
        # write-never under pgvector, i.e. it accelerated an always-empty answer.

        # 2.2 Vector search index — a DISPOSABLE acceleration layer over the
        # authoritative vectors in identity_embeddings.embedding.
        #
        # The backend decides what actually runs. Under pgvector there is no
        # separate index and therefore NO snapshot or reconciliation loops —
        # the previous code constructed the FAISS service and started three
        # loops unconditionally, which is why an empty index was re-serialized
        # to disk every 5 minutes (527 times) on a pgvector deployment.
        #
        # Selection FAILS CLOSED: a configured-but-unusable FAISS backend stops
        # startup unless VECTOR_INDEX_FALLBACK=pgvector is explicitly set.
        logger.info("  🔄 Initializing vector search index...")
        vector_index_manager = None
        try:
            from backend.core.vector_index import select_backend
            from backend.core.vector_index.manager import VectorIndexManager

            model_version = None
            try:
                model_version = os.path.splitext(
                    os.path.basename(str(settings.RECOGNITION_MODEL)))[0][:64]
            except Exception:
                model_version = None

            selection = select_backend(
                settings,
                storage_dir=settings.IDENTITY_INDEX_DB_PATH,
                model_version=model_version)
            vector_index_manager = VectorIndexManager(selection, db_manager, settings)

            startup_report = await vector_index_manager.startup()
            logger.info("  ✅ Vector index ready: backend=%s %s",
                        selection.backend, startup_report)
            if selection.degraded:
                logger.critical(
                    "  🚨 DEGRADED: running %s while %s was configured — %s",
                    selection.backend, selection.requested, selection.reason)

            # Publish for routes/services that need status or direct access.
            try:
                core_module = sys.modules.get('backend.core')
                if core_module is not None:
                    setattr(core_module, 'vector_index_manager', vector_index_manager)
                    setattr(core_module, 'vector_index', selection.index)
            except Exception as e:
                logger.warning(f"Could not publish vector_index in module: {e}")

            initialized_components.append("vector_index")

            # Snapshot + reconciliation loops exist ONLY for a real FAISS index.
            if selection.backend == "faiss":
                autosave_interval = vector_index_manager.autosave_interval()

                async def _vector_index_autosave():
                    await vector_index_manager.save_once(trigger="autosave")

                async def _vector_index_reconcile():
                    await vector_index_manager.reconcile_once(trigger="scheduled")

                background_tasks["vector_index_autosave"] = asyncio.create_task(
                    supervised_loop(
                        "vector_index_autosave",
                        lambda: vector_index_manager.autosave_interval(),
                        _vector_index_autosave,
                        initial_delay=autosave_interval,
                        error_backoff_base=120.0))
                background_tasks["vector_index_reconcile"] = asyncio.create_task(
                    supervised_loop(
                        "vector_index_reconcile",
                        lambda: vector_index_manager.reconcile_interval(),
                        _vector_index_reconcile,
                        initial_delay=600.0,
                        error_backoff_base=600.0))
                logger.info("  ✅ Vector index loops started (autosave every %.0fs, "
                            "reconcile every %.0fs)", autosave_interval,
                            vector_index_manager.reconcile_interval())
            else:
                logger.info("  ⏭️  No index loops: %s stores vectors in place, so "
                            "there is nothing to snapshot or reconcile",
                            selection.backend)

            # Initialize Identity Service
            from backend.core.identity_service import IdentityService
            from backend.core.identity_index_pgvector import get_pgvector_index
            
            # Get pgvector index if using pgvector backend
            pgvector_index = None
            if settings.VECTOR_BACKEND.lower() == 'pgvector':
                pgvector_index = get_pgvector_index()
                if pgvector_index:
                    logger.info("  ✅ pgvector index initialized for IdentityService")
                    # Self-heal: make sure the HNSW/IVFFlat index on
                    # identity_embeddings.embedding actually exists (create_all
                    # doesn't create it, and without it every similarity search
                    # is a sequential scan). Previously this method was never
                    # called from anywhere.
                    try:
                        async with db_manager.get_session() as _idx_db:
                            idx_results = await pgvector_index.ensure_indexes_exist(_idx_db)
                        if idx_results.get('general_index'):
                            logger.info("  ✅ pgvector ANN index verified/created on identity_embeddings.embedding")
                        else:
                            logger.warning("  ⚠️  pgvector ANN index could NOT be verified - similarity searches will seq-scan")
                    except Exception as idx_err:
                        logger.warning(f"  ⚠️  pgvector ANN index check failed: {idx_err}")
                else:
                    logger.warning("  ⚠️  pgvector backend enabled but pgvector_index is None")

            # (Settings hydration moved to phase 1.1.1 — it has to precede
            # every consumer, including the pgvector index geometry above.)

            identity_service = IdentityService(
                pgvector_index=pgvector_index,
                vector_index=(vector_index_manager.index
                              if vector_index_manager is not None else None))
            
            # Make it available globally
            try:
                core_module = sys.modules.get('backend.core')
                if core_module is not None:
                    setattr(core_module, 'identity_service', identity_service)
                
                svc_module = sys.modules.get('backend.core.identity_service')
                if svc_module is not None:
                    setattr(svc_module, 'identity_service', identity_service)
            except Exception as e:
                logger.warning(f"Could not set identity_service in module: {e}")
            
            logger.info("  ✅ Identity service initialized")
            initialized_components.append("identity_service")
            
            # Initialize Advanced Search Service
            try:
                from backend.core.advanced_search import advanced_search_service
                # Pass pgvector_index if available (same one used by IdentityService)
                advanced_search_service.initialize(model_manager, None, pgvector_index=pgvector_index)
                if pgvector_index:
                    logger.info("  ✅ Advanced search service initialized with pgvector backend")
                else:
                    logger.info("  ✅ Advanced search service initialized with FAISS backend")
                
                # Make it available globally
                try:
                    core_module = sys.modules.get('backend.core')
                    if core_module is not None:
                        setattr(core_module, 'advanced_search_service', advanced_search_service)
                    
                    adv_search_module = sys.modules.get('backend.core.advanced_search')
                    if adv_search_module is not None:
                        setattr(adv_search_module, 'advanced_search_service', advanced_search_service)
                except Exception as e:
                    logger.warning(f"Could not set advanced_search_service in module: {e}")
                
                initialized_components.append("advanced_search")
                
                # Initialize Batch Search Service
                try:
                    from backend.core.batch_search_service import batch_search_service
                    batch_search_service.initialize(advanced_search_service)
                    logger.info("  ✅ Batch search service initialized")
                    
                    # Make it available globally
                    try:
                        core_module = sys.modules.get('backend.core')
                        if core_module is not None:
                            setattr(core_module, 'batch_search_service', batch_search_service)
                        
                        batch_module = sys.modules.get('backend.core.batch_search_service')
                        if batch_module is not None:
                            setattr(batch_module, 'batch_search_service', batch_search_service)
                    except Exception as e:
                        logger.warning(f"Could not set batch_search_service in module: {e}")
                    
                    initialized_components.append("batch_search")
                except Exception as e:
                    logger.warning(f"  ⚠️  Batch search service initialization failed: {e}")
                    # Non-critical
                    
            except Exception as e:
                logger.warning(f"  ⚠️  Advanced search service initialization failed: {e}")
                # Non-critical, continue without advanced search
            
            # Load known faces from assets/faces into Identity system
            if identity_service and model_manager:
                try:
                    from backend.core.identity_loader import IdentityLoader
                    identity_loader = IdentityLoader(identity_service, model_manager)
                    
                    # Get faces directory from settings
                    faces_dir = settings.FACES_DIR
                    
                    # Load known faces into Identity system
                    async with db_manager.get_session() as db:
                        logger.info("  🔄 Loading known faces from storage/faces directory...")
                        loaded, skipped, errors = await identity_loader.load_known_faces_from_directory(
                            faces_dir=faces_dir,
                            db=db,
                            force_reload=False  # Don't reload existing identities
                        )
                        
                        if loaded > 0:
                            await db.commit()
                            logger.info(f"  ✅ Loaded {loaded} known faces from {faces_dir} into Identity system")
                        elif skipped > 0:
                            logger.info(f"  ℹ️  {skipped} known faces already loaded (skipped)")
                        
                        # Verify indexes
                        verification = await identity_loader.verify_indexes(db)
                        logger.info(f"  📊 Index Verification:")
                        
                        # Check which backend is being used
                        use_pgvector = (
                            settings.VECTOR_BACKEND.lower() == 'pgvector' and
                            identity_service.use_pgvector and
                            identity_service.pgvector_index
                        )
                        
                        if use_pgvector:
                            logger.info(f"     Backend: pgvector")
                            logger.info(f"     KNOWN: pgvector={verification['known_index']['pgvector_count']} embeddings, "
                                      f"DB={verification['known_index']['database_count']} identities, "
                                      f"Match={verification['known_index']['match']}")
                            logger.info(f"     UNKNOWN: pgvector={verification['unknown_index']['pgvector_count']} embeddings, "
                                      f"DB={verification['unknown_index']['database_count']} identities, "
                                      f"Match={verification['unknown_index']['match']}")
                        else:
                            logger.info(f"     Backend: FAISS")
                            logger.info(f"     KNOWN: FAISS={verification['known_index']['faiss_count']}, "
                                      f"DB={verification['known_index']['database_count']}, "
                                      f"Match={verification['known_index']['match']}")
                            logger.info(f"     UNKNOWN: FAISS={verification['unknown_index']['faiss_count']}, "
                                      f"DB={verification['unknown_index']['database_count']}, "
                                      f"Match={verification['unknown_index']['match']}")
                        
                        if verification['known_index'].get('issues'):
                            logger.warning(f"  ⚠️  KNOWN index issues: {verification['known_index']['issues']}")
                        if verification['unknown_index'].get('issues'):
                            logger.warning(f"  ⚠️  UNKNOWN index issues: {verification['unknown_index']['issues']}")
                        
                        # Repair orphaned entries AFTER loading known faces (critical - must run after loading)
                        # Only repair FAISS if using FAISS backend
                        use_pgvector = (
                            settings.VECTOR_BACKEND.lower() == 'pgvector' and
                            identity_service.use_pgvector and
                            identity_service.pgvector_index
                        )
                        
                        if not use_pgvector:
                            # FAISS backend: converge the index onto PostgreSQL.
                            # Replaces repair_orphaned_* on the legacy service,
                            # which compared COUNT(*) against ntotal — blind to
                            # the common case of one vector missing and one
                            # stale vector present, which counts identically to
                            # a healthy index.
                            try:
                                if vector_index_manager is not None:
                                    _recon = await vector_index_manager.reconcile_once(
                                        trigger="startup")
                                    logger.info("  🔧 Vector index reconciliation: %s", _recon)
                                else:
                                    logger.warning("  ⚠️  No vector index manager; "
                                                   "skipping reconciliation")
                            except Exception as repair_error:
                                logger.warning(f"  ⚠️  Reconciliation failed: {repair_error}")
                        else:
                            logger.info("  ⏭️  Skipping FAISS repair (using pgvector backend)")
                            logger.info("  ℹ️  pgvector doesn't require FAISS repair - all data is in PostgreSQL")
                    
                except Exception as e:
                    logger.warning(f"  ⚠️  Failed to load known faces from storage/faces: {e}")
                    # Non-critical, continue without loading
            
        except Exception as e:
            logger.error(f"  ❌ Identity index initialization failed: {e}")
            logger.warning("    Continuing without identity management (unknown faces will use legacy system)")
            identity_service = None

        # 2.2f Crash-safe embedding provenance: a worker that died between the
        # embedding commit and its detection's persistence leaves a camera-origin
        # embedding with no detection. Reconcile it now (canonical path, vector
        # index up) and again on every retention cycle. Idempotent.
        try:
            from backend.core.identity_retention import identity_retention_manager as _irm
            if _irm:
                _stale = await _irm.reconcile_orphan_camera_embeddings()
                if _stale.get("embeddings"):
                    logger.warning("  🧹 Reconciled %s stale camera embedding(s) from a previous crash",
                                   _stale["embeddings"])
        except Exception as e:
            logger.error(f"  ❌ Stale camera embedding reconciliation failed at startup: {e}")

        # ==================== PHASE 3: Core Services ====================
        logger.info("⚙️ Phase 3: Starting Core Services...")

        # 3.1 Face Tracker
        if FACE_TRACKING_ENABLED:
            logger.info("  🔄 Starting face tracker...")
            try:
                await face_tracker.start()
                logger.info(f"  ✅ Face tracker started (window: {FACE_TRACKING_WINDOW_SECONDS}s)")
                initialized_components.append("face_tracker")
            except Exception as e:
                logger.error(f"  ❌ Face tracker failed: {e}")
                # Non-critical, continue without tracker
        else:
            logger.info("  ⏭️  Face tracker disabled")

        # 3.2 Batch Database Writer
        logger.info("  🔄 Starting batch database writer...")
        try:
            await batch_writer.start()
            logger.info(f"  ✅ Batch writer started (batch: {BATCH_WRITE_SIZE})")
            initialized_components.append("batch_writer")
        except Exception as e:
            logger.error(f"  ❌ Batch writer failed: {e}")
            # Performance impact only

        # 3.3 Data Retention Manager
        logger.info("  🔄 Starting data retention manager...")
        try:
            await retention_manager.start()
            logger.info(f"  ✅ Data retention started (keep: {settings.DATA_RETENTION_DAYS} days)")
            initialized_components.append("retention_manager")
        except Exception as e:
            logger.error(f"  ❌ Data retention failed: {e}")
            # Non-critical
        
        # 3.2.9 Background Task Notifier
        logger.info("  🔄 Initializing background task notifier...")
        try:
            from backend.core.background_task_notifier import background_task_notifier
            from backend.core.websocket_manager import ws_manager
            background_task_notifier.set_ws_manager(ws_manager)
            
            # Initialize Redis pub/sub for WebSocket multi-worker support
            logger.info("  🔄 Initializing WebSocket Redis pub/sub...")
            try:
                redis_initialized = await ws_manager.initialize_redis()
                if redis_initialized:
                    logger.info("  ✅ WebSocket Redis pub/sub initialized")
                    initialized_components.append("websocket_redis")
                else:
                    logger.warning("  ⚠️  WebSocket Redis pub/sub disabled - WebSocket broadcasts will only work within single worker")
            except Exception as e:
                logger.error(f"  ❌ WebSocket Redis pub/sub initialization failed: {e}")
                # Non-critical - continue without Redis pub/sub

            # Multi-worker single-flight requires shared locking infrastructure.
            # Documented warning path: with WORKERS>1 and no Redis, distributed
            # locks degrade to per-process guards — duplicate intelligence jobs
            # and assessment computations become possible (assessment ROWS
            # still dedup via the DB unique constraint).
            try:
                from backend.core.distributed_lock import redis_available
                workers_configured = int(settings.WORKERS or 1)
                if workers_configured > 1 and not redis_available():
                    logger.warning(
                        "  ⚠️  WORKERS=%s without Redis: cross-worker single-flight "
                        "locks are DEGRADED to per-process guards. Configure Redis "
                        "(shared locking infrastructure) for safe multi-worker "
                        "operation.", workers_configured)
            except Exception:
                logger.debug("multi-worker lock check skipped", exc_info=True)

            logger.info("  ✅ Background task notifier initialized")
            initialized_components.append("task_notifier")
        except Exception as e:
            logger.warning(f"  ⚠️  Task notifier initialization failed: {e}")
            # Non-critical
        
        # 3.2.9 Offline map availability (MapLibre + Martin)
        # One deep check now so the first /api/maps/availability answer is
        # real, then a supervised refresh loop. Failure here must never block
        # boot: maps degrade to "unavailable", the service does not.
        logger.info("  🔄 Checking offline map datasets (Martin)...")
        try:
            from backend.core import map_availability
            snap = await map_availability.refresh()
            await map_availability.start_refresh_loop()
            # Any installed archive whose CONTENT has never been measured is
            # reported unavailable, so measure it — in the background, because
            # decoding tiles must not stand between the process and serving
            # traffic. The dataset stays unavailable until the check passes.
            await map_availability.start_boot_verification()
            initialized_components.append("map_availability")
            logger.info("  ✅ Map availability: martin=%s %s", snap.martin_reachable,
                        {k: (v["state"] if v["available"] else v["reason"])
                         for k, v in snap.styles.items()})
        except Exception as e:
            logger.warning(f"  ⚠️  Map availability check failed (maps report unavailable): {e}")

        # 3.3.0 Log Cleanup Manager
        logger.info("  🔄 Starting log cleanup manager...")
        try:
            from backend.core.log_cleanup import log_cleanup_manager
            if log_cleanup_manager:
                await log_cleanup_manager.start()
                logger.info(f"  ✅ Log cleanup started (retention: {settings.LOGS_LIFE_TIME_HOURS} hours)")
                initialized_components.append("log_cleanup")
            else:
                logger.warning("  ⚠️  Log cleanup not available")
        except ImportError as e:
            logger.warning(f"  ⚠️  Log cleanup module not available: {e}")
        except Exception as e:
            logger.warning(f"  ⚠️  Log cleanup failed: {e}")
            # Non-critical

        # 3.3.1 Identity Clustering Service (for merge suggestions)
        logger.info("  🔄 Starting identity clustering service...")
        try:
            from backend.core.identity_clustering import clustering_service
            if clustering_service and clustering_service._enabled:
                await clustering_service.start()
                logger.info(f"  ✅ Identity clustering started (interval: {clustering_service.cluster_interval_hours} hours)")
                initialized_components.append("identity_clustering")
            else:
                logger.warning("  ⚠️  Identity clustering disabled (libraries not available)")
        except Exception as e:
            logger.warning(f"  ⚠️  Identity clustering failed: {e}")
            # Non-critical

        # 3.3.2 Identity Retention Manager
        logger.info("  🔄 Starting identity retention manager...")
        try:
            from backend.core.identity_retention import identity_retention_manager
            if identity_retention_manager:
                await identity_retention_manager.start()
                logger.info(f"  ✅ Identity retention started")
                initialized_components.append("identity_retention")
            else:
                logger.warning("  ⚠️  Identity retention not available")
        except Exception as e:
            logger.warning(f"  ⚠️  Identity retention failed: {e}")
            # Non-critical

        # 3.4 System Metrics Collector
        logger.info("  🔄 Starting system metrics collector...")
        try:
            await metrics_collector.start()
            logger.info(f"  ✅ Metrics collector started (interval: {metrics_collector.collection_interval}s)")
            initialized_components.append("metrics_collector")
        except Exception as e:
            logger.error(f"  ❌ Metrics collector failed: {e}")
            # Non-critical

        # 3.5 Cache Metrics Updater
        logger.info("  🔄 Starting cache metrics updater...")
        try:
            cache_metrics_task = asyncio.create_task(update_cache_metrics())
            logger.info("  ✅ Cache metrics updater started")
            initialized_components.append("cache_metrics")
        except Exception as e:
            logger.error(f"  ❌ Cache metrics failed: {e}")
            # Non-critical

        # 3.6 Queue Batch Flusher (periodic flush of incomplete batches)
        logger.info("  🔄 Starting queue batch flusher...")
        try:
            from backend.core.service_supervisor import supervised_loop as _supervised_loop
            batch_flush_task = asyncio.create_task(
                _supervised_loop(
                    "batch_flusher",
                    0.5,                       # check every 0.5 seconds (unchanged)
                    processing_queue.flush_batches,
                    error_backoff_base=0.5,
                    error_backoff_max=30,
                    jitter=0,
                ),
                name="batch_flusher",
            )
            logger.info("  ✅ Queue batch flusher started")
            initialized_components.append("batch_flusher")
        except Exception as e:
            logger.error(f"  ❌ Batch flusher failed: {e}")
            # Non-critical

        # 3.6.1 ML drift monitor (REPORT-ONLY — never triggers deployment or
        # retraining; interval live-tunable; startup delay avoids competing
        # with boot). Non-critical: RULES mode needs none of this.
        logger.info("  🔄 Starting ML drift monitor...")
        try:
            from backend.core.service_supervisor import supervised_loop as _supervised_loop
            from backend.ml.drift_service import drift_service as _drift_service

            def _drift_interval():
                hours = float(settings.ML_DRIFT_CHECK_INTERVAL_HOURS or 24)
                return max(3600.0, hours * 3600.0)

            ml_drift_task = asyncio.create_task(
                _supervised_loop(
                    "ml_drift_monitor",
                    _drift_interval,
                    _drift_service.scheduled_check,
                    initial_delay=600.0,
                ),
                name="ml_drift_monitor",
            )
            logger.info("  ✅ ML drift monitor started (report-only)")
            initialized_components.append("ml_drift_monitor")
        except Exception as e:
            logger.error(f"  ❌ ML drift monitor failed: {e}")
            # Non-critical

        # 3.7 Event-loop lag monitor: measures how late a 1s sleep wakes up.
        # Lag > 0.5s means something is blocking the loop — exactly the failure
        # mode behind the 499/502 outages — so it's exported as a gauge and
        # WARN-logged with the drift so it's visible without Prometheus.
        logger.info("  🔄 Starting event-loop lag monitor...")
        try:
            async def _lag_probe_cycle():
                from backend.core import metrics as _metrics
                t0 = asyncio.get_event_loop().time()
                await asyncio.sleep(1.0)
                lag = max(0.0, asyncio.get_event_loop().time() - t0 - 1.0)
                try:
                    if _metrics.metrics_event_loop_lag is not None:
                        _metrics.metrics_event_loop_lag.set(lag)
                except Exception:
                    pass
                if lag > 0.5:
                    logger.warning(f"[LOOP-LAG] ⚠️ Event loop blocked for {lag:.2f}s — something is running sync work on the loop")

            from backend.core.service_supervisor import supervised_loop as _supervised_loop2
            # interval=0: the probe's own 1s sleep IS the cadence; adding an
            # interval sleep would halve the sampling rate.
            loop_lag_task = asyncio.create_task(
                _supervised_loop2("loop_lag_monitor", 0, _lag_probe_cycle,
                                  error_backoff_base=1, error_backoff_max=30, jitter=0),
                name="loop_lag_monitor",
            )
            logger.info("  ✅ Event-loop lag monitor started")
            initialized_components.append("loop_lag_monitor")
        except Exception as e:
            logger.error(f"  ❌ Event-loop lag monitor failed: {e}")
            # Non-critical

        # ==================== PHASE 4: Worker Pool ====================
        logger.info("👷 Phase 4: Starting Worker Pool...")

        worker_count = settings.QUEUE_WORKERS

        for i in range(worker_count):
            try:
                worker_task = asyncio.create_task(queue_worker(i + 1))
                worker_tasks.append(worker_task)
                logger.info(f"  ✅ Worker {i + 1}/{worker_count} started")
            except Exception as e:
                logger.error(f"  ❌ Worker {i + 1} failed: {e}")

        if worker_tasks:
            initialized_components.append(f"workers_{len(worker_tasks)}")

        # ==================== PHASE 5: Health Verification ====================
        logger.info("🏥 Phase 5: Health Verification...")

        health_status = {}

        # Database health
        try:
            db_healthy = await db_manager.health_check()
            health_status["database"] = db_healthy
            logger.info(f"  {'✅' if db_healthy else '❌'} Database: {'Healthy' if db_healthy else 'Unhealthy'}")
        except Exception as e:
            health_status["database"] = False
            logger.error(f"  ❌ Database health check failed: {e}")

        # Models health
        try:
            models_healthy = model_manager.health_check()
            health_status["models"] = models_healthy
            logger.info(f"  {'✅' if models_healthy else '❌'} Models: {'Healthy' if models_healthy else 'Unhealthy'}")
        except Exception as e:
            health_status["models"] = False
            logger.error(f"  ❌ Models health check failed: {e}")

        # Redis health (if enabled)
        if CACHE_ENABLED:
            try:
                redis_healthy = await cache_manager.health_check()
                health_status["redis"] = redis_healthy
                logger.info(f"  {'✅' if redis_healthy else '❌'} Redis: {'Healthy' if redis_healthy else 'Unhealthy'}")
            except Exception as e:
                health_status["redis"] = False
                logger.error(f"  ❌ Redis health check failed: {e}")

        # Queue health
        try:
            queue_stats = await processing_queue.get_stats()
            queue_healthy = True  # Just check if we can get stats
            health_status["queue"] = queue_healthy
            logger.info(f"  ✅ Queue: Healthy (size: {queue_stats['queue_size']})")
        except Exception as e:
            health_status["queue"] = False
            logger.error(f"  ❌ Queue health check failed: {e}")

        # Create the first administrator, if the deployment has none yet.
        # The credential comes from configuration (preferably a Docker secret
        # file) and is never logged.
        try:
            from backend.services.bootstrap_admin import (
                BootstrapAdminError,
                ensure_bootstrap_admin,
            )

            outcome = await ensure_bootstrap_admin(db_manager)
            logger.info("  ✅ Bootstrap administrator: %s", outcome.value)
        except BootstrapAdminError as e:
            logger.error("  ❌ Bootstrap administrator requirement not satisfied: %s", e)
            raise
        except Exception as e:
            logger.error("  ❌ Bootstrap administrator check failed: %s: %s", type(e).__name__, e)
            if settings.is_production:
                raise

        # SQL Agent initialization
        global sql_agent_instance, SQL_AGENT_AVAILABLE, SQL_AGENT_ROUTER_AVAILABLE, set_sql_agent_instance
        sql_agent_instance = None
        
        # Try to import SQL Agent
        try:
            from sql_agent.agent import SQLIntelligenceAgent
            from sql_agent.conversation_memory import ConversationMemory
            from sql_agent.api import set_sql_agent_instance as set_instance
            
            SQL_AGENT_AVAILABLE = True
            SQL_AGENT_ROUTER_AVAILABLE = True
            set_sql_agent_instance = set_instance
            
            logger.info("  🔄 Initializing SQL Intelligence Agent...")
            # No storage_dir argument: the default derives from STORAGE_DIR
            # (settings.CONVERSATION_CACHE_DIR), which the storage volume
            # persists. The old literal "conversation_cache" pinned the
            # global agent's memory to the container's writable layer, where
            # every --force-recreate erased it.
            memory = ConversationMemory()
            session_id = memory.start_session()
            sql_agent_instance = SQLIntelligenceAgent(conversation_memory=memory)
            logger.info(f"  ✅ SQL Agent initialized (session: {session_id})")
            initialized_components.append("sql_agent")
            
            # Set the instance in the API router
            if set_sql_agent_instance:
                set_sql_agent_instance(sql_agent_instance, True)
                logger.info("  ✅ SQL Agent instance set in API router")
            
            # Test database connection
            if sql_agent_instance.test_connection():
                logger.info("  ✅ SQL Agent database connection verified")
            else:
                logger.warning("  ⚠️ SQL Agent database connection failed")
        except Exception as e:
            logger.info("  ⏭️ SQL Agent skipped (not available)")
            SQL_AGENT_AVAILABLE = False
            SQL_AGENT_ROUTER_AVAILABLE = False
            sql_agent_instance = None
            set_sql_agent_instance = None
            if 'set_instance' in locals():
                set_instance(None, False)

        # Check critical components
        critical_healthy = all([
            health_status.get("database", False),
            health_status.get("models", False)
        ])

        if critical_healthy:
            startup_success = True
            startup_duration = time.time() - startup_time

            logger.info("=" * 70)
            logger.info(f"✅ Service started successfully in {startup_duration:.2f}s")
            logger.info("=" * 70)

            # Emit startup metrics
            metrics_queue_size.set(processing_queue.queue.qsize())

        else:
            logger.error("=" * 70)
            logger.error("❌ Critical components unhealthy - service may not function correctly")
            logger.error("=" * 70)
            # We'll still start but log warning

    except Exception as startup_error:
        logger.error(f"🚨 Startup failed: {startup_error}")
        logger.error("Stack trace:", exc_info=True)

        # Emergency shutdown of initialized components
        await _emergency_shutdown(initialized_components, worker_tasks)

        raise startup_error

    # ==================== RUNNING PHASE ====================
    try:
        # Service is now running
        yield

    finally:
        # ==================== SHUTDOWN PHASE ====================
        shutdown_time = time.time()
        logger.info("=" * 70)
        logger.info("🛑 Starting graceful shutdown...")
        # Deterministic shutdown context, one structured line. This exists
        # because the 2026-07-28 "mystery shutdowns" were a planned
        # max_requests recycle that nothing attributed: the shutdown sequence
        # logged WHAT stopped but never WHO asked, HOW LONG the process had
        # lived, or what state it was in. psutil-free so it can never fail
        # the shutdown path.
        try:
            import os as _os
            import threading as _threading
            _uptime_s = int(time.time() - startup_time)
            _rss_mb = None
            try:
                with open("/proc/self/status") as _f:
                    for _line in _f:
                        if _line.startswith("VmRSS"):
                            _rss_mb = int(_line.split()[1]) // 1024
                            break
            except OSError:
                pass  # non-Linux dev host
            logger.info(
                "shutdown_context pid=%s ppid=%s uptime_s=%s rss_mb=%s "
                "active_tasks=%s threads=%s "
                "(cause is logged by the process manager: look for a gunicorn "
                "'Worker exiting'/'ABORTED'/max-requests line just above)",
                _os.getpid(), _os.getppid(), _uptime_s, _rss_mb,
                len(asyncio.all_tasks()), _threading.active_count(),
            )
        except Exception:  # pragma: no cover — instrumentation must never block shutdown
            pass
        logger.info("=" * 70)

        shutdown_results = {}

        # Stop components in REVERSE order of initialization
        components_to_stop = [
            ("workers", "Stopping worker pool", lambda: worker_tasks),
            ("cache_metrics", "Stopping cache metrics", lambda: cache_metrics_task if 'cache_metrics' in initialized_components else None),
            # These three were STARTED but never stopped here — the audit's
            # missing stop-list entries. loop_lag/batch_flusher are real tasks
            # (cancel + await); task_notifier owns no task (verified) and is
            # listed so the stop list is exhaustive in code, not just in prose.
            ("loop_lag_monitor", "Stopping event-loop lag monitor", lambda: loop_lag_task if 'loop_lag_monitor' in initialized_components else None),
            ("batch_flusher", "Stopping queue batch flusher", lambda: batch_flush_task if 'batch_flusher' in initialized_components else None),
            ("task_notifier", "Releasing background task notifier", lambda: "stateless" if 'task_notifier' in initialized_components else None),
            ("metrics_collector", "Stopping metrics collector", lambda: metrics_collector if 'metrics_collector' in initialized_components else None),
            ("log_cleanup", "Stopping log cleanup", lambda: log_cleanup_manager if 'log_cleanup' in initialized_components else None),
            ("retention_manager", "Stopping data retention", lambda: retention_manager if 'retention_manager' in initialized_components else None),
            ("identity_retention", "Stopping identity retention", lambda: identity_retention_manager if 'identity_retention' in initialized_components else None),
            ("identity_clustering", "Stopping identity clustering", lambda: clustering_service if 'identity_clustering' in initialized_components else None),
            ("vector_index", "Saving vector index snapshot", lambda: vector_index_manager if 'vector_index' in initialized_components else None),
            ("batch_writer", "Stopping batch writer", lambda: batch_writer if 'batch_writer' in initialized_components else None),
            ("face_tracker", "Stopping face tracker", lambda: face_tracker if 'face_tracker' in initialized_components else None),
            ("production_cache", "Stopping production cache", lambda: production_cache_manager if 'production_cache' in initialized_components else None),
            ("websocket_redis", "Stopping WebSocket Redis pub/sub", lambda: ws_manager if 'websocket_redis' in initialized_components else None),
            ("redis_cache_service", "Closing Redis cache service", lambda: redis_cache_service if 'redis_cache_service' in initialized_components else None),
            ("redis_cache", "Closing Redis cache", lambda: cache_manager if 'redis_cache' in initialized_components else None),
            ("models", "Cleaning up models", lambda: model_manager if 'models' in initialized_components else None),
            ("database", "Closing database", lambda: db_manager if 'database' in initialized_components else None),
        ]

        for component_name, description, get_component in components_to_stop:
            try:
                if get_component is None:
                    logger.info(f"  📝 {description}")
                    shutdown_results[component_name] = "skipped"
                    continue

                component = get_component()
                if not component:
                    logger.info(f"  ⚠️  {description} (not initialized)")
                    shutdown_results[component_name] = "not_initialized"
                    continue

                logger.info(f"  🔄 {description}...")
                start = time.time()
                
                # Vector index: snapshot on the way out. Off-loop (serializing
                # 100k vectors writes 201 MB) and BOUNDED — abandoning a timed-out
                # save is safe because the snapshot commits through a single
                # atomic pointer swap, so on-disk state is always the last
                # COMPLETE snapshot, and PostgreSQL can rebuild it regardless.
                if component_name == "vector_index" and component:
                    try:
                        result = await asyncio.wait_for(
                            component.save_once(trigger="shutdown"), timeout=45.0)
                        logger.info(f"    ✅ Vector index snapshot: {result}")
                        shutdown_results[component_name] = "saved"
                    except asyncio.TimeoutError:
                        logger.error(
                            "    ❌ Snapshot timed out after 45s — the previous "
                            "snapshot remains valid and PostgreSQL is authoritative")
                        shutdown_results[component_name] = "timeout"
                    except Exception as e:
                        logger.error(f"    ❌ Failed to snapshot vector index: {e}")
                        shutdown_results[component_name] = f"error: {e}"
                    continue

                if component_name == "workers":
                    # Cancel all worker tasks
                    for worker_task in component:
                        worker_task.cancel()

                    # Wait for workers to finish (with timeout)
                    try:
                        await asyncio.wait_for(
                            asyncio.gather(*component, return_exceptions=True),
                            timeout=15.0
                        )
                        logger.info(f"    ✅ Workers stopped")
                    except asyncio.TimeoutError:
                        logger.warning(f"    ⚠️  Workers shutdown timeout")
                    except Exception as e:
                        logger.error(f"    ❌ Workers shutdown error: {e}")

                elif component_name in ("cache_metrics", "loop_lag_monitor", "batch_flusher"):
                    # Bare-task components: cancel + await, bounded.
                    component.cancel()
                    try:
                        await asyncio.wait_for(component, timeout=5.0)
                    except (asyncio.CancelledError, asyncio.TimeoutError):
                        pass

                elif component_name == "task_notifier":
                    # Stateless (no task to cancel) — recorded so the stop
                    # list accounts for every started component.
                    shutdown_results[component_name] = "skipped (stateless)"
                    continue

                elif component_name == "redis_cache_service":
                    from backend.core.redis_cache import redis_cache_service
                    await redis_cache_service.close()
                    logger.info(f"    ✅ Redis cache service closed")
                    shutdown_results[component_name] = "stopped"
                    continue
                
                elif component_name == "websocket_redis":
                    from backend.core.websocket_manager import ws_manager
                    if ws_manager._redis_listener_task and not ws_manager._redis_listener_task.done():
                        ws_manager._redis_listener_task.cancel()
                        try:
                            await ws_manager._redis_listener_task
                        except asyncio.CancelledError:
                            pass
                    if ws_manager.redis_pubsub:
                        await ws_manager.redis_pubsub.unsubscribe()
                    if ws_manager.redis_client:
                        await ws_manager.redis_client.close()
                    logger.info(f"    ✅ WebSocket Redis pub/sub stopped")
                    shutdown_results[component_name] = "stopped"
                    continue

                elif hasattr(component, 'stop'):
                    # Components with async stop() method — BOUNDED: one hung
                    # stop() used to stall the entire shutdown.
                    result = await _bounded_stop(component_name, component.stop())
                    if result != "success":
                        shutdown_results[component_name] = result
                        continue
                    logger.info(f"    ✅ Stopped in {(time.time() - start):.2f}s")

                elif hasattr(component, 'close'):
                    # Components with async close() method — bounded likewise.
                    result = await _bounded_stop(component_name, component.close())
                    if result != "success":
                        shutdown_results[component_name] = result
                        continue
                    logger.info(f"    ✅ Closed in {(time.time() - start):.2f}s")

                elif component_name == "models":
                    # Models cleanup (synchronous)
                    # No special cleanup needed, Python GC will handle
                    logger.info(f"    ✅ Cleaned up in {(time.time() - start):.2f}s")

                shutdown_results[component_name] = "success"

            except Exception as e:
                logger.error(f"  ❌ Failed to stop {component_name}: {e}")
                shutdown_results[component_name] = f"error: {str(e)}"
                # Continue shutdown even if one component fails

        timed_out = sorted(n for n, r in shutdown_results.items() if r == "timeout")
        if timed_out:
            logger.warning("shutdown_timeouts: %s", timed_out)

        # Final flush of any pending batch writes
        if 'batch_writer' in initialized_components and hasattr(batch_writer, 'pending_detections'):
            pending_count = len(batch_writer.pending_detections)
            if pending_count > 0:
                logger.info(f"  🔄 Flushing {pending_count} pending batch writes...")
                try:
                    await batch_writer.flush()
                    logger.info(f"    ✅ Batch writes flushed")
                except Exception as e:
                    logger.error(f"    ❌ Batch flush failed: {e}")

        # Cancel-then-await for any remaining async tasks.
        #
        # The previous order was inverted: it WAITED 10 seconds hoping tasks
        # would finish on their own (periodic loops never do — they sleep), and
        # only then cancelled, without awaiting the cancellations. Every
        # shutdown therefore stalled exactly 10s (visible in the logs as
        # "Waiting for application shutdown" -> "Worker exiting" 10s apart),
        # logged the anonymous "3 tasks still pending", and exited before the
        # cancelled tasks' cleanup ran. Cancelling FIRST turns the same tasks
        # into immediate CancelledError exits, and awaiting them lets their
        # finally-blocks complete.
        logger.info("  🔄 Cleaning up remaining tasks...")
        try:
            pending_tasks = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
            if pending_tasks:
                logger.info(f"    ⏳ Cancelling {len(pending_tasks)} remaining tasks...")
                for task in pending_tasks:
                    task.cancel()
                done, pending = await asyncio.wait(pending_tasks, timeout=5.0)
                if pending:
                    # NAME the survivors — an anonymous count is undebuggable.
                    # A task that survives cancel+await is either shielded or
                    # stuck in un-cancellable blocking code; the name says
                    # which service owns it.
                    survivors = [
                        f"{t.get_name()}:{getattr(t.get_coro(), '__qualname__', repr(t.get_coro())[:60])}"
                        for t in pending
                    ]
                    logger.warning(
                        "    ⚠️  %d tasks survived cancellation after 5s: %s",
                        len(pending), ", ".join(survivors),
                    )
        except Exception as e:
            logger.error(f"    ❌ Task cleanup failed: {e}")

        shutdown_duration = time.time() - shutdown_time

        logger.info("=" * 70)
        logger.info(f"✅ Shutdown completed in {shutdown_duration:.2f}s")
        logger.info("=" * 70)

        # Shutdown summary
        successful = sum(1 for r in shutdown_results.values() if r in ["success", "skipped", "not_initialized"])
        total = len(shutdown_results)

        logger.info("📊 Shutdown Summary:")
        logger.info(f"  • Components: {successful}/{total} successful")
        logger.info(f"  • Duration: {shutdown_duration:.2f}s")


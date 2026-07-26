# Settings Runtime-Consumer Matrix

**Updated: 2026-07-25 (settings-system overhaul).** This documents, for every operationally
important setting, where it is consumed at runtime and what a save on the admin
Settings page really does (`apply_mode`).

## How the system works now

```
Admin PUT /api/settings/{key}
  └─ typed validation (type / min / max / enum)      backend/core/runtime_settings.py
  └─ persist to `settings` table (NEVER reverted — sync is seed-only)
  └─ if apply_mode is dynamic: setattr(config.settings, KEY, value)  ← consumers read per call
  └─ honest response: {applied, apply_mode, restart_required, effective_value}

Startup (backend/lifespan.py)
  └─ hydrate_from_db(): admin-modified DB values re-applied → settings SURVIVE restarts
```

**Value precedence:** admin-modified DB value → environment → default.
A DB row equal to the env value is just a seeded mirror. When an admin value
diverges from the environment, the API exposes `overridden: true` + `env_value`.

**Multi-worker caveat:** apply mutates the current process only. This deployment
runs `WORKERS=1`, so that is complete. Do not raise WORKERS without adding a
cross-process signal (pub/sub) to `runtime_settings.apply_to_runtime`.

## Dynamic settings (save → behavior changes without restart)

| Key | Consumer (file) | Read timing | apply_mode |
|---|---|---|---|
| SIMILARITY_THRESHOLD | identity_service.known_threshold (property) → pgvector search | per call | immediate |
| UNKNOWN_SIMILARITY_THRESHOLD (new) | identity_service.unknown_threshold (property) | per call | immediate |
| SAVE_IMAGES / SAVE_UNKNOWN_FACES / SKIP_UNKNOWN_FACES / SAVE_CROPPED_IMAGES | image_processing.py | per call | immediate |
| SAVE_WEBHOOK_IMAGES | queue_worker.py | per call | immediate |
| MAX_PHOTOS_PER_PERSON | image_processing.py | per call | immediate |
| DASHBOARD_FACE_DISPLAY_HOURS | stats.py, websocket.py | per call | immediate |
| ALERT_NOTIFICATION_WINDOW_HOURS | stats.py + face_tracker.frontend_notification_window (property) | per call | immediate |
| CACHE_TTL | cache_manager.py (per set) | per call | immediate |
| MAX_STORAGE_GB | data_retention.get_storage_stats | per call | immediate |
| BATCH_SEARCH_MAX_IMAGES / _TIMEOUT_SECONDS | batch_search_service, batch_export | per call | immediate |
| IDENTITY_ENRICH_MIN_SIMILARITY / _MIN_QUALITY / IDENTITY_MAX_EMBEDDINGS | identity_service enrichment | per call | immediate |
| WEBHOOK_MAX_BODY_MB / WEBHOOK_DEDUP_TTL_SECONDS | webhook.py | per request | next_request |
| DATA_RETENTION_DAYS / CLEANUP_INTERVAL_HOURS | data_retention.py (properties, read at run start / each loop) | per job run | next_job_run |
| LOGS_LIFE_TIME_HOURS | log_cleanup.py (property) | per job run | next_job_run |
| SNAPSHOT_RETENTION_DAYS / INACTIVE_THRESHOLD_DAYS / IDENTITY_CLEANUP_INTERVAL_HOURS / MAX_EMBEDDINGS_PER_IDENTITY | identity_retention.py (properties) | per job run | next_job_run |
| SEARCH_HISTORY_RETENTION_DAYS / _MAX_PER_USER | data_retention._cleanup_auxiliary (NEW — was documented but never enforced) | per job run | next_job_run |
| AUDIT_LOG_RETENTION_DAYS (new) | data_retention._cleanup_auxiliary (chatbot/identity/settings audit tables) | per job run | next_job_run |

## Restart-required settings (saved + honestly labeled; cannot be dynamic)

| Key group | Why it can't be live | apply_mode |
|---|---|---|
| CONFIDENCE_THRESHOLD | bound at SCRFD model load (model_manager.py) | api_restart |
| QUEUE_WORKERS / MAX_QUEUE_SIZE / MAX_CONCURRENT_REQUESTS | worker tasks + queue sized at startup (lifespan, processing_queue) | api_restart |
| INFERENCE_WORKERS / MAX_CONCURRENT_INFERENCE(_PER_PIPELINE) | thread pool + semaphores created at import (image_processing) | api_restart |
| BATCH_WRITE_* / PIPELINE_BATCH_SIZE | batch writer/queue singletons | api_restart |
| SQL_AGENT_MAX_CONCURRENT / _TOTAL_TIMEOUT | semaphore/constants captured at module import (sql_agent/api/routes.py) | api_restart |
| LOG_LEVEL / LOG_DIR | logging configured at process start (utils/logging.py) | api_restart |
| VECTOR_BACKEND / PGVECTOR_HNSW_EF_SEARCH | index backend chosen at startup | api_restart |
| PGVECTOR_INDEX_TYPE / _HNSW_M / _HNSW_EF_CONSTRUCTION / _IVFFLAT_LISTS | baked into the built index structure | index_rebuild |
| WORKERS / HOST / PORT / DB_* / REDIS_* / DATABASE_URL | container/bind/connection level (docker-compose env) | container_recreate |

All other non-curated keys get auto-derived metadata (type/default from
`config.Settings.model_fields`) and are labeled `api_restart`
(`container_recreate` for the server/database/cache categories).

## Retention endpoints

```
GET  /api/admin/retention/status          stored/effective/source per key, last run, next run, lock state
POST /api/admin/retention/run?dry_run=true    count candidates — deletes NOTHING
POST /api/admin/retention/run?dry_run=false   real run (advisory-lock guarded, 409 if running)
```

Retention run safety: Postgres advisory lock + in-process lock (no overlap),
batched deletes (1000/batch), file deletion in an executor with a path-traversal
guard (must be under STORAGE_DIR, never FACES_DIR), 24h safety floor (records
newer than 1 day are never deleted regardless of configuration), failures
recorded in the structured result instead of being swallowed.

## Verify commands

```bash
# typed metadata + no-store
curl -s -H "Authorization: Bearer $TOKEN" http://localhost/api/settings/DATA_RETENTION_DAYS | jq

# live-threshold proof (recognition changes immediately)
# PUT SIMILARITY_THRESHOLD=0.99 → send an enrolled face → log shows "Below threshold ... (threshold 0.99)"
# PUT back 0.4 → same face → "KNOWN identity recognized"

# retention
curl -s -H "Authorization: Bearer $TOKEN" http://localhost/api/admin/retention/status | jq
curl -s -X POST -H "Authorization: Bearer $TOKEN" "http://localhost/api/admin/retention/run?dry_run=true" | jq

# full suite
docker exec face_recognition_api python -m pytest tests/ -q
```

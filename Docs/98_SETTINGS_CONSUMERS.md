# 98 — Settings page: who consumes each setting

Generated 2026-09-06 from the live settings page (364 settings) and a scan of the
repository: Python attribute reads (`settings.KEY`), by-name reads (`getattr(settings, "KEY")`, lookup tables,
`_seconds("KEY", …)`), the `sql_agent.config` mirror, Docker compose files, env templates, Dockerfiles and shell scripts.
**In use** is the value the running API reports. **Applied** is when a change takes effect
(`immediate` / `next_request` / `next_job_run` live; `api_restart`, `worker_restart`, `container_recreate` need a
restart; `index_rebuild` needs the vector index rebuilt).

Regenerate: `python scripts/settings_consumers.py --token <admin JWT>`.


## advanced

| Setting | In use | Applied | Python consumers | Docker / env / shell |
|---|---|---|---|---|
| `ANOMALY_BASELINE_MAX_DAYS` | 365 | immediate | backend/core/security_intelligence_service.py | .env.example |
| `ANOMALY_DEVIATION_SIGMA` | 2.0 | immediate | backend/core/security_intelligence_service.py<br>backend/ml/feature_builders.py | — |
| `ANOMALY_HOLIDAYS` |  | immediate | backend/core/time_context.py | .env.example |
| `ANOMALY_MAX_ITEMS` | 200 | immediate | backend/core/security_intelligence_service.py | — |
| `ANOMALY_MIN_BASELINE_SAMPLES` | 5 | immediate | backend/core/security_intelligence_service.py<br>backend/ml/feature_builders.py | — |
| `ANOMALY_MIN_STD_HOURS` | 0.75 | immediate | backend/core/security_intelligence_service.py<br>backend/ml/feature_builders.py | — |
| `API_DEFAULT_PAGE_SIZE` | 25 | immediate | backend/utils/pagination.py | — |
| `API_MAX_PAGE_SIZE` | 100 | immediate | backend/utils/pagination.py | — |
| `API_RATE_LIMIT_ENABLED` | True | immediate | backend/core/rate_limiter.py | .env.example |
| `ASSESSMENT_DEDUP_WINDOW_MINUTES` | 5 | immediate | backend/core/assessment_service.py<br>backend/ml/inference_service.py | .env.example |
| `AUTO_THRESHOLD_LEARNING_ENABLED` | True | immediate | backend/core/threshold_learner.py | — |
| `DEFAULT_SITE_TIMEZONE` | Asia/Beirut | immediate | backend/core/security_intelligence_service.py<br>backend/core/time_context.py<br>backend/ml/feature_store.py | .env.example |
| `INTEL_QUERY_TIMEOUT_SECONDS` | 30.0 | immediate | backend/routes/intelligence.py | — |
| `ML_EVIDENCE_MIN_REVIEWED_PER_BAND` | 0 | immediate | backend/ml/evidence_stats.py (by name)<br>backend/ml/readiness.py<br>backend/ml/readiness.py (by name) | — |
| `ML_EVIDENCE_MIN_REVIEWED_TOTAL` | 0 | immediate | backend/ml/evidence_stats.py (by name)<br>backend/ml/readiness.py<br>backend/ml/readiness.py (by name) | — |
| `ML_JOB_HEARTBEAT_SECONDS` | 10.0 | worker_restart | backend/ml/worker.py (by name) | .env.example |
| `ML_JOB_LEASE_SECONDS` | 60 | worker_restart | backend/ml/worker.py (by name)<br>backend/routes/ml_ops.py | .env.example |
| `ML_JOB_POLL_SECONDS` | 2.0 | worker_restart | backend/ml/worker.py (by name) | .env.example |
| `ML_JOB_TERMINATE_GRACE_SECONDS` | 15.0 | worker_restart | backend/ml/worker.py (by name) | .env.example |
| `ML_SCIENTIFIC_MIN_HISTORY_DAYS` | 0 | immediate | backend/ml/readiness.py<br>backend/ml/readiness.py (by name) | — |
| `ML_SCIENTIFIC_MIN_MEDIAN_APPEARANCES` | 0 | immediate | backend/ml/readiness.py<br>backend/ml/readiness.py (by name) | — |
| `MULTI_CAMERA_CO_APPEARANCE_ENABLED` | True | immediate | backend/core/intelligence_service.py | — |
| `MULTI_CAMERA_DISTANCE_METERS` | 500.0 | immediate | backend/core/activity_correlation.py<br>backend/core/intelligence_service.py<br>backend/core/threshold_learner.py<br>backend/routes/risk_assessments.py | — |
| `MULTI_CAMERA_MIN_CO_APPEARANCES` | 2 | immediate | backend/core/intelligence_service.py | — |
| `MULTI_CAMERA_TIME_WINDOW_MINUTES` | 10 | immediate | backend/core/activity_correlation.py<br>backend/core/intelligence_service.py<br>backend/core/threshold_learner.py<br>backend/routes/risk_assessments.py | — |
| `NETWORK_FALLBACK_MAX_IDENTITIES` | 200 | immediate | backend/core/security_intelligence_service.py | — |
| `PATTERN_MAX_PER_TYPE` | 100 | immediate | backend/core/security_intelligence_service.py | — |
| `PATTERN_OFF_HOURS_END` | 5 | immediate | backend/core/security_intelligence_service.py<br>backend/ml/feature_builders.py | — |
| `PATTERN_OFF_HOURS_START` | 2 | immediate | backend/core/security_intelligence_service.py<br>backend/ml/feature_builders.py | — |
| `PATTERN_RAPID_MIN_SPEED_KMH` | 15.0 | immediate | backend/core/security_intelligence_service.py | — |
| `PATTERN_RAPID_WINDOW_SECONDS` | 300 | immediate | backend/core/security_intelligence_service.py | — |
| `PATTERN_SCAN_LIMIT` | 50000 | immediate | backend/core/security_intelligence_service.py | — |
| `PGVECTOR_HNSW_EF_SEARCH` | 100 | api_restart | backend/core/identity_index_pgvector.py | — |
| `RATE_LIMIT_DEFAULT_PER_MINUTE` | 300 | immediate | backend/core/rate_limiter.py | .env.example |
| `RATE_LIMIT_HEAVY_PER_MINUTE` | 60 | immediate | backend/core/rate_limiter.py | .env.example |
| `SQL_AGENT_MAX_MODEL_CALLS` | 24 | api_restart | sql_agent/run_control.py | .env.example |
| `SQL_AGENT_MAX_RUN_TOKENS` | 65536 | api_restart | sql_agent/llm/gateway.py<br>sql_agent/run_control.py | .env.example |
| `SQL_AGENT_MAX_TOOL_CALLS` | 12 | api_restart | sql_agent/run_control.py | .env.example |
| `SQL_AGENT_MEMORY_RETENTION_DAYS` | 30 | api_restart | sql_agent/memory_policy.py | .env.example |
| `THRESHOLD_MIN_SAMPLES_FOR_ACTIVATION` | 10 | immediate | backend/core/threshold_learner.py<br>backend/routes/risk_assessments.py | .env.example |
| `TRAJECTORY_PREDICTION_ENABLED` | True | immediate | backend/core/trajectory_predictor.py<br>backend/routes/intelligence.py<br>backend/routes/intelligence.py (by name) | — |
| `WEEKEND_DAYS` | 5,6 | immediate | backend/core/time_context.py | .env.example |

## advanced_search

| Setting | In use | Applied | Python consumers | Docker / env / shell |
|---|---|---|---|---|
| `BATCH_SEARCH_ENABLED` | True | immediate | backend/routes/batch_export.py | — |
| `BATCH_SEARCH_MAX_CONCURRENCY` | 5 | immediate | backend/core/batch_search_service.py | — |
| `BATCH_SEARCH_MAX_IMAGES` | 20 | immediate | backend/core/batch_search_service.py<br>backend/routes/advanced_search.py<br>backend/routes/batch_export.py | — |
| `BATCH_SEARCH_TIMEOUT_SECONDS` | 300 | immediate | backend/core/batch_search_service.py<br>backend/routes/advanced_search.py<br>backend/routes/batch_export.py | — |
| `CONFIDENCE_HIGH_MIN` | 0.75 | immediate | backend/core/advanced_search.py<br>backend/routes/advanced_search.py | — |
| `CONFIDENCE_LOW_MIN` | 0.4 | immediate | backend/core/advanced_search.py<br>backend/routes/advanced_search.py | — |
| `CONFIDENCE_MEDIUM_MIN` | 0.6 | immediate | backend/core/advanced_search.py<br>backend/routes/advanced_search.py | — |
| `CONFIDENCE_VERY_HIGH_MIN` | 0.9 | immediate | backend/core/advanced_search.py<br>backend/routes/advanced_search.py | — |
| `CROSS_CAMERA_TRACKING_ENABLED` | True | immediate | backend/routes/intelligence.py<br>backend/routes/intelligence.py (by name) | — |
| `EXPORT_RESULTS_ENABLED` | True | immediate | backend/routes/batch_export.py | — |
| `FACE_QUALITY_ENABLED` | True | api_restart | backend/core/face_quality.py | — |
| `FACE_QUALITY_THRESHOLD_ANGLE` | 30.0 | immediate | backend/core/face_quality.py | — |
| `FACE_QUALITY_THRESHOLD_BLUR` | 0.5 | immediate | backend/core/face_quality.py | — |
| `FACE_QUALITY_THRESHOLD_LIGHTING` | 0.5 | immediate | backend/core/face_quality.py | — |
| `FACE_QUALITY_THRESHOLD_SIZE` | 50 | immediate | backend/core/face_quality.py | — |
| `LIVE_ALERTS_ENABLED` | True | api_restart | backend/core/detection_evidence.py | — |
| `LIVE_ALERT_CLIP_DURATION_SECONDS` | 60 | immediate | backend/core/live_alert_service.py | — |
| `LIVE_ALERT_DEFAULT_COOLDOWN_MINUTES` | 30 | immediate | backend/core/live_alert_service.py | — |
| `LIVE_ALERT_MAX_PER_IDENTITY` | 5 | immediate | backend/core/live_alert_service.py | — |
| `LIVE_ALERT_MAX_PER_USER` | 50 | immediate | backend/core/live_alert_service.py | — |
| `LIVE_ALERT_MIN_SIMILARITY` | 0.75 | immediate | backend/core/live_alert_service.py | — |
| `NEGATIVE_SEARCH_ENABLED` | True | immediate | backend/routes/advanced_search.py | — |
| `RELATED_IDENTITIES_ENABLED` | True | immediate | backend/routes/intelligence.py<br>backend/routes/intelligence.py (by name) | — |
| `RELATED_IDENTITY_MIN_CO_APPEARANCES` | 3 | immediate | backend/core/intelligence_service.py<br>backend/core/security_intelligence_service.py | — |
| `RELATED_IDENTITY_TIME_WINDOW_MINUTES` | 30 | immediate | backend/core/intelligence_service.py<br>backend/core/security_intelligence_service.py<br>backend/ml/feature_builders.py | — |
| `SEARCH_CANDIDATE_MULTIPLIER` | 2 | immediate | backend/core/advanced_search.py | — |
| `SEARCH_DEFAULT_TOP_K` | 10 | immediate | backend/core/batch_search_service.py<br>backend/core/identity_index_pgvector.py<br>backend/routes/advanced_search.py<br>backend/routes/batch_export.py | — |
| `SEARCH_FILTERED_CANDIDATE_MULTIPLIER` | 6 | immediate | backend/core/advanced_search.py | — |
| `SEARCH_HISTORY_MAX_PER_USER` | 1000 | next_job_run | backend/core/data_retention.py<br>backend/routes/advanced_search.py<br>backend/routes/retention.py (by name) | — |
| `SEARCH_HISTORY_RETENTION_DAYS` | 90 | next_job_run | backend/core/data_retention.py<br>backend/routes/advanced_search.py<br>backend/routes/retention.py (by name) | — |
| `SEARCH_MAX_TOP_K` | 100 | immediate | backend/routes/advanced_search.py<br>backend/routes/batch_export.py | — |
| `SEARCH_MIN_QUALITY_THRESHOLD` | 0.3 | immediate | backend/core/advanced_search.py<br>backend/core/face_quality.py<br>backend/routes/advanced_search.py | — |
| `SEARCH_QUALITY_WARNING_THRESHOLD` | 0.6 | immediate | backend/core/advanced_search.py<br>backend/core/face_quality.py<br>backend/routes/advanced_search.py | — |
| `SEARCH_RETRIEVAL_FLOOR` | 0.2 | immediate | backend/core/advanced_search.py | — |
| `SMS_PROVIDER_URL` |  | immediate | backend/routes/live_alerts.py | — |
| `SMTP_HOST` |  | immediate | backend/routes/live_alerts.py | — |
| `TEMPORAL_PATTERNS_ENABLED` | True | immediate | backend/routes/intelligence.py<br>backend/routes/intelligence.py (by name) | — |
| `TWILIO_ACCOUNT_SID` |  | immediate | backend/routes/live_alerts.py | — |
| `WATCHLIST_ENABLED` | True | immediate | backend/core/detection_evidence.py | — |

## cache

| Setting | In use | Applied | Python consumers | Docker / env / shell |
|---|---|---|---|---|
| `CACHE_TTL` | 3600 | immediate | backend/core/cache_manager.py<br>backend/core/redis_cache.py<br>backend/routes/websocket.py | .env (local, uncommitted)<br>.env.example<br>docker/docker-compose.cpu.yml<br>docker/docker-compose.prod.yml |
| `CACHE_TTL_UNKNOWN` | 108000 | container_recreate | backend/routes/identities.py<br>backend/routes/websocket.py | — |
| `REDIS_MAX_CONNECTIONS` | 50 | container_recreate | backend/core/cache_manager.py<br>backend/core/cache_manager.py (by name)<br>backend/core/redis_cache.py | .env (local, uncommitted)<br>.env.example<br>docker/docker-compose.cpu.yml<br>docker/docker-compose.prod.yml |
| `REDIS_URL` | (hidden) | container_recreate | backend/config.py<br>backend/core/cache_manager.py<br>backend/core/redis_cache.py<br>backend/core/websocket_manager.py<br>… +3 more | .env (local, uncommitted)<br>.env.example<br>docker/docker-compose.cpu.yml<br>docker/docker-compose.prod.yml<br>… +1 more |
| `REDIS_URL_FILE` | (hidden) | container_recreate | backend/security/redaction.py (by name) | .env.example |

## database

| Setting | In use | Applied | Python consumers | Docker / env / shell |
|---|---|---|---|---|
| `DATABASE_URL` | (hidden) | container_recreate | backend/core/runtime_fingerprint.py<br>backend/security/redaction.py (by name)<br>backend/utils/migrations.py<br>db_connection.py<br>… +2 more | .env (local, uncommitted)<br>.env.example<br>docker/docker-compose.cpu.yml<br>docker/docker-compose.prod.yml<br>… +1 more |
| `DATABASE_URL_FILE` | (hidden) | container_recreate | backend/security/redaction.py (by name) | — |
| `DB_COMMAND_TIMEOUT` | 120 | container_recreate | db_connection.py | — |
| `DB_CONNECT_TIMEOUT` | 30 | container_recreate | db_connection.py | — |
| `DB_HOST` | postgres | container_recreate | sql_agent/config.py<br>sql_agent/config.py (mirrored into sql_agent.config.Config) | .env (local, uncommitted)<br>.env.example<br>docker/docker-compose.cpu.yml<br>docker/docker-compose.prod.yml |
| `DB_IDLE_TX_TIMEOUT_MS` | 300000 | container_recreate | db_connection.py | — |
| `DB_MAX_OVERFLOW` | 60 | container_recreate | db_connection.py | .env (local, uncommitted)<br>.env.example<br>docker/docker-compose.cpu.yml<br>docker/docker-compose.prod.yml |
| `DB_POOL_PRE_PING` | True | container_recreate | db_connection.py | .env (local, uncommitted)<br>.env.example<br>docker/docker-compose.cpu.yml<br>docker/docker-compose.prod.yml |
| `DB_POOL_RECYCLE` | 3600 | container_recreate | db_connection.py | .env (local, uncommitted)<br>.env.example<br>docker/docker-compose.cpu.yml<br>docker/docker-compose.prod.yml |
| `DB_POOL_SIZE` | 30 | container_recreate | backend/core/batch_search_service.py<br>db_connection.py | .env (local, uncommitted)<br>.env.example<br>docker/docker-compose.cpu.yml<br>docker/docker-compose.prod.yml |
| `DB_POOL_TIMEOUT` | 60 | container_recreate | db_connection.py | — |
| `DB_PORT` | 5432 | container_recreate | sql_agent/config.py<br>sql_agent/config.py (mirrored into sql_agent.config.Config) | .env (local, uncommitted)<br>.env.example<br>docker/docker-compose.cpu.yml<br>docker/docker-compose.prod.yml |
| `DB_STATEMENT_TIMEOUT_MS` | 120000 | container_recreate | db_connection.py | — |
| `LOCAL_DB_HOST` | localhost | container_recreate | backend/utils/migrations.py | — |
| `POSTGRES_DB` | face_recognition | container_recreate | sql_agent/config.py<br>sql_agent/config.py (mirrored into sql_agent.config.Config) | .env (local, uncommitted)<br>.env.example<br>docker/docker-compose.cpu.yml<br>docker/docker-compose.prod.yml<br>… +1 more |
| `POSTGRES_PASSWORD` | (hidden) | container_recreate | backend/security/redaction.py (by name)<br>backend/security/webhook_auth.py (by name)<br>sql_agent/config.py<br>sql_agent/config.py (mirrored into sql_agent.config.Config)<br>… +1 more | .env (local, uncommitted)<br>.env.example<br>docker/docker-compose.cpu.yml<br>docker/docker-compose.prod.yml<br>… +1 more |
| `POSTGRES_PASSWORD_FILE` | (hidden) | container_recreate | backend/security/redaction.py (by name) | .env.example |
| `POSTGRES_USER` | postgres | container_recreate | sql_agent/config.py<br>sql_agent/config.py (mirrored into sql_agent.config.Config) | .env (local, uncommitted)<br>.env.example<br>docker/docker-compose.cpu.yml<br>docker/docker-compose.prod.yml<br>… +1 more |

## deployment

| Setting | In use | Applied | Python consumers | Docker / env / shell |
|---|---|---|---|---|
| `AGENT_ORCHESTRATOR` | langgraph | container_recreate | sql_agent/config.py (mirrored into sql_agent.config.Config) | .env.example<br>docker/env.production.example |
| `ALLOW_EXTERNAL_APIS` | False | api_restart | backend/security/offline_policy.py (by name) | .env.example<br>deploy.sh<br>docker/env.production.example |
| `ALLOW_EXTERNAL_TELEMETRY` | False | api_restart | backend/security/offline_policy.py (by name) | .env.example<br>deploy.sh<br>docker/env.production.example |
| `ALLOW_MODEL_DOWNLOADS` | False | api_restart | backend/security/offline_policy.py (by name) | .env.example<br>deploy.sh<br>docker/env.production.example |
| `EMBEDDING_BASE_URL` |  | api_restart | backend/routes/health.py (by name)<br>backend/security/offline_policy.py (by name) | .env.example<br>deploy.sh<br>docker/env.production.example |
| `EMBEDDING_MODEL_PATH` | /home/appuser/.cache/chroma/onnx_models/all-MiniLM-L6-v2/onnx/model.onnx | container_recreate | backend/security/offline_policy.py (by name) | docker/env.production.example |
| `EMBEDDING_PROVIDER` | local | api_restart | backend/security/offline_policy.py (by name)<br>sql_agent/config.py (mirrored into sql_agent.config.Config) | .env.example<br>docker/env.production.example |
| `LLM_API_KEY` | (hidden) | api_restart | backend/security/redaction.py (by name)<br>sql_agent/config.py (mirrored into sql_agent.config.Config) | — |
| `LLM_API_KEY_FILE` | (hidden) | api_restart | backend/security/redaction.py (by name) | — |
| `LLM_BASE_URL` |  | api_restart | backend/routes/health.py<br>backend/security/offline_policy.py (by name)<br>sql_agent/config.py (mirrored into sql_agent.config.Config) | .env.example<br>deploy.sh<br>docker/env.production.example |
| `LLM_MODEL` |  | container_recreate | sql_agent/config.py (mirrored into sql_agent.config.Config) | .env.example<br>docker/docker-compose.prod.yml<br>docker/env.production.example |
| `LLM_PROVIDER` | ollama | api_restart | backend/routes/health.py (by name)<br>backend/security/offline_policy.py (by name)<br>sql_agent/config.py (mirrored into sql_agent.config.Config) | .env.example<br>deploy.sh<br>docker/docker-compose.prod.yml<br>docker/env.production.example |
| `LLM_SQL_MODEL` |  | container_recreate | sql_agent/config.py (mirrored into sql_agent.config.Config) | .env.example<br>docker/env.production.example |
| `MCP_SQL_URL` |  | api_restart | backend/routes/health.py (by name)<br>backend/security/offline_policy.py (by name)<br>sql_agent/config.py (mirrored into sql_agent.config.Config) | .env.example<br>deploy.sh<br>docker/docker-compose.prod.yml<br>docker/env.production.example |
| `MILVUS_URI` |  | api_restart | backend/routes/health.py (by name)<br>backend/security/offline_policy.py (by name)<br>sql_agent/config.py (mirrored into sql_agent.config.Config) | .env.example<br>deploy.sh<br>docker/env.production.example |
| `OFFLINE_ALLOWED_HOSTS` |  | api_restart | backend/security/offline_policy.py (by name) | — |
| `OFFLINE_BUNDLE_MANIFEST` |  | container_recreate | backend/security/offline_policy.py (by name) | docker/env.production.example<br>scripts/verify_offline_bundle.sh |
| `OFFLINE_MODE` |  | api_restart | backend/security/offline_policy.py (by name) | .env.example<br>deploy.sh<br>docker/docker-compose.prod.yml<br>docker/env.production.example |
| `OTEL_EXPORTER_ENDPOINT` |  | api_restart | backend/security/offline_policy.py (by name) | .env.example<br>deploy.sh<br>docker/env.production.example |
| `SQL_AGENT_LEARN_FROM_QUERIES` | False | api_restart | sql_agent/tools/agent_tools.py (by name) | docker/docker-compose.cpu.yml<br>docker/docker-compose.prod.yml |
| `SQL_AGENT_OPIK_ENABLED` | True | api_restart | backend/security/offline_policy.py (by name)<br>sql_agent/config.py<br>sql_agent/config.py (mirrored into sql_agent.config.Config) | .env.example<br>deploy.sh<br>docker/.env (local, uncommitted)<br>docker/docker-compose.cpu.yml<br>… +2 more |
| `STT_BASE_URL` |  | api_restart | backend/routes/health.py (by name)<br>backend/security/offline_policy.py (by name) | .env.example<br>deploy.sh<br>docker/env.production.example |
| `STT_MODEL_PATH` |  | container_recreate | backend/security/offline_policy.py (by name) | docker/env.production.example |
| `STT_PROVIDER` | none | api_restart | backend/security/offline_policy.py (by name) | .env.example<br>docker/env.production.example |
| `VECTOR_STORE` | chroma | container_recreate | sql_agent/config.py (mirrored into sql_agent.config.Config) | .env.example<br>docker/docker-compose.prod.yml<br>docker/env.production.example |

## identity

| Setting | In use | Applied | Python consumers | Docker / env / shell |
|---|---|---|---|---|
| `ACTIVITY_CORRELATION_ENABLED` | True | api_restart | backend/core/intelligence_service.py | — |
| `CLUSTER_ACTIVE_WINDOW_DAYS` | 90 | next_job_run | backend/core/identity_clustering.py | — |
| `CLUSTER_EPS` | 0.35 | api_restart | backend/core/identity_clustering.py<br>backend/routes/identities.py | .env (local, uncommitted) |
| `CLUSTER_INTERVAL_HOURS` | 24 | api_restart | backend/core/identity_clustering.py | .env (local, uncommitted) |
| `CLUSTER_MIN_SAMPLES` | 2 | api_restart | backend/core/identity_clustering.py<br>backend/routes/identities.py | .env (local, uncommitted) |
| `CLUSTER_MIN_SIZE` | 2 | api_restart | backend/core/identity_clustering.py | .env (local, uncommitted) |
| `CLUSTER_STARTUP_DELAY_HOURS` | 7.0 | next_job_run | backend/core/identity_clustering.py | — |
| `CLUSTER_TRAINED_MODEL_MARGIN` | 0.05 | immediate | backend/core/pipeline_aware_clustering.py | — |
| `CROSS_PIPELINE_SIMILARITY_THRESHOLD` | 0.5 | immediate | backend/core/identity_clustering.py<br>backend/core/pipeline_aware_clustering.py | — |
| `EMBEDDING_RETENTION_MONTHS` | 12 | api_restart | backend/core/identity_retention.py | .env (local, uncommitted) |
| `EMBEDDING_SIMILARITY_WEIGHT` | 0.7 | immediate | backend/core/pipeline_aware_clustering.py<br>backend/core/similarity_model.py | — |
| `ENROLL_CANDIDATE_MIN` | 0.4 | immediate | backend/core/enrollment_service.py<br>backend/routes/identities.py | — |
| `ENROLL_CANDIDATE_POOL` | 25 | immediate | backend/core/enrollment_service.py<br>backend/routes/identities.py | — |
| `ENROLL_MAX_CANDIDATES` | 5 | immediate | backend/core/enrollment_service.py<br>backend/routes/identities.py | — |
| `ENROLL_STRONG_MATCH_MIN` | 0.75 | immediate | backend/core/enrollment_service.py<br>backend/routes/identities.py | — |
| `IDENTITY_AUTO_ENRICH_ENABLED` | False | api_restart | backend/core/identity_service.py | — |
| `IDENTITY_CLEANUP_INTERVAL_HOURS` | 24 | next_job_run | backend/core/identity_retention.py<br>backend/routes/retention.py (by name) | .env (local, uncommitted) |
| `IDENTITY_EMBEDDING_SIZE` | 512 | api_restart | backend/core/identity_index_pgvector.py | .env (local, uncommitted) |
| `IDENTITY_ENRICH_MIN_QUALITY` | 0.5 | immediate | backend/core/identity_service.py | — |
| `IDENTITY_ENRICH_MIN_SIMILARITY` | 0.75 | immediate | backend/core/identity_service.py | — |
| `IDENTITY_INDEX_DB_PATH` | ./database/identity_indexes | api_restart | backend/core/vector_index/factory.py<br>backend/lifespan.py | .env (local, uncommitted) |
| `IDENTITY_INGEST_TOP_K` | 5 | immediate | backend/core/identity_service.py | — |
| `IDENTITY_NEAR_DUPLICATE_MIN` | 0.95 | immediate | backend/core/identity_service.py | — |
| `IDENTITY_QUALITY_THRESHOLD_KNOWN` | 0.5 | api_restart | backend/core/identity_service.py | — |
| `IDENTITY_QUALITY_THRESHOLD_UNKNOWN` | 0.1 | api_restart | backend/core/identity_service.py | — |
| `IDENTITY_SNAPSHOT_REPLACE_MIN_SIMILARITY` | 0.75 | api_restart | backend/core/identity_service.py | — |
| `INACTIVE_THRESHOLD_DAYS` | 180 | next_job_run | backend/core/identity_retention.py<br>backend/routes/retention.py (by name) | .env (local, uncommitted) |
| `KNOWN_INDEX_TYPE` | flat | api_restart | backend/core/vector_index/factory.py | — |
| `MAX_EMBEDDINGS_PER_IDENTITY` | 10 | next_job_run | backend/core/identity_retention.py<br>backend/core/identity_service.py<br>backend/routes/retention.py (by name) | .env (local, uncommitted) |
| `MERGE_WARNING_MIN_SIMILARITY` | 0.4 | api_restart | backend/core/merge_compatibility.py | — |
| `PGVECTOR_HNSW_EF_CONSTRUCTION` | 128 | index_rebuild | backend/core/identity_index_pgvector.py | .env.example<br>docker/docker-compose.cpu.yml<br>docker/docker-compose.prod.yml |
| `PGVECTOR_HNSW_M` | 32 | index_rebuild | backend/core/identity_index_pgvector.py | .env.example<br>docker/docker-compose.cpu.yml<br>docker/docker-compose.prod.yml |
| `PGVECTOR_INDEX_TYPE` | hnsw | index_rebuild | backend/core/identity_index_pgvector.py | .env.example<br>docker/docker-compose.cpu.yml<br>docker/docker-compose.prod.yml |
| `PGVECTOR_IVFFLAT_LISTS` | 100 | index_rebuild | backend/core/identity_index_pgvector.py | — |
| `PGVECTOR_IVFFLAT_PROBES` | 10 | api_restart | backend/core/identity_index_pgvector.py | — |
| `PIPELINE_AWARE_CLUSTERING_ENABLED` | True | immediate | backend/routes/identities.py | — |
| `PIPELINE_SIMILARITY_WEIGHT` | 0.3 | immediate | backend/core/pipeline_aware_clustering.py<br>backend/core/similarity_model.py | — |
| `SIMILARITY_MODEL_AUTO_TRAIN` | True | api_restart | backend/routes/identities.py | — |
| `SIMILARITY_MODEL_MIN_SAMPLES` | 50 | api_restart | backend/core/model_training_service.py<br>backend/routes/identities.py | — |
| `SIMILARITY_MODEL_PATH` | models/similarity_model.pkl | api_restart | backend/core/model_training_service.py<br>backend/core/similarity_model.py | — |
| `SIMILARITY_QUALITY_FLOOR` | 0.7 | immediate | backend/core/similarity_model.py | — |
| `SNAPSHOT_RETENTION_DAYS` | 90 | next_job_run | backend/core/identity_retention.py<br>backend/routes/retention.py (by name) | .env (local, uncommitted) |
| `VECTOR_BACKEND` | pgvector | api_restart | backend/core/advanced_search.py<br>backend/core/enrollment_service.py<br>backend/core/identity_clustering.py<br>backend/core/identity_index_pgvector.py<br>… +9 more | .env.example<br>docker/docker-compose.cpu.yml<br>docker/docker-compose.prod.yml<br>scripts/deploy/stage-health.sh |
| `VECTOR_INDEX_FALLBACK` |  | api_restart | backend/core/vector_index/factory.py | — |

## map

| Setting | In use | Applied | Python consumers | Docker / env / shell |
|---|---|---|---|---|
| `MAP_AVAILABILITY_REFRESH_SECONDS` | 300 | api_restart | backend/core/map_availability.py | — |
| `MAP_BOUNDS_EAST` | 36.92 | api_restart | backend/core/map_availability.py | — |
| `MAP_BOUNDS_NORTH` | 34.89 | api_restart | backend/core/map_availability.py | — |
| `MAP_BOUNDS_SOUTH` | 32.84 | api_restart | backend/core/map_availability.py | — |
| `MAP_BOUNDS_WEST` | 34.8 | api_restart | backend/core/map_availability.py | — |
| `MAP_DATA_DIR` | /app/map-data | api_restart | — | docker/docker-compose.prod.yml<br>docker/docker-compose.regression.yml<br>scripts/run_regression_isolated.sh |
| `MAP_INSTALL_DISK_RESERVE_GB` | 2.0 | api_restart | — | scripts/map_data/install_dataset.sh |
| `MAP_MARTIN_INTERNAL_URL` | http://martin:3000 | api_restart | backend/core/map_availability.py | — |
| `MAP_MAX_COORDINATES` | 10000 | api_restart | backend/core/map_data_service.py | — |

## ml_ops

| Setting | In use | Applied | Python consumers | Docker / env / shell |
|---|---|---|---|---|
| `MLFLOW_ENABLED` | False | immediate | backend/ml/constants.py (by name)<br>backend/ml/mlflow_tracking.py | .env.example |
| `MLFLOW_EXPERIMENT_NAME` | ml-platform | next_job_run | backend/ml/capabilities.py<br>backend/ml/mlflow_tracking.py | .env.example |
| `MLFLOW_TRACKING_URI` |  | next_job_run | backend/ml/capabilities.py<br>backend/ml/mlflow_tracking.py<br>backend/routes/ml_ops.py | .env.example |
| `ML_COLLECTOR_LATE_GRACE_MINUTES` | 120 | next_job_run | backend/ml/collector.py | .env.example |
| `ML_DECISION_MODE` | rules | immediate | backend/ml/decision_service.py<br>backend/ml/mode_service.py<br>backend/ml/mode_service.py (by name)<br>backend/routes/ml_ops.py (by name) | .env.example |
| `ML_DRIFT_CHECK_INTERVAL_HOURS` | 24 | next_job_run | backend/ml/worker.py | .env.example |
| `ML_DRIFT_MIN_SAMPLES` | 200 | next_job_run | backend/ml/drift_service.py<br>backend/ml/worker.py | .env.example |
| `ML_DRIFT_MONITORING_ENABLED` | False | worker_restart | backend/ml/worker.py | .env.example |
| `ML_DRIFT_PSI_CRITICAL` | 0.25 | next_job_run | backend/ml/drift_service.py | .env.example |
| `ML_DRIFT_PSI_WARNING` | 0.1 | next_job_run | backend/ml/drift_service.py | .env.example |
| `ML_DRIFT_REPORT_RETENTION_DAYS` | 365 | next_job_run | backend/core/data_retention.py | .env.example |
| `ML_EVIDENCE_REQUIRE_INDEPENDENT_REVIEW` | False | immediate | backend/ml/labeling_service.py | — |
| `ML_FEATURE_SAMPLED_FULL_VECTOR_RATE` | 0.0 | immediate | backend/ml/inference_service.py | .env.example |
| `ML_GRAPH_MIN_EDGES` | 50 | immediate | backend/ml/feature_builders.py<br>backend/ml/feature_store.py (by name)<br>backend/ml/relational_feature_service.py | .env.example |
| `ML_GRAPH_MIN_NODES` | 25 | immediate | backend/ml/feature_builders.py<br>backend/ml/feature_store.py (by name)<br>backend/ml/relational_feature_service.py | .env.example |
| `ML_GRAPH_MIN_OBSERVATION_DAYS` | 14 | immediate | backend/ml/feature_builders.py<br>backend/ml/feature_store.py (by name)<br>backend/ml/relational_feature_service.py | .env.example |
| `ML_GRAPH_MIN_PAIR_APPEARANCES` | 3 | immediate | backend/ml/feature_builders.py<br>backend/ml/feature_store.py (by name) | .env.example |
| `ML_INFERENCE_TIMEOUT_MS` | 1500 | immediate | backend/ml/inference_service.py | .env.example |
| `ML_JOB_MAINTENANCE_SECONDS` | 30.0 | api_restart | backend/ml/worker.py (by name) | .env.example |
| `ML_MAX_ARTIFACT_MB` | 200 | immediate | backend/ml/registry_service.py | .env.example |
| `ML_MODEL_CACHE_TTL_SECONDS` | 60 | immediate | backend/ml/inference_service.py | .env.example |
| `ML_OPTUNA_MAX_TRIALS` | 30 | next_job_run | backend/ml/run_spec.py<br>backend/routes/ml_ops.py | .env.example |
| `ML_OPTUNA_TIMEOUT_SECONDS` | 600 | next_job_run | backend/ml/run_spec.py<br>backend/routes/ml_ops.py | .env.example |
| `ML_PREDICTION_RETENTION_DAYS` | 180 | next_job_run | backend/core/data_retention.py | .env.example |
| `ML_SHADOW_TIMEOUT_MS` | 3000 | immediate | backend/ml/shadow_service.py | .env.example |
| `ML_SHAP_BACKGROUND_ROWS` | 50 | next_job_run | backend/ml/explainability.py | .env.example |
| `ML_SHAP_MAX_ROWS` | 100 | next_job_run | backend/ml/explainability.py<br>backend/routes/ml_ops.py | .env.example |
| `ML_SNAPSHOT_RETENTION_DAYS` | 365 | next_job_run | backend/core/data_retention.py | .env.example |
| `ML_SUPERVISED_MIN_LABELS` | 100 | immediate | backend/ml/labeling_service.py | .env.example |
| `ML_SUPERVISED_MIN_PER_CLASS` | 25 | immediate | backend/ml/labeling_service.py | .env.example |
| `ML_TRAIN_MAX_THREADS` | 2 | next_job_run | backend/ml/xgboost_runtime.py<br>backend/routes/ml_ops.py | .env.example |
| `ML_WORKER_ID` |  | api_restart | backend/ml/worker.py | .env.example<br>docker/docker-compose.cpu.yml<br>docker/docker-compose.prod.yml |
| `OPTUNA_ENABLED` | False | immediate | backend/ml/constants.py (by name) | .env.example |
| `SHAP_ENABLED` | False | immediate | backend/ml/constants.py (by name) | .env.example |
| `XGBOOST_ENABLED` | False | immediate | backend/ml/constants.py (by name) | .env.example |

## models

| Setting | In use | Applied | Python consumers | Docker / env / shell |
|---|---|---|---|---|
| `CONFIDENCE_THRESHOLD` | 0.5 | api_restart | backend/core/model_manager.py | .env (local, uncommitted)<br>.env.example<br>docker/docker-compose.cpu.yml<br>docker/docker-compose.prod.yml |
| `DETECTION_MODEL` | /app/weights/det_10g.onnx | api_restart | backend/core/enrollment_service.py<br>backend/core/model_manager.py<br>backend/lifespan.py<br>backend/security/offline_policy.py (by name) | .env (local, uncommitted)<br>.env.example<br>docker/docker-compose.cpu.yml<br>docker/docker-compose.prod.yml<br>… +1 more |
| `RECOGNITION_MODEL` | /app/weights/w600k_r50.onnx | api_restart | backend/core/advanced_search.py<br>backend/core/enrollment_service.py<br>backend/core/identity_service.py<br>backend/core/model_manager.py<br>… +2 more | .env (local, uncommitted)<br>.env.example<br>docker/docker-compose.cpu.yml<br>docker/docker-compose.prod.yml<br>… +1 more |
| `SIMILARITY_THRESHOLD` | 0.4 | immediate | backend/core/advanced_search.py<br>backend/core/identity_index_pgvector.py<br>backend/core/identity_service.py<br>backend/routes/advanced_search.py<br>… +1 more | .env (local, uncommitted)<br>.env.example<br>docker/docker-compose.cpu.yml<br>docker/docker-compose.prod.yml |
| `UNKNOWN_SIMILARITY_THRESHOLD` | 0.35 | immediate | backend/core/identity_clustering.py<br>backend/core/identity_index_pgvector.py<br>backend/core/identity_service.py<br>backend/core/pipeline_aware_clustering.py<br>… +1 more | — |

## ollama

| Setting | In use | Applied | Python consumers | Docker / env / shell |
|---|---|---|---|---|
| `OLLAMA_BASE_URL` | http://ollama:11434 | api_restart | backend/routes/health.py<br>backend/security/offline_policy.py (by name)<br>sql_agent/config.py<br>sql_agent/config.py (mirrored into sql_agent.config.Config) | .env (local, uncommitted)<br>.env.example<br>docker/docker-compose.cpu.yml<br>docker/docker-compose.prod.yml<br>… +1 more |
| `OLLAMA_INTERPRETER_MODEL` |  | container_recreate | sql_agent/config.py (mirrored into sql_agent.config.Config) | docker/docker-compose.cpu.yml<br>docker/docker-compose.prod.yml |
| `OLLAMA_MODEL` | qwen2.5:1.5b | api_restart | sql_agent/config.py<br>sql_agent/config.py (mirrored into sql_agent.config.Config) | .env (local, uncommitted)<br>.env.example<br>docker/docker-compose.cpu.yml<br>docker/docker-compose.prod.yml<br>… +2 more |
| `OLLAMA_SQL_MODEL` | hf.co/mradermacher/Arctic-Text2SQL-R1-7B-GGUF:Q4_K_M | api_restart | sql_agent/config.py<br>sql_agent/config.py (mirrored into sql_agent.config.Config) | .env.example<br>docker/docker-compose.cpu.yml<br>docker/docker-compose.prod.yml<br>docker/env.production.example<br>… +1 more |
| `OLLAMA_TEMPERATURE` | 0.1 | api_restart | sql_agent/config.py<br>sql_agent/config.py (mirrored into sql_agent.config.Config)<br>sql_agent/llm/ollama_provider.py | .env (local, uncommitted)<br>.env.example<br>docker/docker-compose.cpu.yml |
| `OLLAMA_TIMEOUT` | 120 | api_restart | sql_agent/config.py<br>sql_agent/config.py (mirrored into sql_agent.config.Config) | .env (local, uncommitted)<br>.env.example<br>docker/docker-compose.cpu.yml |

## processing

| Setting | In use | Applied | Python consumers | Docker / env / shell |
|---|---|---|---|---|
| `BATCH_SIZE` | 10 | api_restart | — | .env (local, uncommitted)<br>docker/docker-compose.cpu.yml<br>docker/docker-compose.gpu.yml |
| `DASHBOARD_CLEANUP_INTERVAL_SECONDS` | 60 | api_restart | backend/routes/stats.py | — |
| `FACE_QUALITY_SCORER` | full | api_restart | backend/core/face_quality.py | — |
| `FACE_TRACKING_CLEANUP_INTERVAL` | 300 | api_restart | backend/config.py | .env (local, uncommitted)<br>docker/docker-compose.cpu.yml |
| `INFERENCE_WORKERS` | 3 | api_restart | backend/services/image_processing.py | .env.example<br>docker/docker-compose.cpu.yml<br>docker/docker-compose.prod.yml<br>scripts/deploy/lib.sh<br>… +1 more |
| `MAX_CONCURRENT_INFERENCE` | 3 | api_restart | backend/services/image_processing.py | .env.example<br>docker/docker-compose.cpu.yml<br>docker/docker-compose.prod.yml |
| `MAX_CONCURRENT_INFERENCE_PER_PIPELINE` | 2 | api_restart | backend/services/image_processing.py | docker/docker-compose.cpu.yml |
| `MAX_CONCURRENT_REQUESTS` | 150 | api_restart | backend/core/processing_queue.py<br>backend/routes/health.py | .env (local, uncommitted)<br>docker/docker-compose.cpu.yml<br>docker/docker-compose.gpu.yml<br>scripts/setup/start_production.sh |
| `MAX_QUEUE_SIZE` | 2000 | api_restart | backend/core/processing_queue.py | .env (local, uncommitted)<br>.env.example<br>docker/docker-compose.cpu.yml<br>docker/docker-compose.gpu.yml<br>… +2 more |
| `PIPELINE_BATCH_SIZE` | 5 | api_restart | backend/core/processing_queue.py | .env (local, uncommitted)<br>docker/docker-compose.gpu.yml |
| `QUEUE_WORKERS` | 15 | api_restart | backend/lifespan.py | .env (local, uncommitted)<br>.env.example<br>docker/docker-compose.cpu.yml<br>docker/docker-compose.gpu.yml<br>… +4 more |
| `WEBHOOK_DEDUP_TTL_SECONDS` | 600 | next_request | backend/routes/admin_tutorial.py (by name)<br>backend/routes/webhook.py | docker/docker-compose.cpu.yml |
| `WEBHOOK_MAX_BODY_MB` | 25 | next_request | backend/lifespan.py<br>backend/routes/webhook.py | docker/docker-compose.cpu.yml |

## retention

| Setting | In use | Applied | Python consumers | Docker / env / shell |
|---|---|---|---|---|
| `AUDIT_LOG_RETENTION_DAYS` | 180 | next_job_run | backend/core/data_retention.py<br>backend/routes/retention.py (by name) | — |
| `BACKUP_INTERVAL_SECONDS` | 86400 | container_recreate | — | .env.example<br>docker/docker-compose.prod.yml<br>scripts/backup/backup-loop.sh |
| `BACKUP_RETENTION_DAYS` | 14 | container_recreate | — | .env.example<br>docker/docker-compose.prod.yml<br>scripts/backup/backup-loop.sh<br>scripts/backup/backup.sh |
| `BATCH_WRITE_INTERVAL` | 2.0 | api_restart | backend/core/batch_writer.py | .env (local, uncommitted)<br>docker/docker-compose.cpu.yml |
| `BATCH_WRITE_SIZE` | 25 | api_restart | backend/config.py | .env (local, uncommitted)<br>docker/docker-compose.cpu.yml |
| `CLEANUP_INTERVAL_HOURS` | 24 | next_job_run | backend/core/data_retention.py<br>backend/routes/retention.py (by name) | .env (local, uncommitted)<br>docker/docker-compose.cpu.yml |
| `DATA_RETENTION_DAYS` | 30 | next_job_run | backend/core/data_retention.py<br>backend/lifespan.py<br>backend/routes/retention.py (by name)<br>backend/routes/stats.py | .env (local, uncommitted)<br>.env.example<br>docker/docker-compose.cpu.yml<br>docker/docker-compose.prod.yml |
| `TASK_HISTORY_RETENTION_DAYS` | 30 | next_job_run | backend/core/data_retention.py | — |

## security

| Setting | In use | Applied | Python consumers | Docker / env / shell |
|---|---|---|---|---|
| `ACCESS_TOKEN_EXPIRE_MINUTES` | 1440 | api_restart | backend/auth/auth_security.py<br>backend/auth/auth_service.py | .env (local, uncommitted)<br>.env.example |
| `AUTH_ALLOWED_ORIGINS` | http://localhost,http://localhost:8000,http://127.0.0.1,http://127.0.0.1:8000 | api_restart | backend/security/origins.py (by name) | .env.example<br>docker/docker-compose.cpu.yml<br>docker/docker-compose.prod.yml |
| `AUTH_COOKIE_HOST_PREFIX` | True | api_restart | backend/auth/auth_security.py | .env.example<br>docker/docker-compose.prod.yml |
| `AUTH_COOKIE_SAMESITE` | lax | api_restart | backend/auth/auth_security.py | .env.example<br>docker/docker-compose.prod.yml |
| `AUTH_COOKIE_SECURE` | False | api_restart | backend/auth/auth_security.py | .env.example<br>docker/docker-compose.prod.yml |
| `AUTH_RATE_LIMIT_ACCOUNT_MAX` | 8 | api_restart | backend/auth/auth_security.py | — |
| `AUTH_RATE_LIMIT_ACCOUNT_WINDOW` | 900 | api_restart | backend/auth/auth_security.py | — |
| `AUTH_RATE_LIMIT_ENABLED` | True | api_restart | backend/auth/auth_security.py | .env.example |
| `AUTH_RATE_LIMIT_GLOBAL_MAX` | 600 | api_restart | backend/auth/auth_security.py | — |
| `AUTH_RATE_LIMIT_GLOBAL_WINDOW` | 60 | api_restart | backend/auth/auth_security.py | — |
| `AUTH_RATE_LIMIT_IP_MAX` | 30 | api_restart | backend/auth/auth_security.py | — |
| `AUTH_RATE_LIMIT_IP_WINDOW` | 900 | api_restart | backend/auth/auth_security.py | — |
| `AUTH_SAME_HOST_ORIGIN_TRUSTED` | True | api_restart | backend/security/origins.py (by name) | .env.example<br>docker/docker-compose.cpu.yml<br>docker/docker-compose.prod.yml |
| `AUTH_TRUST_PROXY_HEADERS` | True | api_restart | backend/auth/auth_security.py | .env.example |
| `BOOTSTRAP_ADMIN_EMAIL` | admin@example.com | api_restart | backend/services/bootstrap_admin.py (by name) | .env.example |
| `BOOTSTRAP_ADMIN_ENABLED` | True | api_restart | backend/services/bootstrap_admin.py (by name) | — |
| `BOOTSTRAP_ADMIN_PASSWORD` | (hidden) | api_restart | backend/security/redaction.py (by name)<br>backend/services/bootstrap_admin.py (by name) | .env.example<br>docker/docker-compose.cpu.yml |
| `BOOTSTRAP_ADMIN_PASSWORD_FILE` | (hidden) | api_restart | backend/security/redaction.py (by name)<br>backend/services/bootstrap_admin.py (by name) | .env.example<br>docker/docker-compose.prod.yml |
| `BOOTSTRAP_ADMIN_REQUIRE_ROTATION` | False | api_restart | backend/services/bootstrap_admin.py (by name) | .env.example<br>docker/docker-compose.cpu.yml<br>docker/docker-compose.prod.yml |
| `BOOTSTRAP_ADMIN_USERNAME` | admin | api_restart | backend/services/bootstrap_admin.py (by name) | .env.example<br>docker/docker-compose.cpu.yml |
| `CORS_ORIGINS` | http://localhost,http://localhost:8000,http://127.0.0.1,http://127.0.0.1:8000 | api_restart | backend/security/origins.py (by name) | .env (local, uncommitted)<br>.env.example<br>docker/docker-compose.cpu.yml<br>docker/docker-compose.prod.yml |
| `ENABLE_API_DOCS` | True | api_restart | backend/main.py | .env.example<br>docker/docker-compose.cpu.yml<br>docker/docker-compose.prod.yml |
| `JWT_ALGORITHM` | HS256 | api_restart | backend/auth/auth_service.py | .env (local, uncommitted)<br>.env.example |
| `JWT_AUDIENCE` | face-recognition-api | api_restart | backend/auth/auth_service.py | .env.example |
| `JWT_ISSUER` | face-recognition-service | api_restart | backend/auth/auth_service.py | .env.example |
| `JWT_SECRET_KEY` | (hidden) | api_restart | backend/auth/auth_security.py<br>backend/auth/auth_service.py<br>backend/security/redaction.py (by name)<br>backend/security/webhook_auth.py (by name)<br>… +1 more | .env (local, uncommitted)<br>.env.example<br>scripts/setup/generate-secrets.sh |
| `JWT_SECRET_KEY_FILE` | (hidden) | api_restart | backend/security/redaction.py (by name) | .env.example<br>docker/docker-compose.prod.yml |
| `TLS_CERT_PATH` | /etc/nginx/certs/server.crt | api_restart | backend/core/operational_metrics.py | — |
| `WEBHOOK_API_KEYS` | (hidden) | api_restart | backend/main.py<br>backend/security/redaction.py (by name)<br>backend/security/webhook_auth.py (by name)<br>utils/logging.py | .env.example<br>docker/docker-compose.cpu.yml |
| `WEBHOOK_API_KEYS_FILE` | (hidden) | api_restart | backend/security/redaction.py (by name) | .env.example<br>docker/docker-compose.cpu.yml<br>docker/docker-compose.prod.yml |
| `WEBHOOK_AUTH_HEADER` | X-Webhook-Key | api_restart | backend/security/webhook_auth.py (by name) | .env.example |
| `WEBHOOK_AUTH_INSECURE_ACK` | False | api_restart | — | .env.example |
| `WEBHOOK_AUTH_MODE` | enforce | api_restart | backend/security/webhook_auth.py (by name) | .env.example<br>docker/docker-compose.cpu.yml<br>docker/docker-compose.prod.yml<br>scripts/deploy/stage-health.sh |
| `WEBHOOK_AUTH_TOKEN` | (hidden) | api_restart | backend/security/redaction.py (by name) | .env.example<br>docker/docker-compose.cpu.yml<br>docker/docker-compose.prod.yml |
| `WEBHOOK_AUTH_TOKEN_FILE` | (hidden) | api_restart | backend/security/redaction.py (by name) | .env.example |
| `WEBHOOK_CREDENTIAL_CACHE_TTL_SECONDS` | 30 | api_restart | backend/security/webhook_credentials.py | .env.example |

## server

| Setting | In use | Applied | Python consumers | Docker / env / shell |
|---|---|---|---|---|
| `ALLOW_CPU_FALLBACK` | True | container_recreate | backend/core/gpu_runtime.py (by name) | .env.example<br>docker/docker-compose.cpu.yml<br>docker/docker-compose.gpu.yml<br>docker/docker-compose.prod.gpu.yml<br>… +1 more |
| `ALLOW_MULTI_WORKER` | False | container_recreate | — | gunicorn.conf.py |
| `APP_NAME` | Face Recognition Service | container_recreate | backend/main.py<br>backend/routes/stats.py<br>db_connection.py | — |
| `BACKGROUND_TASK_NOTIFICATIONS_ENABLED` | True | container_recreate | backend/core/background_task_notifier.py | — |
| `BACKGROUND_TASK_NOTIFICATION_LEAD_TIME_SECONDS` | 60 | container_recreate | backend/core/background_task_notifier.py | — |
| `DEBUG` | False | container_recreate | backend/routes/logs.py (by name)<br>db_connection.py | .env (local, uncommitted)<br>.env.example<br>docker/docker-compose.cpu.yml<br>docker/docker-compose.prod.yml |
| `ENVIRONMENT` | development | container_recreate | backend/core/runtime_fingerprint.py<br>backend/routes/health.py<br>backend/services/bootstrap_admin.py (by name) | .env (local, uncommitted)<br>.env.example<br>deploy.sh<br>docker-entrypoint.sh<br>… +4 more |
| `GIT_COMMIT` |  | container_recreate | backend/core/runtime_fingerprint.py | — |
| `HOST` | 0.0.0.0 | container_recreate | gunicorn.conf.py | .env (local, uncommitted)<br>.env.example<br>docker/docker-compose.cpu.yml<br>docker/docker-compose.prod.yml<br>… +3 more |
| `HOSTNAME` | 2bf54f6218a1 | container_recreate | backend/core/runtime_fingerprint.py | — |
| `LOGS_LIFE_TIME_HOURS` | 48 | next_job_run | backend/core/log_cleanup.py<br>backend/lifespan.py<br>backend/routes/logs.py<br>backend/routes/retention.py (by name) | — |
| `LOG_API_DEFAULT_PAGE_SIZE` | 50 | container_recreate | backend/routes/logs.py | — |
| `LOG_API_MAX_PAGE_SIZE` | 500 | container_recreate | backend/routes/logs.py | — |
| `LOG_API_MAX_SCAN_BYTES` | 67108864 | container_recreate | backend/routes/logs.py | — |
| `LOG_API_MAX_SCAN_FILES` | 6 | container_recreate | backend/routes/logs.py | — |
| `LOG_API_TIMEOUT_SECONDS` | 10.0 | container_recreate | backend/routes/logs.py | — |
| `LOG_BACKUP_COUNT` | 5 | container_recreate | utils/logging.py (by name) | — |
| `LOG_DIR` | /var/log/face-recognition | container_recreate | backend/core/log_cleanup.py<br>backend/routes/logs.py<br>utils/logging.py<br>utils/logging.py (by name) | .env (local, uncommitted)<br>.env.example<br>docker/docker-compose.cpu.yml<br>docker/docker-compose.prod.yml<br>… +3 more |
| `LOG_FILE_LEVEL` |  | container_recreate | utils/logging.py (by name) | — |
| `LOG_FILE_NAME` | app.log | container_recreate | backend/routes/logs.py<br>utils/logging.py (by name) | — |
| `LOG_LEVEL` | INFO | api_restart | backend/ml/worker.py<br>gunicorn.conf.py<br>utils/logging.py<br>utils/logging.py (by name) | .env (local, uncommitted)<br>.env.example<br>docker/docker-compose.cpu.yml<br>docker/docker-compose.prod.yml<br>… +2 more |
| `LOG_MAX_BYTES` | 10485760 | container_recreate | utils/logging.py (by name) | — |
| `MIGRATIONS_EXPECTED_HEAD` |  | container_recreate | backend/core/runtime_fingerprint.py<br>backend/utils/migrations.py | .env.example<br>deploy.sh<br>docker/docker-compose.prod.yml<br>scripts/deploy/self-test.sh<br>… +1 more |
| `MIGRATIONS_MODE` | run | container_recreate | backend/core/runtime_fingerprint.py<br>backend/lifespan.py<br>backend/utils/migrations.py | .env.example<br>docker/docker-compose.cpu.yml<br>docker/docker-compose.prod.yml<br>docker/docker-compose.regression.yml<br>… +1 more |
| `MIGRATION_DB_RETRY_INTERVAL_SECONDS` | 2 | container_recreate | backend/utils/migrations.py | — |
| `MIGRATION_DB_WAIT_SECONDS` | 60 | container_recreate | backend/utils/migrations.py | — |
| `MLFLOW_HTTP_REQUEST_MAX_RETRIES` | 1 | container_recreate | — | .env.example |
| `MLFLOW_HTTP_REQUEST_TIMEOUT` | 10 | container_recreate | — | .env.example |
| `PORT` | 8000 | container_recreate | gunicorn.conf.py | .env (local, uncommitted)<br>.env.example<br>docker/docker-compose.cpu.yml<br>docker/docker-compose.prod.yml<br>… +2 more |
| `USE_GPU` | False | container_recreate | backend/core/gpu_runtime.py (by name)<br>backend/core/operational_metrics.py<br>backend/core/runtime_fingerprint.py<br>gunicorn.conf.py | .env (local, uncommitted)<br>.env.example<br>docker/docker-compose.cpu.yml<br>docker/docker-compose.gpu.yml<br>… +3 more |
| `VERSION` | 5.0.0 | container_recreate | backend/core/runtime_fingerprint.py<br>backend/lifespan.py<br>backend/main.py<br>backend/routes/health.py<br>… +1 more | — |
| `WORKERS` | 1 | container_recreate | backend/core/runtime_fingerprint.py<br>backend/lifespan.py<br>gunicorn.conf.py | .env (local, uncommitted)<br>.env.example<br>deploy.sh<br>docker/docker-compose.cpu.yml<br>… +8 more |

## sql_agent

| Setting | In use | Applied | Python consumers | Docker / env / shell |
|---|---|---|---|---|
| `CHROMADB_PATH` | /app/sql_agent/chromadb_data | api_restart | sql_agent/config.py<br>sql_agent/config.py (mirrored into sql_agent.config.Config) | .env (local, uncommitted)<br>.env.example<br>docker/docker-compose.cpu.yml<br>docker/docker-compose.prod.yml |
| `CHROMA_COLLECTION_NAME` | sql_knowledge_base | api_restart | sql_agent/config.py<br>sql_agent/config.py (mirrored into sql_agent.config.Config) | .env (local, uncommitted)<br>.env.example<br>docker/docker-compose.cpu.yml |
| `LLM_DEV_PROVIDER` | nim | api_restart | backend/security/offline_policy.py (by name)<br>sql_agent/config.py<br>sql_agent/config.py (mirrored into sql_agent.config.Config) | deploy.sh<br>docker/.env (local, uncommitted)<br>docker/docker-compose.cpu.yml<br>docker/docker-compose.regression.yml<br>… +2 more |
| `NVIDIA_NIM_API_KEY` | (hidden) | api_restart | backend/security/offline_policy.py (by name)<br>backend/security/redaction.py (by name)<br>sql_agent/config.py<br>sql_agent/config.py (mirrored into sql_agent.config.Config) | docker/.env (local, uncommitted)<br>docker/docker-compose.cpu.yml<br>docker/docker-compose.regression.yml<br>docker/env.production.example<br>… +1 more |
| `NVIDIA_NIM_BASE_URL` | https://integrate.api.nvidia.com/v1 | api_restart | sql_agent/config.py<br>sql_agent/config.py (mirrored into sql_agent.config.Config) | — |
| `NVIDIA_NIM_INTERPRETER_MODEL` |  | api_restart | sql_agent/config.py (mirrored into sql_agent.config.Config) | docker/docker-compose.cpu.yml |
| `NVIDIA_NIM_MODEL` | meta/llama-3.2-11b-vision-instruct | api_restart | sql_agent/config.py<br>sql_agent/config.py (mirrored into sql_agent.config.Config) | docker/.env (local, uncommitted)<br>docker/docker-compose.cpu.yml |
| `NVIDIA_NIM_SQL_MODEL` | meta/llama-3.2-11b-vision-instruct | api_restart | sql_agent/config.py<br>sql_agent/config.py (mirrored into sql_agent.config.Config) | docker/.env (local, uncommitted)<br>docker/docker-compose.cpu.yml |
| `NVIDIA_NIM_TIMEOUT` | 60 | api_restart | sql_agent/config.py<br>sql_agent/config.py (mirrored into sql_agent.config.Config) | — |
| `OPIK_API_KEY` | (hidden) | api_restart | backend/security/offline_policy.py (by name)<br>backend/security/redaction.py (by name)<br>sql_agent/config.py<br>sql_agent/config.py (mirrored into sql_agent.config.Config) | .env.example<br>docker/docker-compose.cpu.yml |
| `OPIK_PROJECT_NAME` | face-detector-sql-agent | api_restart | sql_agent/config.py<br>sql_agent/config.py (mirrored into sql_agent.config.Config) | .env.example<br>docker/docker-compose.cpu.yml<br>scripts/deploy/stage-dev.sh |
| `OPIK_URL_OVERRIDE` | http://host.docker.internal:5173/api/ | api_restart | backend/security/offline_policy.py (by name)<br>sql_agent/config.py<br>sql_agent/config.py (mirrored into sql_agent.config.Config) | .env.example<br>deploy.sh<br>docker/docker-compose.cpu.yml<br>scripts/deploy/stage-dev.sh |
| `OPIK_WORKSPACE` | default | api_restart | sql_agent/config.py<br>sql_agent/config.py (mirrored into sql_agent.config.Config) | .env.example<br>docker/docker-compose.cpu.yml |
| `RAG_SIMILARITY_THRESHOLD` | 0.3 | api_restart | sql_agent/config.py<br>sql_agent/config.py (mirrored into sql_agent.config.Config) | .env (local, uncommitted)<br>docker/docker-compose.cpu.yml |
| `RAG_TOP_K` | 5 | api_restart | sql_agent/config.py<br>sql_agent/config.py (mirrored into sql_agent.config.Config) | .env (local, uncommitted)<br>docker/docker-compose.cpu.yml |
| `SQL_AGENT_DB_PASSWORD` | (hidden) | api_restart | backend/security/redaction.py (by name)<br>sql_agent/config.py<br>sql_agent/config.py (mirrored into sql_agent.config.Config) | .env.example<br>docker/docker-compose.cpu.yml<br>docker/docker-compose.prod.yml |
| `SQL_AGENT_DB_PASSWORD_FILE` | (hidden) | api_restart | backend/security/redaction.py (by name) | — |
| `SQL_AGENT_DB_USER` | fr_readonly | api_restart | sql_agent/config.py<br>sql_agent/config.py (mirrored into sql_agent.config.Config) | .env.example<br>docker/docker-compose.cpu.yml<br>docker/docker-compose.prod.yml |
| `SQL_AGENT_MAX_ACTIONS_PER_TURN` | 3 | api_restart | sql_agent/graph.py<br>sql_agent/tools/agent_tools.py | — |
| `SQL_AGENT_MAX_CONCURRENT` | 2 | api_restart | sql_agent/api/routes.py | docker/docker-compose.cpu.yml |
| `SQL_AGENT_MAX_EXECUTION_RETRIES` | 1 | api_restart | sql_agent/graph.py<br>sql_agent/tools/agent_tools.py | .env.example<br>docker/docker-compose.cpu.yml |
| `SQL_AGENT_MAX_QUERY_CHARS` | 8000 | api_restart | sql_agent/api/routes.py<br>sql_agent/tools/agent_tools.py | .env.example |
| `SQL_AGENT_MAX_REASONING_STEPS` | 8 | api_restart | sql_agent/tools/agent_tools.py | .env.example<br>docker/docker-compose.cpu.yml |
| `SQL_AGENT_MAX_REPLANS` | 2 | api_restart | sql_agent/graph.py<br>sql_agent/tools/agent_tools.py | .env.example<br>docker/docker-compose.cpu.yml |
| `SQL_AGENT_TOTAL_TIMEOUT` | 300 | api_restart | sql_agent/api/routes.py<br>sql_agent/api/routes.py (by name)<br>sql_agent/llm/gateway.py<br>sql_agent/run_control.py | docker/docker-compose.cpu.yml |
| `SQL_AGENT_TRACE_CONTEXT` | False | api_restart | sql_agent/tools/agent_tools.py | — |

## storage

| Setting | In use | Applied | Python consumers | Docker / env / shell |
|---|---|---|---|---|
| `ALLOWED_IMAGE_EXTENSIONS` | .jpg,.jpeg,.png,.webp | api_restart | — | .env (local, uncommitted) |
| `BACKUP_DIR` | /backups | api_restart | backend/core/operational_metrics.py | scripts/backup/restore.sh |
| `MAX_FILE_SIZE` | 10485760 | api_restart | backend/config.py<br>backend/routes/advanced_search.py<br>backend/routes/stats.py | .env (local, uncommitted) |
| `MAX_PHOTOS_PER_PERSON` | 1 | immediate | backend/services/image_processing.py | .env (local, uncommitted)<br>docker/docker-compose.cpu.yml |
| `MAX_STORAGE_GB` | 5000 | immediate | backend/core/data_retention.py<br>backend/routes/retention.py (by name) | .env (local, uncommitted)<br>.env.example<br>docker/docker-compose.cpu.yml<br>docker/docker-compose.prod.yml |
| `ML_ARTIFACT_DIR` | models/ml | api_restart | backend/ml/capabilities.py<br>backend/ml/dataset_builder.py<br>backend/ml/mlflow_tracking.py<br>backend/ml/registry_service.py<br>… +2 more | .env.example<br>docker-entrypoint.sh<br>docker/docker-compose.cpu.yml<br>docker/docker-compose.prod.yml |
| `SAVE_CROPPED_IMAGES` | False | immediate | backend/services/image_processing.py | .env.example<br>docker/docker-compose.cpu.yml<br>docker/docker-compose.prod.yml |
| `SAVE_IMAGES` | True | immediate | backend/services/image_processing.py | .env (local, uncommitted)<br>.env.example<br>docker/docker-compose.cpu.yml |
| `SAVE_UNKNOWN_FACES` | True | immediate | backend/services/image_processing.py | .env (local, uncommitted)<br>docker/docker-compose.cpu.yml |
| `SAVE_WEBHOOK_IMAGES` | False | immediate | backend/routes/webhook.py<br>backend/services/queue_worker.py | .env.example<br>docker/docker-compose.cpu.yml<br>docker/docker-compose.prod.yml |
| `STORAGE_DIR` | /app/storage | api_restart | backend/core/data_retention.py<br>backend/core/enrollment_service.py<br>backend/core/identity_clustering.py<br>backend/core/identity_loader.py<br>… +8 more | .env (local, uncommitted)<br>.env.example<br>docker-entrypoint.sh<br>docker/docker-compose.cpu.yml<br>… +1 more |

## tracking

| Setting | In use | Applied | Python consumers | Docker / env / shell |
|---|---|---|---|---|
| `ALERT_NOTIFICATION_WINDOW_HOURS` | 1.0 | immediate | backend/core/face_tracker.py<br>backend/routes/settings.py<br>backend/routes/stats.py | — |
| `DASHBOARD_FACE_DISPLAY_HOURS` | 24.0 | immediate | backend/routes/settings.py<br>backend/routes/stats.py<br>backend/routes/websocket.py | — |
| `FACE_TRACKING_ENABLED` | True | api_restart | backend/config.py | .env (local, uncommitted) |
| `FACE_TRACKING_MAX_ENTRIES` | 2000 | api_restart | backend/config.py | .env (local, uncommitted)<br>docker/docker-compose.cpu.yml |
| `FACE_TRACKING_MAX_MEMORY_MB` | 1000 | api_restart | backend/config.py | .env (local, uncommitted)<br>docker/docker-compose.cpu.yml |
| `FACE_TRACKING_SIMILARITY_THRESHOLD` | 0.95 | api_restart | backend/config.py | .env (local, uncommitted) |
| `FACE_TRACKING_WINDOW_SECONDS` | 60 | api_restart | backend/config.py | .env (local, uncommitted) |
| `SHOW_UNKNOWN_FACES_ON_DASHBOARD` | False | immediate | backend/routes/stats.py | — |
| `SKIP_UNKNOWN_FACES` | False | immediate | backend/services/image_processing.py | .env (local, uncommitted)<br>docker/docker-compose.cpu.yml |
| `UNKNOWN_FACE_DISPLAY_HOURS` | 24.0 | next_request | backend/routes/identities.py | — |

## Settings with no consumer found

None: every setting on the page is read by application code, a compose file or a script.

## Declared in config.py but read nowhere (not on the page)

These fields exist on the config.py Settings class with the value below, but no Python module, compose file or script reads them; they are candidates for retirement, not for the page.

| Setting | Current value |
|---|---|
| `REDIS_POOL_SIZE` | 25 |
| `CACHE_LOCAL_SIZE` | 20000 |
| `CACHE_VERSION` | v1 |
| `CACHE_WARNING_ENABLED` | True |
| `CACHE_WARNING_INTERVAL` | 300 |
| `MAP_DEFAULT_LAT` | 33.87 |
| `MAP_DEFAULT_LON` | 35.85 |
| `MAP_DEFAULT_ZOOM` | 10 |
| `VECTOR_INDEX_AUTOSAVE_INTERVAL_SECONDS` | 900.0 |
| `VECTOR_INDEX_RECONCILE_INTERVAL_SECONDS` | 3600.0 |
| `GPU_BATCH_SIZE` | 32 |
| `CPU_BATCH_SIZE` | 10 |
| `BATCH_WRITE_MAX_WAIT` | 5.0 |

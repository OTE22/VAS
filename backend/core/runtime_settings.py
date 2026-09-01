"""
Runtime Settings Service
========================
The single source of truth for TYPED setting metadata and for applying
database-stored settings to the RUNNING application.

Why this exists (2026-07 settings audit):
- The admin settings page persisted values to the `settings` table, but a
  config->DB sync reverted every edit on the next page load, and (except for
  two hardcoded keys) nothing ever pushed a saved value into the running
  process. Saved settings were dead writes.
- `config.settings` is a per-process pydantic object built from env once at
  import. Most hot-path consumers read it per call (`getattr(settings, KEY)`),
  which means mutating that object IS a correct dynamic-apply mechanism for
  those keys (single-worker deployment; see note below).

Apply modes (honesty contract — what a save really does):
    immediate          applied to the running process right now
    next_request       applied now; takes effect on the next request that reads it
    next_job_run       applied now; the periodic job reads it at its next run
    api_restart        stored; requires the api container process to restart
    worker_restart     stored; requires queue-worker restart (same process here)
    container_recreate stored; requires `docker compose up -d` (env/bind level)
    index_rebuild      stored; requires rebuilding the vector index

Value precedence (deviation from naive env-always-wins, documented):
    admin-modified DB value  >  environment  >  field default
A DB row that still equals the environment value is just a seeded mirror; a
row that DIFFERS from env is an intentional admin override and wins. The API
exposes `env_value` whenever env and DB disagree, so the override is visible.

Multi-worker caveat: apply mutates this process's `config.settings` only.
This deployment runs WORKERS=1 (docker-compose), so that is complete. With
N>1 workers a cross-process signal (pub/sub) would be required — do not raise
WORKERS without revisiting this module.
"""

import logging
import os
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional

from config import settings

logger = logging.getLogger(__name__)


class SettingValidationError(ValueError):
    """Raised when a raw value fails typed validation for a setting."""


@dataclass
class SettingMeta:
    value_type: str = "string"          # boolean | integer | float | string | json
    unit: Optional[str] = None
    minimum: Optional[float] = None
    maximum: Optional[float] = None
    allowed_values: Optional[List[str]] = None
    apply_mode: str = "api_restart"
    allow_empty: bool = False
    description: Optional[str] = None


# ---------------------------------------------------------------------------
# Hand-curated registry: the settings whose runtime behavior we GUARANTEE.
# Everything else gets auto-derived metadata (type/default from pydantic) and
# an apply_mode from category classification — honestly restart-required.
# ---------------------------------------------------------------------------

_DYN = "immediate"          # consumers read getattr(settings, KEY) per call
_JOB = "next_job_run"       # periodic jobs re-read at the start of each run

SETTINGS_REGISTRY: Dict[str, SettingMeta] = {
    # --- Recognition behavior (per-call consumers; see identity_service) ---
    "SIMILARITY_THRESHOLD": SettingMeta("float", "0-1", 0.05, 1.0, None, _DYN,
        description="Cosine similarity required to match a KNOWN identity"),
    "UNKNOWN_SIMILARITY_THRESHOLD": SettingMeta("float", "0-1", 0.05, 1.0, None, _DYN,
        description="Cosine similarity required to match an existing UNKNOWN identity"),
    "IDENTITY_ENRICH_MIN_SIMILARITY": SettingMeta("float", "0-1", 0.0, 1.0, None, _DYN),
    "IDENTITY_ENRICH_MIN_QUALITY": SettingMeta("float", "0-1", 0.0, 1.0, None, _DYN),
    "IDENTITY_NEAR_DUPLICATE_MIN": SettingMeta("float", "0-1", 0.0, 1.0, None, _DYN,
        description="Similarity at which an enrichment candidate is a duplicate of a stored view"),
    "IDENTITY_INGEST_TOP_K": SettingMeta("integer", "count", 1, 100, None, _DYN,
        description="Candidate depth when matching an ingested face against known identities"),

    # --- Enrollment review bands (read per upload in enrollment_service) ---
    # The min/max here bound a single edit; the RELATIONSHIP between the four
    # (floor <= strong, pool > shown) is enforced at startup by
    # backend/security/config_guard.py, which this API cannot express.
    "ENROLL_STRONG_MATCH_MIN": SettingMeta("float", "0-1", 0.0, 1.0, None, _DYN,
        description="Similarity at/above which an upload is treated as an existing person"),
    "ENROLL_CANDIDATE_MIN": SettingMeta("float", "0-1", 0.0, 1.0, None, _DYN,
        description="Similarity floor for offering a candidate; below this an upload enrolls directly"),
    "ENROLL_CANDIDATE_POOL": SettingMeta("integer", "count", 2, 500, None, _DYN,
        description="Nearest embeddings retrieved before collapsing to one row per identity"),
    "ENROLL_MAX_CANDIDATES": SettingMeta("integer", "count", 1, 50, None, _DYN,
        description="Candidate identities shown to the administrator for review"),

    # --- Image saving flags (per-call in image_processing/queue_worker) ---
    "SAVE_IMAGES": SettingMeta("boolean", "on/off", apply_mode=_DYN),
    "SAVE_UNKNOWN_FACES": SettingMeta("boolean", "on/off", apply_mode=_DYN),
    "SKIP_UNKNOWN_FACES": SettingMeta("boolean", "on/off", apply_mode=_DYN),
    "SAVE_CROPPED_IMAGES": SettingMeta("boolean", "on/off", apply_mode=_DYN),
    "SAVE_WEBHOOK_IMAGES": SettingMeta("boolean", "on/off", apply_mode=_DYN),
    "MAX_PHOTOS_PER_PERSON": SettingMeta("integer", "count", 0, 1000, None, _DYN),

    # --- Webhook ingress (per-call in webhook.py) ---
    "WEBHOOK_MAX_BODY_MB": SettingMeta("integer", "MB", 1, 500, None, "next_request"),
    "WEBHOOK_DEDUP_TTL_SECONDS": SettingMeta("integer", "sec", 0, 3600, None, "next_request"),

    # --- Dashboard / alerts (per-call in stats.py; tracker via property) ---
    "DASHBOARD_FACE_DISPLAY_HOURS": SettingMeta("float", "hours", 0.1, 168, None, _DYN),
    # 0 = disabled (show all); read per request in identities.list_unknown_identities
    "UNKNOWN_FACE_DISPLAY_HOURS": SettingMeta("float", "hours", 0, 8760, None, "next_request"),
    "ALERT_NOTIFICATION_WINDOW_HOURS": SettingMeta("float", "hours", 0.1, 168, None, _DYN),

    # --- Cache / storage (per-call) ---
    "CACHE_TTL": SettingMeta("integer", "sec", 1, 86400 * 7, None, _DYN),
    "MAX_STORAGE_GB": SettingMeta("integer", "GB", 1, 100000, None, _DYN),

    # --- Search (per-call in batch_search/advanced_search) ---
    "BATCH_SEARCH_MAX_IMAGES": SettingMeta("integer", "count", 1, 500, None, _DYN),
    "BATCH_SEARCH_TIMEOUT_SECONDS": SettingMeta("integer", "sec", 10, 3600, None, _DYN),
    "BATCH_SEARCH_MAX_CONCURRENCY": SettingMeta("integer", "count", 1, 64, None, _DYN),
    "SEARCH_DEFAULT_TOP_K": SettingMeta("integer", "count", 1, 1000, None, _DYN,
        description="Matches per query face when the caller does not specify"),
    "SEARCH_MAX_TOP_K": SettingMeta("integer", "count", 1, 1000, None, _DYN,
        description="Hard ceiling on matches per query face"),
    "SEARCH_RETRIEVAL_FLOOR": SettingMeta("float", "0-1", 0.0, 1.0, None, _DYN,
        description="How deep the vector index is searched, before the display threshold filters"),
    "SEARCH_CANDIDATE_MULTIPLIER": SettingMeta("integer", "factor", 1, 100, None, _DYN),
    "SEARCH_FILTERED_CANDIDATE_MULTIPLIER": SettingMeta("integer", "factor", 1, 100, None, _DYN),
    "SEARCH_MIN_QUALITY_THRESHOLD": SettingMeta("float", "0-1", 0.0, 1.0, None, _DYN,
        description="Face quality below which a search is not attempted"),
    "SEARCH_QUALITY_WARNING_THRESHOLD": SettingMeta("float", "0-1", 0.0, 1.0, None, _DYN,
        description="Face quality below which the result carries a quality warning"),

    # --- Confidence bands (read per call in advanced_search, published to the UI) ---
    "CONFIDENCE_VERY_HIGH_MIN": SettingMeta("float", "0-1", 0.0, 1.0, None, _DYN),
    "CONFIDENCE_HIGH_MIN": SettingMeta("float", "0-1", 0.0, 1.0, None, _DYN),
    "CONFIDENCE_MEDIUM_MIN": SettingMeta("float", "0-1", 0.0, 1.0, None, _DYN),
    "CONFIDENCE_LOW_MIN": SettingMeta("float", "0-1", 0.0, 1.0, None, _DYN),

    # --- Live alerts (per-call in live_alert_service) ---
    "LIVE_ALERT_MAX_PER_USER": SettingMeta("integer", "count", 1, 10000, None, _DYN),
    "LIVE_ALERT_MAX_PER_IDENTITY": SettingMeta("integer", "count", 1, 1000, None, _DYN),
    "LIVE_ALERT_MIN_SIMILARITY": SettingMeta("float", "0-1", 0.0, 1.0, None, _DYN),
    "LIVE_ALERT_CLIP_DURATION_SECONDS": SettingMeta("integer", "sec", 1, 3600, None, _DYN),
    "LIVE_ALERT_DEFAULT_COOLDOWN_MINUTES": SettingMeta("integer", "min", 0, 10080, None, _DYN),
    # Notification transports. Read per call by the readiness probe on the
    # live-alerts page, which reported both channels as unconfigured forever
    # because these three names were declared nowhere. Empty = disabled.
    "SMTP_HOST": SettingMeta("string", "hostname", apply_mode=_DYN, allow_empty=True),
    "SMS_PROVIDER_URL": SettingMeta("string", "url", apply_mode=_DYN, allow_empty=True),
    "TWILIO_ACCOUNT_SID": SettingMeta("string", "sid", apply_mode=_DYN, allow_empty=True),

    # --- Feature flags read per call ---
    "WATCHLIST_ENABLED": SettingMeta("boolean", "on/off", apply_mode=_DYN),
    "SHOW_UNKNOWN_FACES_ON_DASHBOARD": SettingMeta("boolean", "on/off", apply_mode=_DYN),
    # These six were declared with descriptions asserting they enable/disable a
    # feature, rendered as editable switches, and read by nothing. Each now
    # gates its endpoint, so registering them as immediate is truthful.
    "RELATED_IDENTITIES_ENABLED": SettingMeta("boolean", "on/off", apply_mode=_DYN),
    "TEMPORAL_PATTERNS_ENABLED": SettingMeta("boolean", "on/off", apply_mode=_DYN),
    "CROSS_CAMERA_TRACKING_ENABLED": SettingMeta("boolean", "on/off", apply_mode=_DYN),
    "BATCH_SEARCH_ENABLED": SettingMeta("boolean", "on/off", apply_mode=_DYN),
    "EXPORT_RESULTS_ENABLED": SettingMeta("boolean", "on/off", apply_mode=_DYN),
    "NEGATIVE_SEARCH_ENABLED": SettingMeta("boolean", "on/off", apply_mode=_DYN),
    "AUTO_THRESHOLD_LEARNING_ENABLED": SettingMeta("boolean", "on/off", apply_mode=_DYN),
    "TRAJECTORY_PREDICTION_ENABLED": SettingMeta("boolean", "on/off", apply_mode=_DYN),
    "PIPELINE_AWARE_CLUSTERING_ENABLED": SettingMeta("boolean", "on/off", apply_mode=_DYN),

    # --- Pagination (shared dependency in backend/utils/pagination.py) ---
    "API_DEFAULT_PAGE_SIZE": SettingMeta("integer", "rows", 1, 1000, None, _DYN,
        description="Rows per page when a listing endpoint's caller does not specify"),
    "API_MAX_PAGE_SIZE": SettingMeta("integer", "rows", 1, 10000, None, _DYN,
        description="Hard ceiling on rows per page"),

    # --- Face quality gates (properties on the module-level scorer) ---
    "FACE_QUALITY_THRESHOLD_BLUR": SettingMeta("float", "0-1", 0.0, 1.0, None, _DYN),
    "FACE_QUALITY_THRESHOLD_LIGHTING": SettingMeta("float", "0-1", 0.0, 1.0, None, _DYN),
    "FACE_QUALITY_THRESHOLD_SIZE": SettingMeta("integer", "px", 1, 4096, None, _DYN),
    "FACE_QUALITY_THRESHOLD_ANGLE": SettingMeta("float", "degrees", 1.0, 90.0, None, _DYN),

    # --- Clustering weights and bands (properties; were frozen at import) ---
    "PIPELINE_SIMILARITY_WEIGHT": SettingMeta("float", "0-1", 0.0, 1.0, None, _DYN),
    "EMBEDDING_SIMILARITY_WEIGHT": SettingMeta("float", "0-1", 0.0, 1.0, None, _DYN),
    "CROSS_PIPELINE_SIMILARITY_THRESHOLD": SettingMeta("float", "0-1", 0.0, 1.0, None, _DYN),
    "SIMILARITY_QUALITY_FLOOR": SettingMeta("float", "0-1", 0.0, 1.0, None, _DYN),
    "CLUSTER_ACTIVE_WINDOW_DAYS": SettingMeta("integer", "days", 1, 3650, None, _JOB),
    "CLUSTER_TRAINED_MODEL_MARGIN": SettingMeta("float", "0-1", 0.0, 1.0, None, _DYN),

    # --- Intelligence ---
    "INTEL_QUERY_TIMEOUT_SECONDS": SettingMeta("float", "sec", 1.0, 600.0, None, _DYN,
        description="Seconds a heavy analysis may run before returning 503"),

    # --- Security intelligence (per-call in security_intelligence_service) ---
    "ANOMALY_DEVIATION_SIGMA": SettingMeta("float", "sigma", 0.5, 10.0, None, _DYN,
        description="Circular-hour deviation (in effective std devs) that flags off-schedule"),
    "ANOMALY_MIN_BASELINE_SAMPLES": SettingMeta("integer", "count", 1, 1000, None, _DYN),
    "ANOMALY_MIN_STD_HOURS": SettingMeta("float", "hours", 0.0, 12.0, None, _DYN),
    "ANOMALY_MAX_ITEMS": SettingMeta("integer", "count", 1, 5000, None, _DYN),
    "PATTERN_SCAN_LIMIT": SettingMeta("integer", "rows", 100, 1000000, None, _DYN),
    "PATTERN_MAX_PER_TYPE": SettingMeta("integer", "count", 1, 5000, None, _DYN),
    "PATTERN_OFF_HOURS_START": SettingMeta("integer", "hour", 0, 23, None, _DYN,
        description="Unusual-timing window start (site-local hour; window may wrap midnight)"),
    "PATTERN_OFF_HOURS_END": SettingMeta("integer", "hour", 0, 23, None, _DYN),
    "PATTERN_RAPID_WINDOW_SECONDS": SettingMeta("integer", "sec", 10, 86400, None, _DYN),
    "PATTERN_RAPID_MIN_SPEED_KMH": SettingMeta("float", "km/h", 1.0, 500.0, None, _DYN),
    "NETWORK_FALLBACK_MAX_IDENTITIES": SettingMeta("integer", "count", 10, 10000, None, _DYN),
    # Risk platform (per-call consumers in time_context / assessment_service /
    # rate_limiter / threshold_store)
    "DEFAULT_SITE_TIMEZONE": SettingMeta("string", "IANA tz", apply_mode=_DYN,
        description="Business-hour timezone fallback when a pipeline has none (storage stays UTC)"),
    "WEEKEND_DAYS": SettingMeta("string", "Mon=0 list", apply_mode=_DYN),
    "ANOMALY_HOLIDAYS": SettingMeta("string", "ISO dates", apply_mode=_DYN),
    "ANOMALY_BASELINE_MAX_DAYS": SettingMeta("integer", "days", 7, 3650, None, _DYN),
    "ASSESSMENT_DEDUP_WINDOW_MINUTES": SettingMeta("integer", "min", 1, 1440, None, _DYN),
    "THRESHOLD_MIN_SAMPLES_FOR_ACTIVATION": SettingMeta("integer", "count", 1, 100000, None, _DYN),
    "API_RATE_LIMIT_ENABLED": SettingMeta("boolean", "on/off", apply_mode=_DYN),
    "RATE_LIMIT_DEFAULT_PER_MINUTE": SettingMeta("integer", "req/min", 0, 100000, None, _DYN),
    "RATE_LIMIT_HEAVY_PER_MINUTE": SettingMeta("integer", "req/min", 0, 100000, None, _DYN),
    # --- ML pipeline (per-call consumers in backend/ml/) ---
    "ML_DECISION_MODE": SettingMeta("string", "mode", None, None,
        ["rules", "shadow", "hybrid", "ml"], _DYN,
        description="rules is the production-safe default; shadow needs an approved anomaly model; hybrid/ml are gated this release"),
    "ML_INFERENCE_TIMEOUT_MS": SettingMeta("integer", "ms", 100, 60000, None, _DYN),
    "ML_SHADOW_TIMEOUT_MS": SettingMeta("integer", "ms", 100, 60000, None, _DYN),
    "ML_MODEL_CACHE_TTL_SECONDS": SettingMeta("integer", "sec", 5, 3600, None, _DYN),
    "ML_SUPERVISED_MIN_LABELS": SettingMeta("integer", "count", 10, 1000000, None, _DYN),
    "ML_SUPERVISED_MIN_PER_CLASS": SettingMeta("integer", "count", 5, 100000, None, _DYN),
    "ML_COLLECTOR_LATE_GRACE_MINUTES": SettingMeta("integer", "min", 0, 10080, None, _JOB),
    "ML_FEATURE_SAMPLED_FULL_VECTOR_RATE": SettingMeta("float", "0-1", 0.0, 1.0, None, _DYN,
        description="Default 0.0 (disabled). Enabling stores full feature vectors on sampled predictions — requires documented privacy/retention justification"),
    "ML_GRAPH_MIN_NODES": SettingMeta("integer", "count", 1, 100000, None, _DYN),
    "ML_GRAPH_MIN_EDGES": SettingMeta("integer", "count", 1, 1000000, None, _DYN),
    "ML_GRAPH_MIN_OBSERVATION_DAYS": SettingMeta("integer", "days", 1, 3650, None, _DYN),
    "ML_GRAPH_MIN_PAIR_APPEARANCES": SettingMeta("integer", "count", 1, 10000, None, _DYN),
    "ML_DRIFT_CHECK_INTERVAL_HOURS": SettingMeta("integer", "hours", 1, 720, None, _JOB),
    "ML_DRIFT_MIN_SAMPLES": SettingMeta("integer", "count", 10, 1000000, None, _JOB),
    "ML_DRIFT_PSI_WARNING": SettingMeta("float", "psi", 0.01, 10.0, None, _JOB),
    "ML_DRIFT_PSI_CRITICAL": SettingMeta("float", "psi", 0.01, 10.0, None, _JOB),
    "ML_SCIENTIFIC_MIN_HISTORY_DAYS": SettingMeta("integer", "days", 0, 3650, None, _DYN),
    "ML_SCIENTIFIC_MIN_MEDIAN_APPEARANCES": SettingMeta("integer", "count", 0, 100000, None, _DYN),
    "ML_EVIDENCE_MIN_REVIEWED_TOTAL": SettingMeta("integer", "count", 0, 1000000, None, _DYN),
    "ML_EVIDENCE_MIN_REVIEWED_PER_BAND": SettingMeta("integer", "count", 0, 1000000, None, _DYN),
    "ML_EVIDENCE_REQUIRE_INDEPENDENT_REVIEW": SettingMeta("boolean", "on/off", apply_mode=_DYN),
    "ML_PREDICTION_RETENTION_DAYS": SettingMeta("integer", "days", 7, 3650, None, _JOB),
    "ML_SNAPSHOT_RETENTION_DAYS": SettingMeta("integer", "days", 7, 3650, None, _JOB),
    "ML_DRIFT_REPORT_RETENTION_DAYS": SettingMeta("integer", "days", 7, 3650, None, _JOB),
    "ML_MAX_ARTIFACT_MB": SettingMeta("integer", "MB", 1, 4096, None, _DYN),
    "MLFLOW_ENABLED": SettingMeta("boolean", "on/off", apply_mode=_DYN,
        description="Flag only — reported operational only when the dependency imports and the backend responds"),
    "OPTUNA_ENABLED": SettingMeta("boolean", "on/off", apply_mode=_DYN),
    "XGBOOST_ENABLED": SettingMeta("boolean", "on/off", apply_mode=_DYN),
    "SHAP_ENABLED": SettingMeta("boolean", "on/off", apply_mode=_DYN),

    # Existing per-call consumers that predate this registry section
    "RELATED_IDENTITY_MIN_CO_APPEARANCES": SettingMeta("integer", "count", 1, 100, None, _DYN),
    "RELATED_IDENTITY_TIME_WINDOW_MINUTES": SettingMeta("integer", "min", 1, 1440, None, _DYN),
    "MULTI_CAMERA_CO_APPEARANCE_ENABLED": SettingMeta("boolean", "on/off", apply_mode=_DYN),
    "MULTI_CAMERA_DISTANCE_METERS": SettingMeta("float", "m", 10.0, 100000.0, None, _DYN),
    "MULTI_CAMERA_TIME_WINDOW_MINUTES": SettingMeta("integer", "min", 1, 1440, None, _DYN),
    "MULTI_CAMERA_MIN_CO_APPEARANCES": SettingMeta("integer", "count", 1, 100, None, _DYN),

    # --- Retention (jobs re-read at each run start after the rework) ---
    "DATA_RETENTION_DAYS": SettingMeta("integer", "days", 1, 3650, None, _JOB),
    "CLEANUP_INTERVAL_HOURS": SettingMeta("integer", "hours", 1, 168, None, _JOB),
    "LOGS_LIFE_TIME_HOURS": SettingMeta("integer", "hours", 1, 8760, None, _JOB),
    "SNAPSHOT_RETENTION_DAYS": SettingMeta("integer", "days", 1, 3650, None, _JOB),
    "INACTIVE_THRESHOLD_DAYS": SettingMeta("integer", "days", 1, 3650, None, _JOB),
    "IDENTITY_CLEANUP_INTERVAL_HOURS": SettingMeta("integer", "hours", 1, 168, None, _JOB),
    "MAX_EMBEDDINGS_PER_IDENTITY": SettingMeta("integer", "count", 1, 100, None, _JOB),
    "SEARCH_HISTORY_RETENTION_DAYS": SettingMeta("integer", "days", 1, 3650, None, _JOB),
    "SEARCH_HISTORY_MAX_PER_USER": SettingMeta("integer", "count", 10, 100000, None, _JOB),
    "AUDIT_LOG_RETENTION_DAYS": SettingMeta("integer", "days", 7, 3650, None, _JOB),
    "TASK_HISTORY_RETENTION_DAYS": SettingMeta("integer", "days", 1, 3650, None, _JOB),
    # Real float: the clustering job sleeps a fraction of an hour on startup.
    # Auto-derivation typed it from an int-looking default and rejected 0.5.
    "CLUSTER_STARTUP_DELAY_HOURS": SettingMeta("float", "hours", 0.0, 168.0, None, _JOB),

    # --- Curated restart-required (correct, honest labels) ---
    "CONFIDENCE_THRESHOLD": SettingMeta("float", "0-1", 0.05, 1.0, None, "api_restart",
        description="Face-detector confidence (bound at model load)"),
    "QUEUE_WORKERS": SettingMeta("integer", "count", 1, 64, None, "api_restart"),
    "MAX_QUEUE_SIZE": SettingMeta("integer", "count", 10, 100000, None, "api_restart"),
    "MAX_CONCURRENT_REQUESTS": SettingMeta("integer", "count", 1, 1000, None, "api_restart"),
    "INFERENCE_WORKERS": SettingMeta("integer", "count", 1, 32, None, "api_restart"),
    "MAX_CONCURRENT_INFERENCE": SettingMeta("integer", "count", 1, 32, None, "api_restart"),
    "MAX_CONCURRENT_INFERENCE_PER_PIPELINE": SettingMeta("integer", "count", 1, 16, None, "api_restart"),
    "BATCH_WRITE_SIZE": SettingMeta("integer", "count", 1, 1000, None, "api_restart"),
    "BATCH_WRITE_INTERVAL": SettingMeta("float", "sec", 0.1, 60, None, "api_restart"),
    "PIPELINE_BATCH_SIZE": SettingMeta("integer", "count", 1, 50, None, "api_restart"),
    "SQL_AGENT_MAX_CONCURRENT": SettingMeta("integer", "count", 1, 16, None, "api_restart"),
    "SQL_AGENT_TOTAL_TIMEOUT": SettingMeta("integer", "sec", 30, 3600, None, "api_restart"),
    "SQL_AGENT_MAX_REASONING_STEPS": SettingMeta("integer", "count", 1, 10, None, "api_restart"),
    "SQL_AGENT_MAX_REPLANS": SettingMeta("integer", "count", 0, 5, None, "api_restart"),
    "SQL_AGENT_MAX_EXECUTION_RETRIES": SettingMeta("integer", "count", 0, 3, None, "api_restart"),
    "LOG_LEVEL": SettingMeta("string", None, None, None,
        ["DEBUG", "INFO", "WARNING", "ERROR"], "api_restart"),
    "VECTOR_BACKEND": SettingMeta("string", "backend", None, None,
        ["faiss", "pgvector"], "api_restart"),
    "PGVECTOR_INDEX_TYPE": SettingMeta("string", "index", None, None,
        ["hnsw", "ivfflat"], "index_rebuild"),
    "PGVECTOR_HNSW_M": SettingMeta("integer", "index param", 4, 128, None, "index_rebuild"),
    "PGVECTOR_HNSW_EF_CONSTRUCTION": SettingMeta("integer", "index param", 8, 1024, None, "index_rebuild"),
    "PGVECTOR_HNSW_EF_SEARCH": SettingMeta("integer", "index param", 8, 1024, None, "api_restart"),
    "PGVECTOR_IVFFLAT_LISTS": SettingMeta("integer", "index param", 1, 32768, None, "index_rebuild"),
    # Search-time probe count — the recall/latency knob for an existing ivfflat
    # index. Only ..._LISTS was registered, so the one an operator actually
    # tunes fell through to auto-derived metadata.
    "PGVECTOR_IVFFLAT_PROBES": SettingMeta("integer", "index param", 1, 32768, None, "api_restart"),
    "WORKERS": SettingMeta("integer", "count", 1, 64, None, "container_recreate"),
    # Read by scripts/backup/backup.sh and backup-loop.sh in the SEPARATE
    # `backup` service, via environment variables set in
    # docker-compose.prod.yml. Applying them to this process would do nothing
    # at all, so container_recreate is the only honest mode — the operator has
    # to change the environment and recreate that service.
    "BACKUP_RETENTION_DAYS": SettingMeta("integer", "days", 1, 3650, None, "container_recreate",
        description="Days of backups kept by the backup service (scripts/backup/backup.sh)"),
    "BACKUP_INTERVAL_SECONDS": SettingMeta("integer", "sec", 3600, 2592000, None, "container_recreate",
        description="Seconds between automatic backup runs (scripts/backup/backup-loop.sh)"),
}

# Categories whose non-curated keys can never be live (bind/env/connection level)
_CONTAINER_LEVEL_CATEGORIES = {"server", "database", "cache"}

# Keys whose apply hooks the service guarantees (subset of registry, _DYN/_JOB/next_request)
DYNAMIC_APPLY_MODES = {"immediate", "next_request", "next_job_run"}

# Modes that can never be satisfied by writing to this process's settings
# object, not even at boot: they describe how the container itself was
# launched (bind address, port, mounts, worker count). Applying one would make
# `effective_value` disagree with reality, which is worse than not applying it.
_NEVER_IN_PROCESS_APPLY_MODES = {"container_recreate"}

# last time apply_to_runtime succeeded, per key (in-process)
_last_applied_at: Dict[str, datetime] = {}


# ---------------------------------------------------------------------------
# Metadata
# ---------------------------------------------------------------------------

def _pydantic_field(key: str):
    return type(settings).model_fields.get(key)


def _derive_type(default: Any) -> str:
    if isinstance(default, bool):
        return "boolean"
    if isinstance(default, int):
        return "integer"
    if isinstance(default, float):
        return "float"
    if isinstance(default, (dict, list)):
        return "json"
    return "string"


def get_meta(key: str, category: Optional[str] = None) -> SettingMeta:
    """Registry metadata, or auto-derived metadata for non-curated keys."""
    meta = SETTINGS_REGISTRY.get(key)
    if meta:
        return meta

    default = get_default(key)
    value_type = _derive_type(default)
    apply_mode = ("container_recreate"
                  if (category or "").lower() in _CONTAINER_LEVEL_CATEGORIES
                  else "api_restart")
    return SettingMeta(value_type=value_type, apply_mode=apply_mode)


def get_default(key: str) -> Any:
    f = _pydantic_field(key)
    if f is None:
        return None
    return f.default


def has_key(key: str) -> bool:
    return _pydantic_field(key) is not None


# ---------------------------------------------------------------------------
# Expected nginx webhook body limit
# ---------------------------------------------------------------------------

# The nginx configs are baked into the API image by `COPY . .` - the same files
# tests/test_deployment_surface.py parses. IMPORTANT: this is the EXPECTED /
# deployment limit: the running nginx container mounts the HOST file, which can
# in principle differ from the copy baked into this image. Every message built
# from this value must therefore say "expected nginx webhook body limit" and
# never claim to be the live value. The live limit is verified operationally
# (boundary POSTs through the running nginx), not from here.
_NGINX_CONF_CANDIDATES = ("/app/nginx.conf", "/app/nginx.prod.conf")


def expected_nginx_webhook_body_limit_mb() -> Optional[int]:
    """Parse client_max_body_size (integer MB) from the webhook location of the
    nginx config baked into this image. Returns None - never raises - when no
    candidate file exists or the location/directive cannot be parsed. Extraction
    mirrors tests/test_deployment_surface.py so the two cannot disagree."""
    import re
    for path in _NGINX_CONF_CANDIDATES:
        try:
            with open(path, encoding="utf-8") as f:
                text = f.read()
        except OSError:
            continue
        if "location ~ ^/(api/)?webhook/" not in text:
            continue
        location = text.split("location ~ ^/(api/)?webhook/", 1)[1].split("}", 1)[0]
        match = re.search(r"client_max_body_size\s+(\d+)m", location, re.I)
        if match:
            return int(match.group(1))
    return None


# ---------------------------------------------------------------------------
# Typed parsing / validation
# ---------------------------------------------------------------------------

_TRUE = {"true", "1", "yes", "on"}
_FALSE = {"false", "0", "no", "off"}


def typed_parse(key: str, raw: Optional[str], category: Optional[str] = None) -> Any:
    """Parse + validate a raw string for a setting. Raises SettingValidationError.

    Explicit None/empty handling: `0`, `false`, `0.0` are VALID values; empty
    string is valid only when the setting allows it.
    """
    meta = get_meta(key, category)

    if raw is None or raw == "":
        if meta.allow_empty:
            return ""
        raise SettingValidationError(f"{key}: a value is required")

    raw = raw.strip()

    if meta.value_type == "boolean":
        low = raw.lower()
        if low in _TRUE:
            value: Any = True
        elif low in _FALSE:
            value = False
        else:
            raise SettingValidationError(
                f"{key}: '{raw}' is not a boolean (use true/false/1/0/yes/no/on/off)")
    elif meta.value_type == "integer":
        try:
            value = int(raw)
        except ValueError:
            raise SettingValidationError(f"{key}: '{raw}' is not an integer")
    elif meta.value_type == "float":
        try:
            value = float(raw)
        except ValueError:
            raise SettingValidationError(f"{key}: '{raw}' is not a number")
    elif meta.value_type == "json":
        import json
        try:
            value = json.loads(raw)
        except json.JSONDecodeError as e:
            raise SettingValidationError(f"{key}: invalid JSON ({e.msg})")
    else:
        value = raw

    if meta.minimum is not None and isinstance(value, (int, float)) and not isinstance(value, bool):
        if value < meta.minimum:
            raise SettingValidationError(f"{key}: {value} is below the minimum ({meta.minimum:g})")
    if meta.maximum is not None and isinstance(value, (int, float)) and not isinstance(value, bool):
        if value > meta.maximum:
            raise SettingValidationError(f"{key}: {value} is above the maximum ({meta.maximum:g})")
    if meta.allowed_values is not None and str(value) not in meta.allowed_values:
        raise SettingValidationError(
            f"{key}: '{value}' is not one of {meta.allowed_values}")

    return value


def to_stored_string(value: Any) -> str:
    """Canonical string form for DB storage (booleans lowercase, json compact)."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (dict, list)):
        import json
        return json.dumps(value)
    return str(value)


# ---------------------------------------------------------------------------
# Source / effective value
# ---------------------------------------------------------------------------

def env_value_of(key: str) -> Optional[str]:
    """The raw environment value for this key, if the env sets it."""
    f = _pydantic_field(key)
    env_name = key
    return os.environ.get(env_name)


def effective_value(key: str) -> Any:
    """The value the running process actually uses right now."""
    return getattr(settings, key, None)


def describe_source(key: str, stored_raw: Optional[str], category: Optional[str] = None) -> Dict[str, Any]:
    """Classify where the effective value comes from + expose env divergence.

    Precedence implemented by hydrate/apply: admin-modified DB > env > default.
    A stored value equal to the env value is a seeded mirror (source=environment).
    """
    env_raw = env_value_of(key)
    default = get_default(key)

    stored_typed = None
    if stored_raw not in (None, "") or get_meta(key, category).allow_empty:
        try:
            stored_typed = typed_parse(key, stored_raw, category)
        except SettingValidationError:
            stored_typed = stored_raw  # legacy junk value; surface as-is

    env_typed = None
    if env_raw is not None:
        try:
            env_typed = typed_parse(key, env_raw, category)
        except SettingValidationError:
            env_typed = env_raw

    if stored_typed is not None and env_typed is not None and stored_typed != env_typed:
        source = "database"      # intentional admin override
        overridden = True        # env and DB disagree — shown to the admin
    elif env_raw is not None:
        source = "environment"
        overridden = False
    elif stored_typed is not None and stored_typed != default:
        source = "database"
        overridden = False
    else:
        source = "default"
        overridden = False

    return {
        "source": source,
        "overridden": overridden,
        "env_value": env_typed,
        "stored_value": stored_typed,
        "default_value": default,
    }


# ---------------------------------------------------------------------------
# Runtime application
# ---------------------------------------------------------------------------

def apply_to_runtime(key: str, value: Any, category: Optional[str] = None,
                     *, at_boot: bool = False) -> bool:
    """Push a validated value into the running process, if its apply_mode allows.

    Returns True when the running behavior now reflects the value (immediate /
    next_request / next_job_run), False when a restart-level action is needed.

    `at_boot=True` lifts the apply_mode gate, because boot IS the restart that
    an `api_restart` / `worker_restart` / `index_rebuild` save was waiting for.
    Without it those saves are durable but permanently inert: the row sits in
    the settings table, the UI promises "takes effect after a restart", and the
    restart re-reads env/defaults and ignores it forever. `container_recreate`
    is still refused — those are bind/env-level (ports, mounts) and setting them
    on this object would make `effective_value` report a value the container is
    not actually running under.
    """
    from backend.security.config_guard import SECURITY_CRITICAL_KEYS
    from backend.security.redaction import SECRET_SETTINGS

    # Security-critical settings are never mutable in-process. This function is
    # the only path that writes to the live settings object, so refusing here
    # closes the hole where an admin token could set ENVIRONMENT=development
    # and neutralize the production guard without restarting anything.
    # This holds at boot too: those keys come from env, never from the DB.
    if key in SECURITY_CRITICAL_KEYS:
        logger.warning(
            "[SETTINGS] ⛔ Refused runtime change to security-critical setting %s "
            "(restart with new configuration instead)", key
        )
        return False

    meta = get_meta(key, category)
    if at_boot:
        if meta.apply_mode in _NEVER_IN_PROCESS_APPLY_MODES:
            return False
    elif meta.apply_mode not in DYNAMIC_APPLY_MODES:
        return False

    try:
        setattr(settings, key, value)
    except Exception as e:
        logger.error("[SETTINGS] Failed to apply %s to runtime: %s", key, e)
        raise

    _last_applied_at[key] = datetime.utcnow()
    shown = "***" if key in SECRET_SETTINGS else repr(value)
    logger.info("[SETTINGS] ✅ Applied %s=%s to runtime (mode=%s)", key, shown, meta.apply_mode)
    return True


def last_applied_at(key: str) -> Optional[str]:
    ts = _last_applied_at.get(key)
    return ts.isoformat() if ts else None


async def hydrate_from_db(db) -> int:
    """At startup: load every admin-modified DB value into the running config.

    Only rows whose value differs from the environment/default (i.e. genuine
    admin edits) are applied — this is what makes saved settings survive
    container restarts.

    This deliberately does NOT filter on apply_mode. Startup is precisely the
    moment an `api_restart` / `worker_restart` / `index_rebuild` save becomes
    due; skipping those here meant the settings page promised "takes effect
    after a restart" for 111 of 170 keys and never kept it. `apply_to_runtime`
    still refuses security-critical keys and container-level modes.

    Call this as early as the database allows — before the components that read
    these values are constructed (see backend/lifespan.py phase 1.1). A value
    applied after its consumer has already captured it is applied too late.
    """
    from sqlalchemy import select
    from db_models import Setting

    applied = 0
    deferred: List[str] = []
    try:
        result = await db.execute(select(Setting))
        rows = result.scalars().all()
    except Exception as e:
        logger.warning("[SETTINGS] Hydration skipped (settings table unavailable): %s", e)
        return 0

    for row in rows:
        if not has_key(row.key):
            continue
        info = describe_source(row.key, row.value, row.category)
        if info["source"] != "database":
            continue  # seeded mirror of env/default — nothing to apply
        try:
            value = typed_parse(row.key, row.value, row.category)
            if apply_to_runtime(row.key, value, row.category, at_boot=True):
                applied += 1
            else:
                deferred.append(row.key)
        except SettingValidationError as e:
            logger.warning("[SETTINGS] Hydration skipped invalid stored value: %s", e)
        except Exception as e:
            # One unsettable key must not abort hydration for every other key.
            logger.warning("[SETTINGS] Hydration could not apply %s: %s", row.key, e)

    if applied:
        logger.info("[SETTINGS] ✅ Hydrated %d admin-modified setting(s) from database", applied)
    else:
        logger.info("[SETTINGS] Hydration complete: no admin-modified settings in database")
    if deferred:
        logger.warning(
            "[SETTINGS] %d admin-modified setting(s) need the container recreated "
            "to take effect: %s", len(deferred), ", ".join(sorted(deferred)))
    return applied

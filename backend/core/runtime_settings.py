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
    "IDENTITY_MAX_EMBEDDINGS": SettingMeta("integer", "count", 1, 100, None, _DYN),

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
    "WORKERS": SettingMeta("integer", "count", 1, 64, None, "container_recreate"),
}

# Categories whose non-curated keys can never be live (bind/env/connection level)
_CONTAINER_LEVEL_CATEGORIES = {"server", "database", "cache"}

# Keys whose apply hooks the service guarantees (subset of registry, _DYN/_JOB/next_request)
DYNAMIC_APPLY_MODES = {"immediate", "next_request", "next_job_run"}

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

def apply_to_runtime(key: str, value: Any, category: Optional[str] = None) -> bool:
    """Push a validated value into the running process, if its apply_mode allows.

    Returns True when the running behavior now reflects the value (immediate /
    next_request / next_job_run), False when a restart-level action is needed.
    """
    meta = get_meta(key, category)
    if meta.apply_mode not in DYNAMIC_APPLY_MODES:
        return False

    try:
        setattr(settings, key, value)
    except Exception as e:
        logger.error("[SETTINGS] Failed to apply %s to runtime: %s", key, e)
        raise

    _last_applied_at[key] = datetime.utcnow()
    logger.info("[SETTINGS] ✅ Applied %s=%r to runtime (mode=%s)", key, value, meta.apply_mode)
    return True


def last_applied_at(key: str) -> Optional[str]:
    ts = _last_applied_at.get(key)
    return ts.isoformat() if ts else None


async def hydrate_from_db(db) -> int:
    """At startup: load admin-modified DB values for dynamic keys into runtime.

    Only rows whose value differs from the environment/default (i.e. genuine
    admin edits) are applied — this is what makes saved settings survive
    container restarts.
    """
    from sqlalchemy import select
    from db_models import Setting

    applied = 0
    try:
        result = await db.execute(select(Setting))
        rows = result.scalars().all()
    except Exception as e:
        logger.warning("[SETTINGS] Hydration skipped (settings table unavailable): %s", e)
        return 0

    for row in rows:
        if not has_key(row.key):
            continue
        meta = get_meta(row.key, row.category)
        if meta.apply_mode not in DYNAMIC_APPLY_MODES:
            continue
        info = describe_source(row.key, row.value, row.category)
        if info["source"] != "database":
            continue  # seeded mirror of env/default — nothing to apply
        try:
            value = typed_parse(row.key, row.value, row.category)
            apply_to_runtime(row.key, value, row.category)
            applied += 1
        except SettingValidationError as e:
            logger.warning("[SETTINGS] Hydration skipped invalid stored value: %s", e)

    if applied:
        logger.info("[SETTINGS] ✅ Hydrated %d admin-modified setting(s) from database", applied)
    else:
        logger.info("[SETTINGS] Hydration complete: no admin-modified dynamic settings in database")
    return applied

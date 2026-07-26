"""
Settings Management Routes
==========================
Admin-only endpoints for managing system settings with audit logging.
"""

import os
import sys
import logging
import json
from typing import List, Optional, Dict, Any
from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException, status, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from pydantic import BaseModel, Field, validator
from config import settings as config_settings

# Add parent directory to path
parent_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if parent_dir not in sys.path:
    sys.path.insert(0, parent_dir)

from db_connection import get_db
from db_models import Setting, SettingsAuditLog, User
from backend.auth.auth_service import require_role, get_current_user
from backend.utils.identity_audit import get_client_info
from backend.core import runtime_settings
from backend.core.runtime_settings import SettingValidationError

logger = logging.getLogger(__name__)

# Settings must never be served stale by a browser or proxy
NO_STORE = "no-store, no-cache, must-revalidate"


def _setting_payload(setting: Setting) -> Dict[str, Any]:
    """Typed, honest per-setting payload: stored vs effective value, source,
    apply mode, restart requirements. Legacy fields kept for compatibility."""
    meta = runtime_settings.get_meta(setting.key, setting.category)
    src = runtime_settings.describe_source(setting.key, setting.value, setting.category)
    effective = runtime_settings.effective_value(setting.key)
    dynamic = meta.apply_mode in runtime_settings.DYNAMIC_APPLY_MODES

    hidden = setting.is_sensitive
    return {
        "id": setting.id,
        "key": setting.key,
        # legacy raw-string field (frontend compat)
        "value": "***HIDDEN***" if hidden else (setting.value if setting.value is not None else ""),
        # typed schema
        "stored_value": "***HIDDEN***" if hidden else src["stored_value"],
        "effective_value": "***HIDDEN***" if hidden else effective,
        "default_value": "***HIDDEN***" if hidden else src["default_value"],
        "env_value": "***HIDDEN***" if hidden else src["env_value"],
        "value_type": meta.value_type,
        "unit": meta.unit,
        "minimum": meta.minimum,
        "maximum": meta.maximum,
        "allowed_values": meta.allowed_values,
        "source": src["source"],
        "overridden": src["overridden"],
        "apply_mode": meta.apply_mode,
        "requires_restart": meta.apply_mode in ("api_restart", "worker_restart", "container_recreate"),
        "requires_worker_restart": meta.apply_mode == "worker_restart",
        "requires_index_rebuild": meta.apply_mode == "index_rebuild",
        "is_dynamic": dynamic,
        "validation_status": "valid",
        "last_changed_at": setting.updated_at.isoformat() if setting.updated_at else None,
        "last_applied_at": runtime_settings.last_applied_at(setting.key),
        # existing fields
        "category": setting.category,
        "description": setting.description or f"{setting.key} configuration setting",
        "is_sensitive": setting.is_sensitive,
        "is_readonly": setting.is_readonly,
        "created_at": setting.created_at.isoformat() if setting.created_at else None,
        "updated_at": setting.updated_at.isoformat() if setting.updated_at else None,
        "display_value": "***HIDDEN***" if hidden else (setting.value if setting.value not in (None, "") else "(empty)"),
        "can_edit": not setting.is_readonly,
        "value_hint": _get_value_hint(meta.value_type),
    }

router = APIRouter(
    prefix="/api/settings",
    tags=["Settings Management"],
    responses={
        401: {
            "description": """
            **Unauthorized - Invalid or missing token**
            
            This error occurs when:
            - No Authorization header is provided
            - The token is invalid or expired
            - The token format is incorrect
            
            **Solution:**
            1. Login at `POST /api/auth/login` to get an access token
            2. Include the token in requests: `Authorization: Bearer <token>`
            3. In Swagger UI: Click "Authorize" button and enter `Bearer <token>`
            
            **Example Response:**
            ```json
            {
              "detail": "Authentication required. Please provide a Bearer token in the Authorization header."
            }
            ```
            """,
            "content": {
                "application/json": {
                    "example": {
                        "detail": "Authentication required. Please provide a Bearer token in the Authorization header."
                    }
                }
            }
        },
        403: {
            "description": """
            **Forbidden - Admin access required**
            
            This error occurs when:
            - The user is authenticated but doesn't have admin role
            - The user's account is inactive or blocked
            
            **Solution:**
            - Login with an admin account
            - Contact system administrator to grant admin access
            
            **Example Response:**
            ```json
            {
              "detail": "Access denied. Required role: admin"
            }
            ```
            """,
            "content": {
                "application/json": {
                    "example": {
                        "detail": "Access denied. Required role: admin"
                    }
                }
            }
        },
        404: {
            "description": """
            **Setting not found**
            
            This error occurs when:
            - The setting key doesn't exist in the system
            - The setting was deleted or never created
            - Typo in the setting key name
            
            **Solution:**
            - Use `GET /api/settings` to see all available settings
            - Check the setting key spelling
            - Verify the setting exists in your configuration
            
            **Example Response:**
            ```json
            {
              "detail": "Setting 'INVALID_KEY' not found"
            }
            ```
            """,
            "content": {
                "application/json": {
                    "example": {
                        "detail": "Setting 'INVALID_KEY' not found"
                    }
                }
            }
        },
        500: {
            "description": """
            **Internal server error**
            
            This error occurs when:
            - Database connection issues
            - Configuration sync failures
            - Unexpected system errors
            
            **Solution:**
            - Check server logs for detailed error information
            - Verify database is running and accessible
            - Contact system administrator if issue persists
            
            **Example Response:**
            ```json
            {
              "detail": "Failed to retrieve settings"
            }
            ```
            """,
            "content": {
                "application/json": {
                    "example": {
                        "detail": "Failed to retrieve settings"
                    }
                }
            }
        }
    }
)


# =====================================================
# Pydantic Models
# =====================================================

class SettingResponse(BaseModel):
    """Response model for a setting"""
    id: int
    key: str
    value: Optional[str]
    value_type: str
    category: str
    description: Optional[str]
    is_sensitive: bool
    is_readonly: bool
    created_at: str
    updated_at: str

    class Config:
        from_attributes = True


class SettingUpdateRequest(BaseModel):
    """
    Request model for updating a setting.
    
    The value must match the setting's declared type:
    - int: Whole numbers (e.g., "50000")
    - float: Decimal numbers (e.g., "0.4")
    - bool: true, false, yes, no, 1, 0, on, off
    - string: Any text value
    - json: Valid JSON string
    """
    value: Optional[str] = Field(
        None,
        description="New value for the setting. Must match the setting's type (int, float, bool, string, json).",
        example="75000"
    )
    change_reason: Optional[str] = Field(
        None,
        description="Optional reason for the change. Recommended for audit trail and troubleshooting.",
        example="Performance optimization - increasing cache size"
    )
    
    class Config:
        json_schema_extra = {
            "example": {
                "value": "75000",
                "change_reason": "Performance optimization"
            }
        }


class SettingsAuditLogResponse(BaseModel):
    """Response model for settings audit log entry"""
    id: int
    setting_key: str
    old_value: Optional[str]
    new_value: Optional[str]
    value_type: str
    changed_by_user_id: Optional[int]
    changed_by_username: Optional[str]
    change_reason: Optional[str]
    ip_address: Optional[str]
    created_at: str

    class Config:
        from_attributes = True


# =====================================================
# Helper Functions
# =====================================================

def parse_setting_value(value: str, value_type: str) -> Any:
    """Parse setting value based on its type"""
    if value is None:
        return None
    
    try:
        if value_type == "int":
            return int(value)
        elif value_type == "float":
            return float(value)
        elif value_type == "bool":
            return value.lower() in ("true", "1", "yes", "on")
        elif value_type == "json":
            return json.loads(value)
        else:
            return value
    except (ValueError, json.JSONDecodeError) as e:
        logger.warning(f"Failed to parse setting value '{value}' as {value_type}: {e}")
        return value


def format_setting_value(value: Any, value_type: str) -> str:
    """Format setting value to string for storage"""
    if value is None:
        return ""
    
    if value_type == "json":
        return json.dumps(value)
    elif value_type == "bool":
        return "true" if value else "false"
    else:
        return str(value)


async def sync_settings_from_config(db: AsyncSession):
    """Sync settings from config.py to database"""
    # Get all config attributes
    config_dict = {}
    categories = {
        "server": ["HOST", "PORT", "WORKERS", "USE_GPU", "ENVIRONMENT", "DEBUG", "LOG_DIR", "LOG_LEVEL", "LOGS_LIFE_TIME_HOURS", "BACKGROUND_TASK_NOTIFICATIONS_ENABLED", "BACKGROUND_TASK_NOTIFICATION_LEAD_TIME_SECONDS"],
        "security": ["JWT_SECRET_KEY", "JWT_ALGORITHM", "ACCESS_TOKEN_EXPIRE_MINUTES"],
        "database": ["DATABASE_URL", "DB_HOST", "DB_PORT", "POSTGRES_DB", "POSTGRES_USER", "POSTGRES_PASSWORD", 
                    "DB_POOL_SIZE", "DB_MAX_OVERFLOW", "DB_POOL_RECYCLE", "DB_POOL_PRE_PING"],
        "cache": ["REDIS_URL", "REDIS_MAX_CONNECTIONS", "REDIS_POOL_SIZE", "CACHE_TTL", "CACHE_LOCAL_SIZE", 
                 "CACHE_VERSION", "CACHE_WARNING_ENABLED", "CACHE_WARNING_INTERVAL"],
        "models": ["DETECTION_MODEL", "RECOGNITION_MODEL", "SIMILARITY_THRESHOLD", "UNKNOWN_SIMILARITY_THRESHOLD",
                  "CONFIDENCE_THRESHOLD", "FACES_DIR", "DB_PATH"],
        "processing": ["MAX_QUEUE_SIZE", "QUEUE_WORKERS", "BATCH_SIZE", "MAX_CONCURRENT_REQUESTS",
                      "GPU_BATCH_SIZE", "CPU_BATCH_SIZE", "PIPELINE_BATCH_SIZE",
                      "INFERENCE_WORKERS", "MAX_CONCURRENT_INFERENCE", "MAX_CONCURRENT_INFERENCE_PER_PIPELINE",
                      "WEBHOOK_MAX_BODY_MB", "WEBHOOK_DEDUP_TTL_SECONDS"],
        "storage": ["STORAGE_DIR", "SAVE_IMAGES", "MAX_STORAGE_GB", "MAX_PHOTOS_PER_PERSON", 
                   "SAVE_UNKNOWN_FACES", "MAX_FILE_SIZE", "SAVE_WEBHOOK_IMAGES", "WEBHOOK_IMAGES_DIR",
                   "SAVE_CROPPED_IMAGES", "CROPPED_IMAGES_DIR"],
        "tracking": ["FACE_TRACKING_ENABLED", "FACE_TRACKING_WINDOW_SECONDS", "FACE_TRACKING_MAX_ENTRIES",
                     "FACE_TRACKING_SIMILARITY_THRESHOLD", "SKIP_UNKNOWN_FACES", "SHOW_UNKNOWN_FACES_ON_DASHBOARD",
                     "FACE_TRACKING_MAX_MEMORY_MB", "FACE_TRACKING_CLEANUP_INTERVAL",
                     "DASHBOARD_FACE_DISPLAY_HOURS", "UNKNOWN_FACE_DISPLAY_HOURS",
                     "ALERT_NOTIFICATION_WINDOW_HOURS"],
        "identity": ["IDENTITY_ENRICH_MIN_SIMILARITY", "IDENTITY_ENRICH_MIN_QUALITY", "IDENTITY_MAX_EMBEDDINGS",
                    "CLUSTER_INTERVAL_HOURS", "CLUSTER_STARTUP_DELAY_HOURS", "CLUSTER_MIN_SIZE", "CLUSTER_EPS", "CLUSTER_MIN_SAMPLES",
                    "SNAPSHOT_RETENTION_DAYS", "EMBEDDING_RETENTION_MONTHS", "INACTIVE_THRESHOLD_DAYS",
                    "IDENTITY_CLEANUP_INTERVAL_HOURS", "MAX_EMBEDDINGS_PER_IDENTITY", "IDENTITY_EMBEDDING_SIZE",
                    "IDENTITY_INDEX_DB_PATH", "IDENTITY_INDEX_AUTO_SAVE_INTERVAL", 
                    # Vector Search Backend Configuration
                    "VECTOR_BACKEND", "PGVECTOR_INDEX_TYPE", "PGVECTOR_HNSW_M", "PGVECTOR_HNSW_EF_CONSTRUCTION",
                    "PGVECTOR_IVFFLAT_LISTS", "PGVECTOR_IVFFLAT_PROBES",
                    # FAISS Configuration
                    "REPAIR_FAISS_ON_STARTUP",
                    "REPAIR_FAISS_INTERVAL_HOURS", "FAISS_LAZY_MARKING_THRESHOLD", "KNOWN_INDEX_TYPE",
                    "UNKNOWN_INDEX_TYPE", "KNOWN_INDEX_NLIST", "KNOWN_INDEX_NPROBE", "KNOWN_INDEX_HNSW_M",
                    "KNOWN_INDEX_HNSW_EF_CONSTRUCTION", "KNOWN_INDEX_HNSW_EF_SEARCH", "KNOWN_INDEX_PQ_M",
                    "KNOWN_INDEX_PQ_BITS", "PIPELINE_AWARE_CLUSTERING_ENABLED", "PIPELINE_SIMILARITY_WEIGHT",
                    "EMBEDDING_SIMILARITY_WEIGHT", "CROSS_PIPELINE_SIMILARITY_THRESHOLD", "SIMILARITY_MODEL_PATH",
                    "SIMILARITY_MODEL_MIN_SAMPLES", "SIMILARITY_MODEL_AUTO_TRAIN"],
        "retention": ["DATA_RETENTION_DAYS", "CLEANUP_INTERVAL_HOURS", "AUDIT_LOG_RETENTION_DAYS",
                     "BATCH_WRITE_SIZE", "BATCH_WRITE_INTERVAL", "BATCH_WRITE_MAX_WAIT"],
        "ollama": ["OLLAMA_BASE_URL", "OLLAMA_MODEL", "OLLAMA_SQL_MODEL", "OLLAMA_TEMPERATURE", "OLLAMA_TIMEOUT"],
        "sql_agent": ["CHROMADB_PATH", "CHROMA_COLLECTION_NAME", "RAG_TOP_K", "RAG_SIMILARITY_THRESHOLD",
                     "SQL_AGENT_MAX_CONCURRENT", "SQL_AGENT_TOTAL_TIMEOUT"],
        "advanced_search": [
            "SEARCH_MIN_QUALITY_THRESHOLD", "SEARCH_QUALITY_WARNING_THRESHOLD",
            "CONFIDENCE_VERY_HIGH_MIN", "CONFIDENCE_HIGH_MIN", "CONFIDENCE_MEDIUM_MIN", "CONFIDENCE_LOW_MIN",
            "BATCH_SEARCH_MAX_IMAGES", "BATCH_SEARCH_TIMEOUT_SECONDS",
            "SEARCH_HISTORY_RETENTION_DAYS", "SEARCH_HISTORY_MAX_PER_USER",
            "LIVE_ALERT_DEFAULT_COOLDOWN_MINUTES", "LIVE_ALERT_MAX_PER_USER", "LIVE_ALERT_MAX_PER_IDENTITY",
            "RELATED_IDENTITY_MIN_CO_APPEARANCES", "RELATED_IDENTITY_TIME_WINDOW_MINUTES",
            "FACE_QUALITY_ENABLED", "WATCHLIST_ENABLED", "LIVE_ALERTS_ENABLED",
            "RELATED_IDENTITIES_ENABLED", "TEMPORAL_PATTERNS_ENABLED",
            "CROSS_CAMERA_TRACKING_ENABLED", "BATCH_SEARCH_ENABLED",
            "EXPORT_RESULTS_ENABLED", "NEGATIVE_SEARCH_ENABLED",
            "FACE_QUALITY_THRESHOLD_BLUR", "FACE_QUALITY_THRESHOLD_SIZE",
            "FACE_QUALITY_THRESHOLD_ANGLE", "FACE_QUALITY_THRESHOLD_LIGHTING"
        ],
    }
    
    # Determine value types
    def get_value_type(key: str, value: Any) -> str:
        if isinstance(value, bool):
            return "bool"
        elif isinstance(value, int):
            return "int"
        elif isinstance(value, float):
            return "float"
        elif isinstance(value, (list, dict)):
            return "json"
        else:
            return "string"
    
    # Determine if sensitive
    sensitive_keys = ["JWT_SECRET_KEY", "POSTGRES_PASSWORD", "DATABASE_URL"]
    
    # Determine if readonly
    readonly_keys = ["DATABASE_URL", "DB_HOST", "DB_PORT", "POSTGRES_DB", "POSTGRES_USER", "POSTGRES_PASSWORD"]
    
    # Build settings from config
    for category, keys in categories.items():
        for key in keys:
            if hasattr(config_settings, key):
                value = getattr(config_settings, key)
                value_str = format_setting_value(value, get_value_type(key, value))
                
                # Find or create setting
                result = await db.execute(select(Setting).where(Setting.key == key))
                setting = result.scalar_one_or_none()
                
                # Get custom description for specific settings
                custom_descriptions = {
                    "LOGS_LIFE_TIME_HOURS": "Log retention period in hours. Log entries older than this will be automatically deleted. Default: 48 hours.",
                    "LOG_DIR": "Directory where log files are stored",
                    "LOG_LEVEL": "Logging level: DEBUG, INFO, WARNING, ERROR, CRITICAL",
                    "BACKGROUND_TASK_NOTIFICATIONS_ENABLED": "Enable real-time WebSocket notifications for background tasks (log cleanup, data retention, clustering, etc.). Admins receive alerts 1 minute before tasks start. Default: true",
                    "BACKGROUND_TASK_NOTIFICATION_LEAD_TIME_SECONDS": "Seconds before background task starts to send notification. Default: 60 (1 minute). Increase for longer warning, decrease for shorter.",
                    "REPAIR_FAISS_ON_STARTUP": "Enable/disable FAISS index repair on application startup. When enabled, orphaned entries are cleaned up automatically. Disable for faster startup on large datasets. Default: true",
                    "REPAIR_FAISS_INTERVAL_HOURS": "Background repair interval in hours. FAISS indexes are automatically repaired in the background to remove orphaned entries. Default: 24 hours. Set to 0 to disable background repair.",
                    "FAISS_LAZY_MARKING_THRESHOLD": "Threshold for FAISS lazy marking approach. If mismatch count is below this threshold, orphaned vectors are lazy-marked (skipped during search) instead of rebuilding the entire index. Default: 1 (for demo). For production with large datasets, use 100 or higher.",
                    "CLUSTER_STARTUP_DELAY_HOURS": "Hours to wait after system startup before running the first clustering job. This allows the system to collect some data before generating merge suggestions. Default: 0 (immediate). Set to 1.0 for delayed startup.",
                    "CLUSTER_INTERVAL_HOURS": "Hours between clustering runs. The clustering job finds duplicate unknown identities and generates merge suggestions. Default: 24 hours. Set to 0 to disable automatic clustering.",
                    "PIPELINE_AWARE_CLUSTERING_ENABLED": "Enable pipeline-aware ML clustering for merge suggestions. When enabled, suggestions are filtered by user's accessible pipelines and use ML-based similarity scoring. Default: true",
                    "PIPELINE_SIMILARITY_WEIGHT": "Weight for pipeline overlap in similarity calculation (0.0-1.0). Higher values give more importance to pipeline overlap. Default: 0.3",
                    "EMBEDDING_SIMILARITY_WEIGHT": "Weight for embedding similarity in calculation (0.0-1.0). Higher values give more importance to face embedding similarity. Default: 0.7",
                    "CROSS_PIPELINE_SIMILARITY_THRESHOLD": "Minimum similarity threshold for cross-pipeline matches (identities from different cameras). Stricter threshold to maintain accuracy. Default: 0.50",
                    "SIMILARITY_MODEL_PATH": "Path to save/load the trained ML similarity model. The model learns from user feedback (approved/rejected merge suggestions) to improve accuracy. Default: models/similarity_model.pkl",
                    "SIMILARITY_MODEL_MIN_SAMPLES": "Minimum training samples required before the ML model can be trained. Collect samples by approving/rejecting merge suggestions. Default: 50",
                    "SIMILARITY_MODEL_AUTO_TRAIN": "Automatically train the similarity model when enough samples are collected. When enabled, model trains automatically after reaching minimum samples. Default: true",
                    "SHOW_UNKNOWN_FACES_ON_DASHBOARD": "If True, unknown faces will appear on the main dashboard. If False (default), unknown faces are only visible in the Unknown Faces Center page.",
                    "DASHBOARD_FACE_DISPLAY_HOURS": "How many hours of face detections to show on the dashboard. Faces older than this are hidden but still stored in the database. Default: 3 hours. Increase for longer visibility, decrease for faster dashboard loading.",
                    "UNKNOWN_FACE_DISPLAY_HOURS": "How many hours unknown faces stay visible on the Unknown Faces page (display-only — data stays stored until retention deletes it). Default: 24 hours. Set to 0 to always show all. The page's Show-all toggle reveals older ones on demand.",
                    "ALERT_NOTIFICATION_WINDOW_HOURS": "Minimum time (in hours) between alert popups for the same person on the same camera. Default: 1 hour. Set to 0 to alert on every detection (not recommended - very noisy). Set higher to reduce alert frequency.",
                    "SAVE_WEBHOOK_IMAGES": "Save all images received via webhook for debugging purposes. When enabled, images are saved to WEBHOOK_IMAGES_DIR. Default: true",
                    "WEBHOOK_IMAGES_DIR": "Directory to save webhook images for debugging. Default: ./debug/webhook_images",
                    "SAVE_CROPPED_IMAGES": "Save all cropped person images for debugging. When enabled, cropped images are saved to CROPPED_IMAGES_DIR. Default: true",
                    "CROPPED_IMAGES_DIR": "Directory to save cropped person images for debugging. Default: ./debug/cropped",
                    # pgvector Configuration
                    "VECTOR_BACKEND": "Vector search backend: 'faiss' (default, fast in-memory search) or 'pgvector' (PostgreSQL-based, simpler architecture, ACID compliant). Changing this requires restart. Default: faiss",
                    "PGVECTOR_INDEX_TYPE": "pgvector index type: 'hnsw' (fast, recommended) or 'ivfflat' (memory efficient). Only used when VECTOR_BACKEND=pgvector. Default: hnsw",
                    "PGVECTOR_HNSW_M": "pgvector HNSW M parameter (connections per node, 16-64). Higher = better accuracy, more memory. Only used when PGVECTOR_INDEX_TYPE=hnsw. Default: 16",
                    "PGVECTOR_HNSW_EF_CONSTRUCTION": "pgvector HNSW efConstruction (build-time search width, 64-200). Higher = better index quality, slower build. Only used when PGVECTOR_INDEX_TYPE=hnsw. Default: 64",
                    "PGVECTOR_IVFFLAT_LISTS": "pgvector IVFFlat lists (number of clusters). Use sqrt(N) where N is total vectors. Only used when PGVECTOR_INDEX_TYPE=ivfflat. Default: 100",
                    "PGVECTOR_IVFFLAT_PROBES": "pgvector IVFFlat probes (clusters to search, 1-lists). Higher = better accuracy, slower search. Only used when PGVECTOR_INDEX_TYPE=ivfflat. Default: 10",
                    # FAISS Configuration
                    "KNOWN_INDEX_TYPE": "FAISS index type for KNOWN identities. Options: flat (exact search, best accuracy), ivf (fast, good for 1M+), hnsw (fastest, more memory), ivfpq (smallest, good for 10M+). Only used when VECTOR_BACKEND=faiss. Default: flat",
                    "UNKNOWN_INDEX_TYPE": "FAISS index type for UNKNOWN identities. Recommended: flat (exact search, best for small dynamic datasets). Only used when VECTOR_BACKEND=faiss. Default: flat",
                    "KNOWN_INDEX_NLIST": "Number of clusters for FAISS IVF index (sqrt(N) recommended, e.g., 1000 for 1M vectors). Only used when VECTOR_BACKEND=faiss and KNOWN_INDEX_TYPE=ivf. Default: 1000",
                    "KNOWN_INDEX_NPROBE": "Number of clusters to search in FAISS IVF (10-50, higher = better accuracy, slower). Only used when VECTOR_BACKEND=faiss and KNOWN_INDEX_TYPE=ivf. Default: 20",
                    "KNOWN_INDEX_HNSW_M": "FAISS HNSW M parameter (16-64, higher = better accuracy, more memory). Only used when VECTOR_BACKEND=faiss and KNOWN_INDEX_TYPE=hnsw. Default: 32",
                    "KNOWN_INDEX_HNSW_EF_CONSTRUCTION": "FAISS HNSW efConstruction parameter (200-400). Only used when VECTOR_BACKEND=faiss and KNOWN_INDEX_TYPE=hnsw. Default: 200",
                    "KNOWN_INDEX_HNSW_EF_SEARCH": "FAISS HNSW efSearch parameter (16-128). Only used when VECTOR_BACKEND=faiss and KNOWN_INDEX_TYPE=hnsw. Default: 64",
                    "KNOWN_INDEX_PQ_M": "FAISS PQ m parameter (8, 16, 32, 64). Only used when VECTOR_BACKEND=faiss and KNOWN_INDEX_TYPE=ivfpq. Default: 64",
                    "KNOWN_INDEX_PQ_BITS": "FAISS PQ bits per subquantizer (8 is standard). Only used when VECTOR_BACKEND=faiss and KNOWN_INDEX_TYPE=ivfpq. Default: 8",
                    # Advanced Search Settings
                    "SEARCH_MIN_QUALITY_THRESHOLD": "Minimum quality score (0-1) to attempt search. Faces below this are skipped. Default: 0.3. Lower values allow more faces to be searched but may reduce accuracy.",
                    "SEARCH_QUALITY_WARNING_THRESHOLD": "Quality threshold (0-1) below which a warning is shown. Default: 0.6. Faces below this may have reduced search accuracy.",
                    "CONFIDENCE_VERY_HIGH_MIN": "Minimum similarity for 'Very High' confidence band. Default: 0.90. Results above this are almost certain matches.",
                    "CONFIDENCE_HIGH_MIN": "Minimum similarity for 'High' confidence band. Default: 0.75. Results in this range are strong matches.",
                    "CONFIDENCE_MEDIUM_MIN": "Minimum similarity for 'Medium' confidence band. Default: 0.60. Results in this range are possible matches.",
                    "CONFIDENCE_LOW_MIN": "Minimum similarity for 'Low' confidence band. Default: 0.40. Results in this range are weak matches.",
                    "BATCH_SEARCH_MAX_IMAGES": "Maximum number of images allowed per batch search. Default: 20. Increase for processing more images at once.",
                    "BATCH_SEARCH_TIMEOUT_SECONDS": "Timeout in seconds for batch search operations. Default: 300 (5 minutes). Increase for large batch operations.",
                    "SEARCH_HISTORY_RETENTION_DAYS": "Days to retain search history records. Default: 90. Older records are automatically deleted.",
                    "SEARCH_HISTORY_MAX_PER_USER": "Maximum search history records per user. Default: 1000. Oldest records are deleted when limit is reached.",
                    "LIVE_ALERT_DEFAULT_COOLDOWN_MINUTES": "Default cooldown period (minutes) between live alert triggers. Default: 30. Prevents alert spam for the same person.",
                    "LIVE_ALERT_MAX_PER_USER": "Maximum number of active live alerts per user. Default: 50. Prevents excessive alert creation.",
                    "LIVE_ALERT_MAX_PER_IDENTITY": "Maximum number of active live alerts that can be created for the same identity. Default: 5, Max: 10. Prevents alert spam for a single person.",
                    "RELATED_IDENTITY_MIN_CO_APPEARANCES": "Minimum co-appearances required to establish related identity relationship. Default: 3. Lower values find more relationships but may include false positives.",
                    "RELATED_IDENTITY_TIME_WINDOW_MINUTES": "Time window (minutes) for considering identities as co-appearing. Default: 30. Identities detected within this window are considered together.",
                    "FACE_QUALITY_ENABLED": "Enable face quality scoring. Default: True. When enabled, faces are assessed for blur, lighting, angle, and size before search.",
                    "WATCHLIST_ENABLED": "Enable watchlist functionality. Default: True. When enabled, you can create watchlists (VIP, Threat, POI) and get alerts.",
                    "LIVE_ALERTS_ENABLED": "Enable live search alerts. Default: True. When enabled, you can set up alerts for when previously searched faces are detected again.",
                    "RELATED_IDENTITIES_ENABLED": "Enable related identities analysis. Default: True. When enabled, the system identifies people who frequently appear together.",
                    "TEMPORAL_PATTERNS_ENABLED": "Enable temporal pattern analysis. Default: True. When enabled, the system analyzes typical appearance times and locations.",
                    "CROSS_CAMERA_TRACKING_ENABLED": "Enable cross-camera tracking. Default: True. When enabled, you can visualize an individual's movement across different cameras.",
                    "BATCH_SEARCH_ENABLED": "Enable batch search functionality. Default: True. When enabled, you can process multiple images in a single request.",
                    "EXPORT_RESULTS_ENABLED": "Enable search results export (CSV, PDF, JSON). Default: True. When enabled, you can export search results and history.",
                    "NEGATIVE_SEARCH_ENABLED": "Enable negative search (exclude specific identities). Default: True. When enabled, you can exclude specific people from search results.",
                    "FACE_QUALITY_THRESHOLD_BLUR": "Blur threshold for face quality assessment. Default: 0.5. Higher values require sharper images.",
                    "FACE_QUALITY_THRESHOLD_SIZE": "Minimum face size (pixels) for quality assessment. Default: 64. Smaller faces are marked as low quality.",
                    "FACE_QUALITY_THRESHOLD_ANGLE": "Maximum face angle (degrees) for quality assessment. Default: 30.0. Faces at greater angles are marked as low quality.",
                    "FACE_QUALITY_THRESHOLD_LIGHTING": "Lighting threshold for face quality assessment. Default: 0.4. Higher values require better lighting.",
                }
                
                if not setting:
                    setting = Setting(
                        key=key,
                        value=value_str,
                        value_type=get_value_type(key, value),
                        category=category,
                        description=custom_descriptions.get(key, f"{key} configuration setting"),
                        is_sensitive=key in sensitive_keys,
                        is_readonly=key in readonly_keys
                    )
                    db.add(setting)
                else:
                    # SEED-ONLY: NEVER overwrite the stored value. The previous
                    # code pushed config -> DB here on every settings-page load,
                    # which silently REVERTED every admin edit milliseconds after
                    # it was saved — the root cause of "settings don't apply".
                    # Only descriptive metadata is refreshed.
                    if key in custom_descriptions and setting.description != custom_descriptions[key]:
                        setting.description = custom_descriptions[key]
                    if setting.is_sensitive != (key in sensitive_keys):
                        setting.is_sensitive = key in sensitive_keys
                    if setting.is_readonly != (key in readonly_keys):
                        setting.is_readonly = key in readonly_keys

    await db.commit()


# =====================================================
# API Endpoints
# =====================================================

@router.get(
    "",
    summary="Get All Settings",
    description="""
    Retrieve all system configuration settings with categories and formatted data.
    
    **Features:**
    - Returns all settings grouped by category (100+ settings available)
    - Automatically syncs settings from config.py to database on each request
    - Hides sensitive values (passwords, keys) for security
    - Provides formatted display values and edit permissions
    - Includes custom descriptions for each setting
    
    **Query Parameters:**
    - `category` (optional): Filter settings by category (e.g., "database", "security", "models", "identity", "tracking", "storage")
    
    **Available Categories:**
    - `server`: Server configuration (HOST, PORT, WORKERS, DEBUG, LOG_LEVEL, etc.)
    - `security`: Authentication and security (JWT_SECRET_KEY, etc.)
    - `database`: PostgreSQL settings (DB_POOL_SIZE, DATABASE_URL, etc.)
    - `cache`: Redis cache settings (REDIS_URL, CACHE_TTL, etc.)
    - `models`: AI model configuration (SIMILARITY_THRESHOLD, DETECTION_MODEL, etc.)
    - `processing`: Queue and batch processing (QUEUE_WORKERS, BATCH_SIZE, etc.)
    - `storage`: Image storage settings (STORAGE_DIR, SAVE_IMAGES, SAVE_WEBHOOK_IMAGES, SAVE_CROPPED_IMAGES, etc.)
    - `tracking`: Face tracking optimization (SHOW_UNKNOWN_FACES_ON_DASHBOARD, FACE_TRACKING_ENABLED, etc.)
    - `identity`: Identity management and FAISS (KNOWN_INDEX_TYPE, REPAIR_FAISS_ON_STARTUP, FAISS_LAZY_MARKING_THRESHOLD, etc.)
    - `retention`: Data cleanup settings (DATA_RETENTION_DAYS, etc.)
    - `ollama`: Ollama configuration (for SQL agent)
    - `sql_agent`: SQL agent settings (ChromaDB, etc.)
    - `advanced_search`: Advanced search configuration (quality thresholds, confidence bands, batch search, watchlists, live alerts, etc.)
    
    **New Settings Available:**
    - `SHOW_UNKNOWN_FACES_ON_DASHBOARD` (tracking): Show unknown faces on main dashboard
    - `SAVE_WEBHOOK_IMAGES` (storage): Save webhook images for debugging
    - `SAVE_CROPPED_IMAGES` (storage): Save cropped images for debugging
    - `KNOWN_INDEX_TYPE`, `UNKNOWN_INDEX_TYPE` (identity): FAISS index types (flat, ivf, hnsw, ivfpq)
    - `REPAIR_FAISS_ON_STARTUP`, `REPAIR_FAISS_INTERVAL_HOURS`, `FAISS_LAZY_MARKING_THRESHOLD` (identity): FAISS repair settings
    - `SEARCH_MIN_QUALITY_THRESHOLD`, `SEARCH_QUALITY_WARNING_THRESHOLD` (advanced_search): Quality thresholds for search
    - `CONFIDENCE_VERY_HIGH_MIN`, `CONFIDENCE_HIGH_MIN`, `CONFIDENCE_MEDIUM_MIN`, `CONFIDENCE_LOW_MIN` (advanced_search): Confidence band thresholds
    - `BATCH_SEARCH_MAX_IMAGES`, `BATCH_SEARCH_TIMEOUT_SECONDS` (advanced_search): Batch search limits
    - `SEARCH_HISTORY_RETENTION_DAYS`, `SEARCH_HISTORY_MAX_PER_USER` (advanced_search): Search history settings
    - `LIVE_ALERT_DEFAULT_COOLDOWN_MINUTES`, `LIVE_ALERT_MAX_PER_USER` (advanced_search): Live alert settings
    - `RELATED_IDENTITY_MIN_CO_APPEARANCES`, `RELATED_IDENTITY_TIME_WINDOW_MINUTES` (advanced_search): Related identities settings
    - `FACE_QUALITY_ENABLED`, `WATCHLIST_ENABLED`, `LIVE_ALERTS_ENABLED`, etc. (advanced_search): Feature flags
    - `FACE_QUALITY_THRESHOLD_BLUR`, `FACE_QUALITY_THRESHOLD_SIZE`, etc. (advanced_search): Quality assessment thresholds
    - `PIPELINE_AWARE_CLUSTERING_ENABLED`, `PIPELINE_SIMILARITY_WEIGHT`, `EMBEDDING_SIMILARITY_WEIGHT`, `CROSS_PIPELINE_SIMILARITY_THRESHOLD` (identity): Pipeline-aware ML clustering settings for merge suggestions
    - `SIMILARITY_MODEL_PATH`, `SIMILARITY_MODEL_MIN_SAMPLES`, `SIMILARITY_MODEL_AUTO_TRAIN` (identity): ML similarity model training settings (learns from user feedback to improve accuracy)
    
    **Response:**
    - `categories`: List of all available categories
    - `settings_by_category`: Settings grouped by category (object with category keys)
    - `all_settings`: Flat list of all settings (for "all" category)
    - `total_count`: Total number of settings
    
    **Authentication:** Admin role required
    
    **See Also:**
    - PUT /api/settings/{setting_key} - Update a setting
    - GET /api/settings/audit/log - View change history
    - Documentation: `36_CONFIGURATION_GUIDE.md` for complete configuration guide
    """,
    response_description="Settings data with categories and formatted values",
    responses={
        200: {
            "description": "Settings retrieved successfully",
            "content": {
                "application/json": {
                    "example": {
                        "categories": ["database", "security", "models"],
                        "settings_by_category": {
                            "database": [
                                {
                                    "id": 1,
                                    "key": "DB_POOL_SIZE",
                                    "value": "50",
                                    "value_type": "int",
                                    "category": "database",
                                    "description": "Database connection pool size",
                                    "is_sensitive": False,
                                    "is_readonly": False,
                                    "display_value": "50",
                                    "can_edit": True
                                }
                            ]
                        },
                        "all_settings": [],
                        "total_count": 100
                    }
                }
            }
        },
        401: {"description": "Unauthorized - Invalid or missing token"},
        403: {"description": "Forbidden - Admin access required"}
    }
)
@router.get(
    "/",
    summary="Get All Settings",
    description="Same as GET /api/settings (handles trailing slash)"
)
async def get_all_settings(
    response: Response,
    category: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_role(["admin"]))
):
    """
    Get all system settings with typed metadata (stored vs effective value,
    source, apply mode, restart requirements).

    Seeds missing settings from config.py; NEVER overwrites stored values.
    """
    try:
        response.headers["Cache-Control"] = NO_STORE

        # Seed any missing settings (insert-only; admin edits are preserved)
        await sync_settings_from_config(db)

        query = select(Setting)
        if category and category != "all":
            query = query.where(Setting.category == category)
        query = query.order_by(Setting.category, Setting.key)

        result = await db.execute(query)
        settings = result.scalars().all()

        # Get all categories
        categories_result = await db.execute(
            select(Setting.category).distinct().order_by(Setting.category)
        )
        all_categories = [row[0] for row in categories_result.all()]

        # Group settings by category (single typed payload builder everywhere)
        settings_by_category = {}
        all_payloads = []
        for setting in settings:
            payload = _setting_payload(setting)
            settings_by_category.setdefault(setting.category, []).append(payload)
            all_payloads.append(payload)

        return {
            "categories": all_categories,
            "settings_by_category": settings_by_category,
            "all_settings": all_payloads,
            "total_count": len(settings),
            "filtered_category": category if category and category != "all" else None
        }
    except Exception as e:
        logger.error(f"Error getting settings: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to retrieve settings"
        )


def _get_value_hint(value_type: str) -> str:
    """Get hint text for value type"""
    hints = {
        "bool": "Enter: true, false, yes, no, 1, or 0",
        "boolean": "Enter: true, false, yes, no, 1, or 0",
        "int": "Enter a whole number",
        "integer": "Enter a whole number",
        "float": "Enter a decimal number",
        "json": "Enter valid JSON",
        "string": "Enter a text value"
    }
    return hints.get(value_type, "Enter a value")


# IMPORTANT: Specific routes must come BEFORE parameterized routes
# FastAPI matches routes in order, so /categories and /audit/log must come before /{setting_key}

@router.get(
    "/categories",
    response_model=List[str],
    summary="Get Setting Categories",
    description="""
    Retrieve all available setting categories.
    
    **Use Cases:**
    - Populate category filter dropdowns
    - Group settings by category in UI
    - Display category navigation
    
    **Note:** Categories are also included in the main `/api/settings` endpoint response.
    
    **Authentication:** Admin role required
    """,
    response_description="List of category names",
    responses={
        200: {
            "description": "Categories retrieved successfully",
            "content": {
                "application/json": {
                    "example": ["database", "security", "models", "processing", "storage"]
                }
            }
        },
        401: {"description": "Unauthorized - Invalid or missing token"},
        403: {"description": "Forbidden - Admin access required"}
    }
)
async def get_categories(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_role(["admin"]))
):
    """
    Get all setting categories.
    
    Returns a list of distinct category names used to organize settings.
    Categories are automatically synced from config.py.
    """
    try:
        # Sync settings first
        await sync_settings_from_config(db)
        
        result = await db.execute(
            select(Setting.category).distinct().order_by(Setting.category)
        )
        categories = [row[0] for row in result.all()]
        return categories
    except Exception as e:
        logger.error(f"Error getting categories: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to retrieve categories"
        )


@router.get(
    "/audit/log",
    summary="Get Settings Audit Log",
    description="""
    Retrieve the audit log of all setting changes.
    
    **Features:**
    - Complete history of setting modifications
    - Shows who changed what, when, and why
    - Includes IP address and user agent for security tracking
    - Supports filtering by setting key
    - Paginated results for large datasets
    
    **Query Parameters:**
    - `setting_key` (optional): Filter by specific setting key
    - `limit` (default: 100): Maximum number of entries to return
    - `offset` (default: 0): Number of entries to skip (for pagination)
    
    **Use Cases:**
    - Security auditing
    - Troubleshooting configuration issues
    - Compliance tracking
    - Change history review
    
    **Authentication:** Admin role required
    """,
    response_description="Audit log entries with formatted data",
    responses={
        200: {
            "description": "Audit log retrieved successfully",
            "content": {
                "application/json": {
                    "example": {
                        "logs": [
                            {
                                "id": 1,
                                "setting_key": "CACHE_LOCAL_SIZE",
                                "old_value": "50000",
                                "new_value": "75000",
                                "value_type": "int",
                                "changed_by_user_id": 1,
                                "changed_by_username": "admin",
                                "change_reason": "Performance optimization",
                                "ip_address": "192.168.1.100",
                                "created_at": "2025-01-15T14:30:25",
                                "created_at_formatted": "2025-01-15 14:30:25",
                                "time_ago": "2 hours ago"
                            }
                        ],
                        "total_count": 150,
                        "limit": 100,
                        "offset": 0,
                        "has_more": True
                    }
                }
            }
        },
        401: {"description": "Unauthorized - Invalid or missing token"},
        403: {"description": "Forbidden - Admin access required"}
    }
)
async def get_audit_log(
    response: Response,
    setting_key: Optional[str] = None,
    limit: int = 100,
    offset: int = 0,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_role(["admin"]))
):
    """
    Get settings audit log with formatted data.

    Returns a paginated list of all setting changes with details about
    who made the change, when, and why. Useful for security auditing
    and troubleshooting configuration issues.
    """
    try:
        response.headers["Cache-Control"] = NO_STORE
        query = select(SettingsAuditLog)
        
        if setting_key:
            query = query.where(SettingsAuditLog.setting_key == setting_key)
        
        # Get total count
        count_query = select(func.count(SettingsAuditLog.id))
        if setting_key:
            count_query = count_query.where(SettingsAuditLog.setting_key == setting_key)
        count_result = await db.execute(count_query)
        total_count = count_result.scalar() or 0
        
        query = query.order_by(SettingsAuditLog.created_at.desc()).limit(limit).offset(offset)
        
        result = await db.execute(query)
        logs = result.scalars().all()
        
        # Format logs with display-ready data
        formatted_logs = []
        for log in logs:
            # Format date for display
            formatted_date = None
            formatted_date_iso = None
            if log.created_at:
                formatted_date_iso = log.created_at.isoformat()
                # Format for display (e.g., "2024-01-15 14:30:25")
                formatted_date = log.created_at.strftime("%Y-%m-%d %H:%M:%S")
            
            log_dict = {
                "id": log.id,
                "setting_key": log.setting_key,
                "old_value": log.old_value or "(empty)",
                "new_value": log.new_value or "(empty)",
                "value_type": log.value_type,
                "changed_by_user_id": log.changed_by_user_id,
                "changed_by_username": log.changed_by_username or "System",
                "change_reason": log.change_reason or "-",
                "action": getattr(log, "action", None) or "value_saved",
                "ip_address": log.ip_address or "-",
                "created_at": formatted_date_iso,
                "created_at_formatted": formatted_date,
                # Add relative time
                "time_ago": _get_time_ago(log.created_at) if log.created_at else None,
            }
            formatted_logs.append(log_dict)
        
        return {
            "logs": formatted_logs,
            "total_count": total_count,
            "limit": limit,
            "offset": offset,
            "has_more": (offset + limit) < total_count
        }
    except Exception as e:
        logger.error(f"Error getting audit log: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to retrieve audit log"
        )


@router.get(
    "/{setting_key}",
    summary="Get Setting by Key",
    description="""
    Retrieve a specific setting by its key name.
    
    **Path Parameters:**
    - `setting_key`: The configuration key name (e.g., "CACHE_LOCAL_SIZE", "SIMILARITY_THRESHOLD")
    
    **Features:**
    - Returns detailed information about a single setting
    - Hides sensitive values automatically
    - Includes metadata (type, category, description, readonly status)
    
    **Note:** Reserved paths like "categories" and "audit" cannot be used as setting keys.
    
    **Authentication:** Admin role required
    """,
    response_description="Setting details",
    responses={
        200: {
            "description": "Setting retrieved successfully",
            "content": {
                "application/json": {
                    "example": {
                        "id": 1,
                        "key": "CACHE_LOCAL_SIZE",
                        "value": "50000",
                        "value_type": "int",
                        "category": "cache",
                        "description": "CACHE_LOCAL_SIZE configuration setting",
                        "is_sensitive": False,
                        "is_readonly": False,
                        "created_at": "2025-01-01T00:00:00",
                        "updated_at": "2025-01-15T14:30:25"
                    }
                }
            }
        },
        401: {"description": "Unauthorized - Invalid or missing token"},
        403: {"description": "Forbidden - Admin access required"},
        404: {"description": "Setting not found"}
    }
)
async def get_setting(
    setting_key: str,
    response: Response,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_role(["admin"]))
):
    """
    Get a specific setting by key with full typed metadata
    (stored/effective/default values, source, apply mode).
    """
    try:
        response.headers["Cache-Control"] = NO_STORE
        result = await db.execute(select(Setting).where(Setting.key == setting_key))
        setting = result.scalar_one_or_none()

        if not setting:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Setting '{setting_key}' not found"
            )

        return _setting_payload(setting)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting setting: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to retrieve setting"
        )


@router.put(
    "/{setting_key}",
    summary="Update Setting",
    description="""
    Update a configuration setting value.
    
    **Path Parameters:**
    - `setting_key`: The configuration key name to update (e.g., "SHOW_UNKNOWN_FACES_ON_DASHBOARD", "SIMILARITY_THRESHOLD")
    
    **Request Body:**
    - `value` (required): New value for the setting (must match the setting's type)
    - `change_reason` (optional): Reason for the change (recommended for audit trail)
    
    **Features:**
    - Validates value type (int, float, bool, string, json)
    - Prevents modification of readonly settings (database credentials, etc.)
    - Automatically creates audit log entry with who/when/why
    - Records IP address and user agent for security tracking
    - Syncs with config.py defaults automatically
    
    **Validation:**
    - Value must match the setting's declared type
    - Readonly settings cannot be modified
    - Boolean values accept: true, false, yes, no, 1, 0, on, off
    
    **Important New Settings:**
    - `SHOW_UNKNOWN_FACES_ON_DASHBOARD` (bool): Show unknown faces on main dashboard (default: false)
    - `SAVE_WEBHOOK_IMAGES` (bool): Save webhook images for debugging (default: true)
    - `SAVE_CROPPED_IMAGES` (bool): Save cropped images for debugging (default: true)
    - `KNOWN_INDEX_TYPE` (string): FAISS index type - flat, ivf, hnsw, ivfpq (default: flat)
    - `FAISS_LAZY_MARKING_THRESHOLD` (int): Lazy marking threshold (default: 1, production: 100+)
    - `REPAIR_FAISS_ON_STARTUP` (bool): Enable startup repair (default: true)
    - `REPAIR_FAISS_INTERVAL_HOURS` (int): Background repair interval (default: 24)
    
    **Example Requests:**
    
    Show unknown faces on dashboard:
    ```json
    {
      "value": "true",
      "change_reason": "Want to see all faces on main dashboard"
    }
    ```
    
    Change similarity threshold:
    ```json
    {
      "value": "0.3",
      "change_reason": "Lowering threshold for testing"
    }
    ```
    
    Change FAISS index type:
    ```json
    {
      "value": "ivf",
      "change_reason": "Optimizing for 1M+ known faces"
    }
    ```
    
    **Authentication:** Admin role required
    
    **See Also:**
    - GET /api/settings - List all available settings
    - GET /api/settings/audit/log - View change history
    - Documentation: `36_CONFIGURATION_GUIDE.md` for complete configuration guide
    """,
    response_description="Updated setting with success message",
    responses={
        200: {
            "description": "Setting updated successfully",
            "content": {
                "application/json": {
                    "example": {
                        "success": True,
                        "message": "Setting 'CACHE_LOCAL_SIZE' updated successfully",
                        "setting": {
                            "id": 1,
                            "key": "CACHE_LOCAL_SIZE",
                            "value": "75000",
                            "value_type": "int",
                            "category": "cache",
                            "display_value": "75000",
                            "can_edit": True
                        },
                        "audit_log_id": 42
                    }
                }
            }
        },
        400: {"description": "Bad Request - Invalid value type or format"},
        401: {"description": "Unauthorized - Invalid or missing token"},
        403: {"description": "Forbidden - Admin access required or setting is readonly"},
        404: {"description": "Setting not found"}
    }
)
async def update_setting(
    setting_key: str,
    update_data: SettingUpdateRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_role(["admin"]))
):
    """
    Update a setting value with audit logging.
    
    Updates the value of a configuration setting and automatically creates
    an audit log entry tracking who made the change, when, and why.
    The value is validated against the setting's type before saving.
    """
    try:
        # Get setting
        result = await db.execute(select(Setting).where(Setting.key == setting_key))
        setting = result.scalar_one_or_none()
        
        if not setting:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Setting '{setting_key}' not found"
            )
        
        # Check if readonly
        if setting.is_readonly:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Setting '{setting_key}' is readonly and cannot be modified"
            )
        
        # TYPED validation against the runtime-settings registry: type + min/max
        # + enum + allow_empty. 0 and false are valid values (explicit checks).
        try:
            typed_value = runtime_settings.typed_parse(
                setting_key, update_data.value, setting.category
            )
        except SettingValidationError as ve:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=str(ve)
            )

        meta = runtime_settings.get_meta(setting_key, setting.category)

        # Store old value for audit
        old_value = setting.value

        # Persist the canonical string form (booleans lowercase, json compact)
        setting.value = runtime_settings.to_stored_string(typed_value)
        setting.updated_at = datetime.utcnow()

        # Apply to the running process when the apply mode allows it —
        # replaces the old hardcoded 2-key block with the registry-driven service.
        applied = False
        apply_error = None
        if meta.apply_mode in runtime_settings.DYNAMIC_APPLY_MODES:
            try:
                applied = runtime_settings.apply_to_runtime(
                    setting_key, typed_value, setting.category
                )
            except Exception as e:
                apply_error = str(e)
                logger.error(f"[SETTINGS] Runtime apply failed for {setting_key}: {e}")

        if applied:
            audit_action = "value_applied"
        elif apply_error:
            audit_action = "application_failed"
        else:
            audit_action = "value_saved"

        # Create audit log entry (never stores sensitive VALUES for sensitive keys)
        ip_address, user_agent = get_client_info(request)
        audit_log = SettingsAuditLog(
            setting_key=setting_key,
            old_value="***HIDDEN***" if setting.is_sensitive else old_value,
            new_value="***HIDDEN***" if setting.is_sensitive else setting.value,
            value_type=meta.value_type,
            changed_by_user_id=current_user.id,
            changed_by_username=current_user.username,
            change_reason=update_data.change_reason,
            ip_address=ip_address,
            user_agent=user_agent,
        )
        if hasattr(audit_log, "action"):
            audit_log.action = audit_action
        db.add(audit_log)

        await db.commit()
        await db.refresh(setting)

        logger.info(
            f"Setting '{setting_key}' updated by {current_user.username} "
            f"(action={audit_action}, apply_mode={meta.apply_mode})"
        )

        # Broadcast config change to all connected WebSocket clients
        # This allows frontend to update DASHBOARD_FACE_DISPLAY_MS immediately
        if setting_key in ["DASHBOARD_FACE_DISPLAY_HOURS", "ALERT_NOTIFICATION_WINDOW_HOURS"]:
            try:
                from backend.core.websocket_manager import ws_manager
                from config import settings as config_settings
                
                # Calculate updated config values
                if setting_key == "DASHBOARD_FACE_DISPLAY_HOURS":
                    display_hours = int(setting.value) if setting.value else 3
                    notification_window_hours = getattr(config_settings, 'ALERT_NOTIFICATION_WINDOW_HOURS', 1.0)
                else:  # ALERT_NOTIFICATION_WINDOW_HOURS
                    display_hours = getattr(config_settings, 'DASHBOARD_FACE_DISPLAY_HOURS', 3)
                    notification_window_hours = float(setting.value) if setting.value else 1.0
                
                # Build config response
                config_response = {
                    "success": True,
                    "config": {
                        "face_display_hours": display_hours,
                        "face_display_ms": int(display_hours * 60 * 60 * 1000),
                        "alert_notification_window_hours": notification_window_hours,
                        "alert_notification_window_ms": int(notification_window_hours * 60 * 60 * 1000),
                    }
                }
                if config_response and config_response.get("success"):
                    # Broadcast config change to all connected clients
                    await ws_manager.broadcast({
                        "type": "config_changed",
                        "setting_key": setting_key,
                        "new_value": setting.value,
                        "config": config_response.get("config", {})
                    }, pipeline_id=None, admin_only=False)
                    logger.info(f"[SETTINGS] 📡 Broadcasted config change for '{setting_key}' to all WebSocket clients")
            except Exception as e:
                logger.warning(f"[SETTINGS] ⚠️ Could not broadcast config change: {e}")
        
        # HONEST response: `applied` is true only when the running process now
        # uses the new value; otherwise the message states exactly what's needed.
        if applied:
            if meta.apply_mode == "next_job_run":
                message = f"'{setting_key}' saved and applied — takes effect at the next scheduled job run"
            elif meta.apply_mode == "next_request":
                message = f"'{setting_key}' saved and applied — effective from the next request"
            else:
                message = f"'{setting_key}' saved and applied immediately"
        elif apply_error:
            message = f"'{setting_key}' saved, but applying to the runtime FAILED: {apply_error}"
        elif meta.apply_mode == "index_rebuild":
            message = f"'{setting_key}' saved — requires a vector index rebuild to take effect"
        elif meta.apply_mode == "container_recreate":
            message = f"'{setting_key}' saved — requires container recreate (docker compose up -d) to take effect"
        else:
            message = f"'{setting_key}' saved — requires an API restart to take effect"

        return {
            "success": True,
            "saved": True,
            "applied": applied,
            "apply_mode": meta.apply_mode,
            "restart_required": (not applied) and meta.apply_mode not in runtime_settings.DYNAMIC_APPLY_MODES,
            "apply_error": apply_error,
            "message": message,
            "setting": _setting_payload(setting),
            "audit_log_id": audit_log.id
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error updating setting: {e}", exc_info=True)
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to update setting: {str(e)}"
        )


def _validate_setting_value(value: str, value_type: str) -> Optional[str]:
    """Validate setting value based on type. Returns error message if invalid, None if valid."""
    if not value:
        return None  # Empty values are allowed
    
    try:
        if value_type == "int":
            int(value)
        elif value_type == "float":
            float(value)
        elif value_type == "bool":
            value_lower = value.lower()
            if value_lower not in ("true", "false", "1", "0", "yes", "no", "on", "off"):
                return f"Invalid boolean value. Expected: true, false, yes, no, 1, or 0"
        elif value_type == "json":
            json.loads(value)
        # string type accepts anything
    except ValueError as e:
        return f"Invalid {value_type} value: {str(e)}"
    except json.JSONDecodeError as e:
        return f"Invalid JSON: {str(e)}"
    
    return None


def _get_time_ago(dt: datetime) -> str:
    """Get human-readable time ago string"""
    if not dt:
        return "Unknown"
    
    now = datetime.utcnow()
    diff = now - dt
    
    if diff.days > 365:
        years = diff.days // 365
        return f"{years} year{'s' if years > 1 else ''} ago"
    elif diff.days > 30:
        months = diff.days // 30
        return f"{months} month{'s' if months > 1 else ''} ago"
    elif diff.days > 0:
        return f"{diff.days} day{'s' if diff.days > 1 else ''} ago"
    elif diff.seconds > 3600:
        hours = diff.seconds // 3600
        return f"{hours} hour{'s' if hours > 1 else ''} ago"
    elif diff.seconds > 60:
        minutes = diff.seconds // 60
        return f"{minutes} minute{'s' if minutes > 1 else ''} ago"
    else:
        return "Just now"


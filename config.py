"""
Centralized Configuration Management
====================================
Single source of truth for all configuration variables.
Loads from .env file in the root directory.
"""

import json
import os
from typing import List, Optional
from pydantic_settings import BaseSettings
from pydantic import Field


def parse_origin_list(raw) -> List[str]:
    """Parse an origin configuration value into a clean list.

    Accepts a JSON array ('["https://a","https://b"]'), a comma-separated
    string, a single origin, or "*". Docker Compose passes the JSON form, which
    a plain comma-split turns into one malformed literal origin — so JSON is
    decoded first.
    """
    if raw is None:
        return []
    if isinstance(raw, (list, tuple, set)):
        items = list(raw)
    else:
        text = str(raw).strip()
        if not text:
            return []
        if text.startswith("[") and text.endswith("]"):
            try:
                decoded = json.loads(text)
                items = decoded if isinstance(decoded, list) else [decoded]
            except (ValueError, TypeError):
                items = [text]
        else:
            items = text.split(",")

    origins: List[str] = []
    for item in items:
        value = str(item).strip().strip('"').strip("'").rstrip("/")
        if value and value not in origins:
            origins.append(value)
    return origins


class Settings(BaseSettings):
    """
    Application settings loaded from environment variables.
    All configuration variables are defined here.
    """

    # =====================================================
    # Environment & Server Configuration
    # =====================================================
    ENVIRONMENT: str = Field(default="production", env="ENVIRONMENT")
    DEBUG: bool = Field(default=False, env="DEBUG")
    
    HOST: str = Field(default="0.0.0.0", env="HOST")
    PORT: int = Field(default=8000, env="PORT")
    WORKERS: int = Field(default=4, env="WORKERS")
    
    # GPU Configuration
    USE_GPU: bool = Field(default=False, env="USE_GPU")
    
    # Application Info
    APP_NAME: str = "Face Recognition Service"
    VERSION: str = "5.0.0"
    
    # Logging
    LOG_DIR: str = Field(default="/var/log/face-recognition", env="LOG_DIR")  # Docker path
    LOG_LEVEL: str = Field(default="INFO", env="LOG_LEVEL")
    LOGS_LIFE_TIME_HOURS: int = Field(default=48, env="LOGS_LIFE_TIME_HOURS", description="Log retention period in hours (default: 48 hours)")
    
    # Background Task Notifications
    BACKGROUND_TASK_NOTIFICATIONS_ENABLED: bool = Field(default=True, env="BACKGROUND_TASK_NOTIFICATIONS_ENABLED", description="Enable real-time notifications for background tasks (default: True)")
    BACKGROUND_TASK_NOTIFICATION_LEAD_TIME_SECONDS: int = Field(default=60, env="BACKGROUND_TASK_NOTIFICATION_LEAD_TIME_SECONDS", description="Seconds before task start to send notification (default: 60 = 1 minute)")

    # =====================================================
    # Security & Authentication
    # =====================================================
    JWT_SECRET_KEY: str = Field(
        default="your-secret-key-change-in-production",
        env="JWT_SECRET_KEY"
    )
    JWT_ALGORITHM: str = Field(default="HS256", env="JWT_ALGORITHM")
    ACCESS_TOKEN_EXPIRE_MINUTES: int = Field(default=1440, env="ACCESS_TOKEN_EXPIRE_MINUTES")  # 24 hours
    JWT_ISSUER: str = Field(default="face-recognition-service", env="JWT_ISSUER", description="JWT 'iss' claim — tokens from other issuers are rejected")
    JWT_AUDIENCE: str = Field(default="face-recognition-api", env="JWT_AUDIENCE", description="JWT 'aud' claim — tokens minted for another audience are rejected")

    # --- Authentication cookie ---
    AUTH_COOKIE_SECURE: bool = Field(default=False, env="AUTH_COOKIE_SECURE", description="Set True in production (HTTPS). Enables the Secure flag and the __Host- cookie prefix")
    AUTH_COOKIE_SAMESITE: str = Field(default="lax", env="AUTH_COOKIE_SAMESITE", description="Auth cookie SameSite policy: lax, strict or none")
    AUTH_COOKIE_HOST_PREFIX: bool = Field(default=True, env="AUTH_COOKIE_HOST_PREFIX", description="Use the __Host- cookie prefix when Secure is enabled")

    # --- Login CSRF / origin validation ---
    AUTH_ALLOWED_ORIGINS: str = Field(default="", env="AUTH_ALLOWED_ORIGINS", description="Comma-separated hosts allowed to submit credentials (the request Host is always allowed)")
    AUTH_TRUST_PROXY_HEADERS: bool = Field(default=True, env="AUTH_TRUST_PROXY_HEADERS", description="Trust X-Real-IP from the reverse proxy for client IP attribution")

    # --- Brute-force / credential-stuffing protection ---
    AUTH_RATE_LIMIT_ENABLED: bool = Field(default=True, env="AUTH_RATE_LIMIT_ENABLED", description="Enable login rate limiting (Redis-backed, shared across replicas)")
    AUTH_RATE_LIMIT_ACCOUNT_MAX: int = Field(default=8, env="AUTH_RATE_LIMIT_ACCOUNT_MAX", description="Failed logins per account before throttling")
    AUTH_RATE_LIMIT_ACCOUNT_WINDOW: int = Field(default=900, env="AUTH_RATE_LIMIT_ACCOUNT_WINDOW", description="Account throttle window in seconds")
    AUTH_RATE_LIMIT_IP_MAX: int = Field(default=30, env="AUTH_RATE_LIMIT_IP_MAX", description="Failed logins per source IP before throttling")
    AUTH_RATE_LIMIT_IP_WINDOW: int = Field(default=900, env="AUTH_RATE_LIMIT_IP_WINDOW", description="Source-IP throttle window in seconds")
    AUTH_RATE_LIMIT_GLOBAL_MAX: int = Field(default=600, env="AUTH_RATE_LIMIT_GLOBAL_MAX", description="Global login attempts per window (surge protection)")
    AUTH_RATE_LIMIT_GLOBAL_WINDOW: int = Field(default=60, env="AUTH_RATE_LIMIT_GLOBAL_WINDOW", description="Global surge window in seconds")

    # =====================================================
    # Database Configuration (PostgreSQL)
    # =====================================================
    DATABASE_URL: str = Field(
        default="postgresql+asyncpg://postgres:admin@postgres:5432/face_recognition",  # Docker: postgres hostname
        env="DATABASE_URL"
    )
    DB_HOST: str = Field(default="postgres", env="DB_HOST")
    DB_PORT: int = Field(default=5432, env="DB_PORT")
    POSTGRES_DB: str = Field(default="face_recognition", env="POSTGRES_DB")
    POSTGRES_USER: str = Field(default="postgres", env="POSTGRES_USER")
    POSTGRES_PASSWORD: str = Field(default="admin", env="POSTGRES_PASSWORD")
    
    # Connection Pool Settings
    # Database Connection Pool
    # For 50 cameras: Recommended DB_POOL_SIZE=75, DB_MAX_OVERFLOW=150
    # Default values are conservative for smaller deployments
    DB_POOL_SIZE: int = Field(default=50, env="DB_POOL_SIZE")
    DB_MAX_OVERFLOW: int = Field(default=100, env="DB_MAX_OVERFLOW")
    DB_POOL_RECYCLE: int = Field(default=3600, env="DB_POOL_RECYCLE")
    DB_POOL_PRE_PING: bool = Field(default=True, env="DB_POOL_PRE_PING")

    # =====================================================
    # Redis Cache Configuration
    # =====================================================
    REDIS_URL: str = Field(default="redis://redis:6379/0", env="REDIS_URL")  # Docker: redis hostname
    REDIS_MAX_CONNECTIONS: int = Field(default=100, env="REDIS_MAX_CONNECTIONS")
    REDIS_POOL_SIZE: int = Field(default=50, env="REDIS_POOL_SIZE")
    CACHE_TTL: int = Field(default=3600, env="CACHE_TTL", description="Cache TTL for dashboard data in seconds (default: 3600 = 1 hour)")
    CACHE_TTL_UNKNOWN: int = Field(default=108000, env="CACHE_TTL_UNKNOWN", description="Cache TTL for unknown faces page in seconds (default: 108000 = 30 hours)")
    CACHE_LOCAL_SIZE: int = Field(default=50000, env="CACHE_LOCAL_SIZE")
    CACHE_VERSION: str = Field(default="v1", env="CACHE_VERSION")
    CACHE_WARNING_ENABLED: bool = Field(default=True, env="CACHE_WARNING_ENABLED")
    CACHE_WARNING_INTERVAL: int = Field(default=300, env="CACHE_WARNING_INTERVAL")
    
    # =====================================================
    # Map Service Configuration
    # =====================================================
    MAP_CACHE_TTL: int = Field(default=3600, env="MAP_CACHE_TTL", description="Cache TTL for generated maps in seconds (default: 3600 = 1 hour)")
    MAP_CACHE_ENABLED: bool = Field(default=True, env="MAP_CACHE_ENABLED", description="Enable caching for map generation (default: True)")
    MAP_MAX_COORDINATES: int = Field(default=10000, env="MAP_MAX_COORDINATES", description="Maximum coordinates per map to prevent memory issues (default: 10000)")
    MAP_GENERATION_TIMEOUT: int = Field(default=30, env="MAP_GENERATION_TIMEOUT", description="Timeout for map generation in seconds (default: 30)")
    MAP_MAX_TRACKS: int = Field(default=100, env="MAP_MAX_TRACKS", description="Maximum tracks per map request (default: 100)")
    MAP_DEFAULT_STYLE: str = Field(default="light", env="MAP_DEFAULT_STYLE", description="Default map style: dark, light, satellite, terrain (default: light)")
    
    # Offline Map Tiles Configuration
    MAP_OFFLINE_TILES_PATH: Optional[str] = Field(default="./tiles", env="MAP_OFFLINE_TILES_PATH", description="Path to offline map tiles directory (format: {z}/{x}/{y}.png) or MBTiles file")
    MAP_OFFLINE_TILES_ENABLED: bool = Field(default=True, env="MAP_OFFLINE_TILES_ENABLED", description="Enable offline map tiles (requires MAP_OFFLINE_TILES_PATH to be set)")
    MAP_ENABLE_SECURITY_FEATURES: bool = Field(default=True, env="MAP_ENABLE_SECURITY_FEATURES", description="Enable security intelligence features by default (default: True)")
    MAP_DETECT_PATTERNS: bool = Field(default=True, env="MAP_DETECT_PATTERNS", description="Enable pattern detection by default (default: True)")
    MAP_SHOW_RISK_HEATMAP: bool = Field(default=True, env="MAP_SHOW_RISK_HEATMAP", description="Show risk heatmap by default (default: True)")
    MAP_SHOW_TIMELINE: bool = Field(default=False, env="MAP_SHOW_TIMELINE", description="Show timeline playback control by default (default: False)")
    
    # Animated Map Features
    MAP_SHOW_ANIMATED_AVATAR: bool = Field(default=False, env="MAP_SHOW_ANIMATED_AVATAR", description="Show animated avatar moving along route by default (default: False)")
    MAP_ANIMATION_PERIOD_SECONDS: int = Field(default=1, env="MAP_ANIMATION_PERIOD_SECONDS", description="Animation period: seconds of real time per frame (default: 1)")
    MAP_ANIMATION_MAX_DURATION_SECONDS: int = Field(default=600, env="MAP_ANIMATION_MAX_DURATION_SECONDS", description="Maximum animation duration in seconds (default: 600 = 10 minutes)")
    MAP_ANIMATION_MIN_SPEED: float = Field(default=0.5, env="MAP_ANIMATION_MIN_SPEED", description="Minimum animation playback speed multiplier (default: 0.5x)")
    MAP_ANIMATION_MAX_SPEED: float = Field(default=10.0, env="MAP_ANIMATION_MAX_SPEED", description="Maximum animation playback speed multiplier (default: 10x)")
    MAP_ANIMATION_TRANSITION_TIME_MS: int = Field(default=300, env="MAP_ANIMATION_TRANSITION_TIME_MS", description="Transition time between frames in milliseconds (default: 300)")
    
    # Co-Appearance Detection
    MAP_CO_APPEARANCE_TIME_WINDOW_SECONDS: int = Field(default=10, env="MAP_CO_APPEARANCE_TIME_WINDOW_SECONDS", description="Time window for detecting co-appearances in seconds (default: 10)")
    MAP_CO_APPEARANCE_DISTANCE_METERS: float = Field(default=100.0, env="MAP_CO_APPEARANCE_DISTANCE_METERS", description="Distance threshold for co-appearance detection in meters (default: 100)")
    MAP_CO_APPEARANCE_ENABLED: bool = Field(default=True, env="MAP_CO_APPEARANCE_ENABLED", description="Enable co-appearance detection for multi-identity tracking (default: True)")

    # =====================================================
    # Face Recognition Models
    # =====================================================
    DETECTION_MODEL: str = Field(default="/app/weights/det_10g.onnx", env="DETECTION_MODEL")  # Docker path
    RECOGNITION_MODEL: str = Field(default="/app/weights/w600k_r50.onnx", env="RECOGNITION_MODEL")  # Docker path
    SIMILARITY_THRESHOLD: float = Field(default=0.4, env="SIMILARITY_THRESHOLD")
    UNKNOWN_SIMILARITY_THRESHOLD: float = Field(default=0.35, env="UNKNOWN_SIMILARITY_THRESHOLD", description="Cosine similarity to match an existing UNKNOWN identity (below SIMILARITY_THRESHOLD creates fewer duplicate unknowns)")

    # --- Performance / concurrency ------------------------------------------
    # Threads dedicated to CPU-bound inference (ONNX + OpenCV release the GIL,
    # so a thread pool gives real parallelism without duplicating model memory).
    INFERENCE_WORKERS: int = Field(default=3, env="INFERENCE_WORKERS")
    # Max frames being inferred simultaneously (global) and per pipeline
    MAX_CONCURRENT_INFERENCE: int = Field(default=3, env="MAX_CONCURRENT_INFERENCE")
    MAX_CONCURRENT_INFERENCE_PER_PIPELINE: int = Field(default=2, env="MAX_CONCURRENT_INFERENCE_PER_PIPELINE")
    # Webhook ingress limits
    WEBHOOK_MAX_BODY_MB: int = Field(default=25, env="WEBHOOK_MAX_BODY_MB")
    WEBHOOK_DEDUP_TTL_SECONDS: int = Field(default=60, env="WEBHOOK_DEDUP_TTL_SECONDS")
    # SQL agent isolation
    SQL_AGENT_MAX_CONCURRENT: int = Field(default=2, env="SQL_AGENT_MAX_CONCURRENT")
    SQL_AGENT_TOTAL_TIMEOUT: int = Field(default=300, env="SQL_AGENT_TOTAL_TIMEOUT")

    # --- Identity auto-enrichment: confidently-matched runtime embeddings are added
    # to the identity so it learns the person's appearance range over time.
    IDENTITY_ENRICH_MIN_SIMILARITY: float = Field(default=0.55, env="IDENTITY_ENRICH_MIN_SIMILARITY")
    IDENTITY_ENRICH_MIN_QUALITY: float = Field(default=0.5, env="IDENTITY_ENRICH_MIN_QUALITY")
    IDENTITY_MAX_EMBEDDINGS: int = Field(default=20, env="IDENTITY_MAX_EMBEDDINGS")
    CONFIDENCE_THRESHOLD: float = Field(default=0.5, env="CONFIDENCE_THRESHOLD")
    FACES_DIR: str = Field(default="/app/storage/faces", env="FACES_DIR")  # Unified storage: known faces in storage/faces
    DB_PATH: str = Field(default="/app/database/face_database", env="DB_PATH")  # Docker path
    
    # =====================================================
    # Identity Index Configuration (FAISS / pgvector)
    # =====================================================
    IDENTITY_EMBEDDING_SIZE: int = Field(default=512, env="IDENTITY_EMBEDDING_SIZE")
    IDENTITY_INDEX_DB_PATH: str = Field(default="/app/database/identity_indexes", env="IDENTITY_INDEX_DB_PATH")  # Docker path
    IDENTITY_INDEX_AUTO_SAVE_INTERVAL: int = Field(default=300, env="IDENTITY_INDEX_AUTO_SAVE_INTERVAL")  # 5 minutes
    
    # Vector Search Backend Selection
    # Options: "pgvector" (RECOMMENDED for production) or "faiss" (faster but more complex)
    # 
    # RECOMMENDATION: Use pgvector for production because:
    # - ACID compliant (no sync issues)
    # - Simpler architecture (single data store)
    # - Automatic persistence
    # - Works with multi-instance deployments
    # - Fast enough for face recognition (5-20ms for 1M vectors)
    #
    # Use FAISS only if:
    # - You have > 5M known faces
    # - You absolutely need < 1ms search time
    # - You have resources for FAISS maintenance
    VECTOR_BACKEND: str = Field(
        default="pgvector",
        env="VECTOR_BACKEND",
        description="Vector search backend: 'pgvector' (RECOMMENDED for production) or 'faiss' (faster but requires sync logic)"
    )
    
    # pgvector Configuration
    PGVECTOR_INDEX_TYPE: str = Field(
        default="hnsw",
        env="PGVECTOR_INDEX_TYPE",
        description="pgvector index type: 'hnsw' (fast, recommended) or 'ivfflat' (memory efficient)"
    )
    PGVECTOR_HNSW_M: int = Field(default=16, env="PGVECTOR_HNSW_M", description="HNSW M parameter (connections per node, 16-64)")
    PGVECTOR_HNSW_EF_CONSTRUCTION: int = Field(default=100, env="PGVECTOR_HNSW_EF_CONSTRUCTION", description="HNSW efConstruction (build-time search width, 64-200, higher = better index quality)")
    PGVECTOR_HNSW_EF_SEARCH: int = Field(default=100, env="PGVECTOR_HNSW_EF_SEARCH", description="HNSW efSearch (search-time accuracy, 20-200, higher = more accurate but slower. RECOMMENDED: 100 for face recognition to match FAISS accuracy)")
    PGVECTOR_IVFFLAT_LISTS: int = Field(default=100, env="PGVECTOR_IVFFLAT_LISTS", description="IVFFlat lists (clusters, sqrt(N) is good)")
    PGVECTOR_IVFFLAT_PROBES: int = Field(default=10, env="PGVECTOR_IVFFLAT_PROBES", description="IVFFlat probes (clusters to search, 1-lists)")
    
    # FAISS Repair Configuration (only used when VECTOR_BACKEND=faiss)
    REPAIR_FAISS_ON_STARTUP: bool = Field(default=True, env="REPAIR_FAISS_ON_STARTUP")  # Enable/disable repair on startup
    REPAIR_FAISS_INTERVAL_HOURS: int = Field(default=24, env="REPAIR_FAISS_INTERVAL_HOURS")  # Background repair interval (hours)
    
    # Index Type Configuration
    # Options: "flat" (IndexFlatIP), "ivf" (IndexIVFFlat), "hnsw" (IndexHNSWFlat), "ivfpq" (IndexIVFPQ)
    KNOWN_INDEX_TYPE: str = Field(default="flat", env="KNOWN_INDEX_TYPE", description="Index type for KNOWN identities: flat, ivf, hnsw, ivfpq")
    UNKNOWN_INDEX_TYPE: str = Field(default="flat", env="UNKNOWN_INDEX_TYPE", description="Index type for UNKNOWN identities: flat (recommended)")
    
    # IVF Configuration (for IndexIVFFlat)
    KNOWN_INDEX_NLIST: int = Field(default=1000, env="KNOWN_INDEX_NLIST", description="Number of clusters for IVF index (sqrt(N) recommended, e.g., 1000 for 1M vectors)")
    KNOWN_INDEX_NPROBE: int = Field(default=20, env="KNOWN_INDEX_NPROBE", description="Number of clusters to search in IVF (10-50, higher = better accuracy, slower)")
    
    # HNSW Configuration (for IndexHNSWFlat)
    KNOWN_INDEX_HNSW_M: int = Field(default=32, env="KNOWN_INDEX_HNSW_M", description="HNSW M parameter (16-64, higher = better accuracy, more memory)")
    KNOWN_INDEX_HNSW_EF_CONSTRUCTION: int = Field(default=200, env="KNOWN_INDEX_HNSW_EF_CONSTRUCTION", description="HNSW efConstruction (200-400)")
    KNOWN_INDEX_HNSW_EF_SEARCH: int = Field(default=64, env="KNOWN_INDEX_HNSW_EF_SEARCH", description="HNSW efSearch (16-128)")
    
    # IVFPQ Configuration (for IndexIVFPQ)
    KNOWN_INDEX_PQ_M: int = Field(default=64, env="KNOWN_INDEX_PQ_M", description="PQ m parameter (8, 16, 32, 64)")
    KNOWN_INDEX_PQ_BITS: int = Field(default=8, env="KNOWN_INDEX_PQ_BITS", description="PQ bits per subquantizer (8 is standard)")

    # =====================================================
    # Queue & Processing Configuration
    # =====================================================
    MAX_QUEUE_SIZE: int = Field(default=10000, env="MAX_QUEUE_SIZE")
    QUEUE_WORKERS: int = Field(default=50, env="QUEUE_WORKERS")
    BATCH_SIZE: int = Field(default=20, env="BATCH_SIZE")
    MAX_CONCURRENT_REQUESTS: int = Field(default=500, env="MAX_CONCURRENT_REQUESTS")
    GPU_BATCH_SIZE: int = Field(default=32, env="GPU_BATCH_SIZE")
    CPU_BATCH_SIZE: int = Field(default=10, env="CPU_BATCH_SIZE")
    PIPELINE_BATCH_SIZE: int = Field(default=5, env="PIPELINE_BATCH_SIZE")
    
    # Rate Limiting
    RATE_LIMIT_ENABLED: bool = Field(default=False, env="RATE_LIMIT_ENABLED")
    RATE_LIMIT_INTERVAL: float = Field(default=1.0, env="RATE_LIMIT_INTERVAL")

    # =====================================================
    # Storage Configuration
    # =====================================================
    STORAGE_DIR: str = Field(default="/app/storage", env="STORAGE_DIR")  # Docker path
    SAVE_IMAGES: bool = Field(default=True, env="SAVE_IMAGES")
    MAX_STORAGE_GB: int = Field(default=500, env="MAX_STORAGE_GB")
    MAX_PHOTOS_PER_PERSON: int = Field(default=1, env="MAX_PHOTOS_PER_PERSON")
    SAVE_UNKNOWN_FACES: bool = Field(default=False, env="SAVE_UNKNOWN_FACES")
    MAX_FILE_SIZE: int = Field(default=10485760, env="MAX_FILE_SIZE")  # 10MB in bytes
    
    # Webhook Debug Configuration
    SAVE_WEBHOOK_IMAGES: bool = Field(default=True, env="SAVE_WEBHOOK_IMAGES", description="Save all images received via webhook for debugging")
    WEBHOOK_IMAGES_DIR: str = Field(default="./debug/webhook_images", env="WEBHOOK_IMAGES_DIR", description="Directory to save webhook images")
    
    # Crop Debug Configuration
    SAVE_CROPPED_IMAGES: bool = Field(default=True, env="SAVE_CROPPED_IMAGES", description="Save cropped person images for debugging")
    CROPPED_IMAGES_DIR: str = Field(default="./debug/cropped", env="CROPPED_IMAGES_DIR", description="Directory to save cropped images")

    # =====================================================
    # Monitoring & Metrics
    # =====================================================
    ENABLE_METRICS: bool = Field(default=True, env="ENABLE_METRICS")
    METRICS_PORT: int = Field(default=9090, env="METRICS_PORT")

    # =====================================================
    # CORS Configuration
    # =====================================================
    CORS_ORIGINS: str = Field(default="*", env="CORS_ORIGINS")

    @property
    def cors_origins_list(self) -> List[str]:
        """Origins allowed by CORS."""
        return parse_origin_list(self.CORS_ORIGINS)

    @property
    def cors_allows_wildcard(self) -> bool:
        return "*" in self.cors_origins_list

    @property
    def cors_allow_credentials(self) -> bool:
        """Credentials and a wildcard origin are mutually exclusive.

        Starlette echoes the request Origin back with
        Access-Control-Allow-Credentials: true when both are set, which lets any
        site read authenticated responses. Never return True alongside "*".
        """
        return not self.cors_allows_wildcard

    @property
    def auth_allowed_origins_list(self) -> List[str]:
        """Origins allowed to submit credentials."""
        return parse_origin_list(self.AUTH_ALLOWED_ORIGINS)

    @property
    def approved_origins(self) -> List[str]:
        """The single approved-origin policy.

        CORS, login-Origin validation, CSRF and WebSocket Origin checks all
        resolve through here so they can never drift apart. AUTH_ALLOWED_ORIGINS
        wins when set; otherwise the non-wildcard CORS entries are used.
        """
        explicit = self.auth_allowed_origins_list
        if explicit:
            return explicit
        return [origin for origin in self.cors_origins_list if origin != "*"]

    @property
    def is_production(self) -> bool:
        return str(self.ENVIRONMENT).strip().lower() in ("production", "prod")

    # =====================================================
    # Data Retention & Cleanup
    # =====================================================
    DATA_RETENTION_DAYS: int = Field(default=30, env="DATA_RETENTION_DAYS")
    CLEANUP_INTERVAL_HOURS: int = Field(default=24, env="CLEANUP_INTERVAL_HOURS")

    # =====================================================
    # Batch Processing
    # =====================================================
    BATCH_WRITE_SIZE: int = Field(default=50, env="BATCH_WRITE_SIZE")
    BATCH_WRITE_INTERVAL: float = Field(default=1.0, env="BATCH_WRITE_INTERVAL")
    BATCH_WRITE_MAX_WAIT: float = Field(default=5.0, env="BATCH_WRITE_MAX_WAIT")

    # =====================================================
    # Face Tracking (Optimization)
    # =====================================================
    FACE_TRACKING_ENABLED: bool = Field(default=True, env="FACE_TRACKING_ENABLED")
    FACE_TRACKING_WINDOW_SECONDS: int = Field(default=0, env="FACE_TRACKING_WINDOW_SECONDS")
    FACE_TRACKING_MAX_ENTRIES: int = Field(default=5000, env="FACE_TRACKING_MAX_ENTRIES")
    FACE_TRACKING_SIMILARITY_THRESHOLD: float = Field(default=0.95, env="FACE_TRACKING_SIMILARITY_THRESHOLD")
    SKIP_UNKNOWN_FACES: bool = Field(default=False, env="SKIP_UNKNOWN_FACES")
    SHOW_UNKNOWN_FACES_ON_DASHBOARD: bool = Field(default=False, env="SHOW_UNKNOWN_FACES_ON_DASHBOARD", description="If True, unknown faces will appear on the main dashboard. If False (default), unknown faces are only visible in the Unknown Faces Center page.")
    FAISS_LAZY_MARKING_THRESHOLD: int = Field(default=1, env="FAISS_LAZY_MARKING_THRESHOLD", description="Threshold for FAISS lazy marking approach. If mismatch count is below this threshold, orphaned vectors are lazy-marked (skipped during search) instead of rebuilding the entire index. Default: 1 (for demo). For production with large datasets, use 100 or higher.")
    FACE_TRACKING_MAX_MEMORY_MB: int = Field(default=2000, env="FACE_TRACKING_MAX_MEMORY_MB")
    FACE_TRACKING_CLEANUP_INTERVAL: int = Field(default=300, env="FACE_TRACKING_CLEANUP_INTERVAL")
    
    # =====================================================
    # Dashboard Display Settings
    # =====================================================
    DASHBOARD_FACE_DISPLAY_HOURS: int = Field(default=3, env="DASHBOARD_FACE_DISPLAY_HOURS", description="How many hours of face detections to show on dashboard. Default: 3 hours. Faces older than this are hidden from the dashboard but still stored in database.")
    UNKNOWN_FACE_DISPLAY_HOURS: float = Field(default=24, env="UNKNOWN_FACE_DISPLAY_HOURS", description="How many hours unknown faces stay visible on the Unknown Faces page (display-only — data stays stored until retention deletes it). 0 = show all. A Show-all toggle on the page reveals older ones.")
    ALERT_NOTIFICATION_WINDOW_HOURS: float = Field(default=1.0, env="ALERT_NOTIFICATION_WINDOW_HOURS", description="Hours between alert popups for the same person on the same camera. Default: 1 hour. Set to 0 to alert on every detection (not recommended - noisy).")

    # =====================================================
    # Identity Management - Clustering & Merge Suggestions
    # =====================================================
    # These settings control the automatic merge suggestion system
    # The clustering job runs periodically to find duplicate identities
    CLUSTER_INTERVAL_HOURS: int = Field(default=24, env="CLUSTER_INTERVAL_HOURS", description="Hours between clustering runs (default: 24)")
    CLUSTER_STARTUP_DELAY_HOURS: float = Field(default=7, env="CLUSTER_STARTUP_DELAY_HOURS", description="Hours to wait after startup before first clustering run. Set to 0 for immediate. (default: 0)")
    CLUSTER_MIN_SIZE: int = Field(default=2, env="CLUSTER_MIN_SIZE", description="Minimum cluster size for merge suggestions (default: 2)")
    CLUSTER_EPS: float = Field(default=0.35, env="CLUSTER_EPS", description="Epsilon parameter for DBSCAN clustering. Lower = stricter matching. (default: 0.35)")
    CLUSTER_MIN_SAMPLES: int = Field(default=2, env="CLUSTER_MIN_SAMPLES", description="Minimum samples per cluster for DBSCAN (default: 2)")
    
    # Pipeline-Aware ML Clustering
    PIPELINE_AWARE_CLUSTERING_ENABLED: bool = Field(default=True, env="PIPELINE_AWARE_CLUSTERING_ENABLED", description="Enable pipeline-aware ML clustering for merge suggestions (default: True)")
    PIPELINE_SIMILARITY_WEIGHT: float = Field(default=0.3, env="PIPELINE_SIMILARITY_WEIGHT", description="Weight for pipeline overlap in similarity calculation (0.0-1.0, default: 0.3)")
    EMBEDDING_SIMILARITY_WEIGHT: float = Field(default=0.7, env="EMBEDDING_SIMILARITY_WEIGHT", description="Weight for embedding similarity in calculation (0.0-1.0, default: 0.7)")
    CROSS_PIPELINE_SIMILARITY_THRESHOLD: float = Field(default=0.50, env="CROSS_PIPELINE_SIMILARITY_THRESHOLD", description="Minimum similarity threshold for cross-pipeline matches (default: 0.50)")
    
    # ML Similarity Model Training
    SIMILARITY_MODEL_PATH: str = Field(default="models/similarity_model.pkl", env="SIMILARITY_MODEL_PATH", description="Path to save/load the trained similarity model (default: models/similarity_model.pkl)")
    SIMILARITY_MODEL_MIN_SAMPLES: int = Field(default=50, env="SIMILARITY_MODEL_MIN_SAMPLES", description="Minimum training samples required before model can be trained (default: 50)")
    SIMILARITY_MODEL_AUTO_TRAIN: bool = Field(default=True, env="SIMILARITY_MODEL_AUTO_TRAIN", description="Automatically train model when enough samples are collected (default: True)")

    # =====================================================
    # Identity Management - Quality Thresholds
    # =====================================================
    IDENTITY_QUALITY_THRESHOLD_KNOWN: float = Field(
        default=0.5,
        env="IDENTITY_QUALITY_THRESHOLD_KNOWN",
        description="Minimum quality score (0-1) to save embedding for KNOWN identities. Higher = stricter. Default: 0.5"
    )
    IDENTITY_QUALITY_THRESHOLD_UNKNOWN: float = Field(
        default=0.1,
        env="IDENTITY_QUALITY_THRESHOLD_UNKNOWN",
        description="Minimum quality score (0-1) to save embedding for UNKNOWN identities. Lower = save more. Default: 0.1 (saves almost all)"
    )
    
    # =====================================================
    # Identity Management - Retention
    # =====================================================
    SNAPSHOT_RETENTION_DAYS: int = Field(default=90, env="SNAPSHOT_RETENTION_DAYS")
    EMBEDDING_RETENTION_MONTHS: int = Field(default=12, env="EMBEDDING_RETENTION_MONTHS")
    INACTIVE_THRESHOLD_DAYS: int = Field(default=180, env="INACTIVE_THRESHOLD_DAYS")
    IDENTITY_CLEANUP_INTERVAL_HOURS: int = Field(default=24, env="IDENTITY_CLEANUP_INTERVAL_HOURS")
    MAX_EMBEDDINGS_PER_IDENTITY: int = Field(default=10, env="MAX_EMBEDDINGS_PER_IDENTITY")

    # =====================================================
    # Identity Management - FAISS Index
    # =====================================================
    # Note: IDENTITY_EMBEDDING_SIZE and IDENTITY_INDEX_DB_PATH are already defined above
    # This section is kept for documentation purposes

    # =====================================================
    # Advanced Search Configuration
    # =====================================================
    # Quality Scoring
    SEARCH_MIN_QUALITY_THRESHOLD: float = Field(
        default=0.3,
        env="SEARCH_MIN_QUALITY_THRESHOLD",
        description="Minimum quality score (0-1) to attempt search. Faces below this are skipped. Default: 0.3"
    )
    SEARCH_QUALITY_WARNING_THRESHOLD: float = Field(
        default=0.6,
        env="SEARCH_QUALITY_WARNING_THRESHOLD",
        description="Quality threshold (0-1) below which a warning is shown. Default: 0.6"
    )
    
    # Confidence Bands
    CONFIDENCE_VERY_HIGH_MIN: float = Field(
        default=0.90,
        env="CONFIDENCE_VERY_HIGH_MIN",
        description="Minimum similarity for 'Very High' confidence band. Default: 0.90"
    )
    CONFIDENCE_HIGH_MIN: float = Field(
        default=0.75,
        env="CONFIDENCE_HIGH_MIN",
        description="Minimum similarity for 'High' confidence band. Default: 0.75"
    )
    CONFIDENCE_MEDIUM_MIN: float = Field(
        default=0.60,
        env="CONFIDENCE_MEDIUM_MIN",
        description="Minimum similarity for 'Medium' confidence band. Default: 0.60"
    )
    CONFIDENCE_LOW_MIN: float = Field(
        default=0.40,
        env="CONFIDENCE_LOW_MIN",
        description="Minimum similarity for 'Low' confidence band. Default: 0.40"
    )
    
    # Batch Search
    BATCH_SEARCH_MAX_IMAGES: int = Field(
        default=20,
        env="BATCH_SEARCH_MAX_IMAGES",
        description="Maximum number of images allowed per batch search. Default: 20"
    )
    BATCH_SEARCH_TIMEOUT_SECONDS: int = Field(
        default=300,
        env="BATCH_SEARCH_TIMEOUT_SECONDS",
        description="Timeout in seconds for batch search operations. Default: 300 (5 minutes)"
    )
    
    # Search History
    SEARCH_HISTORY_RETENTION_DAYS: int = Field(
        default=90,
        env="SEARCH_HISTORY_RETENTION_DAYS",
        description="Days to retain search history records. Default: 90"
    )
    SEARCH_HISTORY_MAX_PER_USER: int = Field(
        default=1000,
        env="SEARCH_HISTORY_MAX_PER_USER",
        description="Maximum search history records per user. Default: 1000"
    )

    # Audit log retention (chatbot/identity/settings audit tables)
    AUDIT_LOG_RETENTION_DAYS: int = Field(
        default=180,
        env="AUDIT_LOG_RETENTION_DAYS",
        description="Days to retain audit-log records (chatbot, identity, settings). Default: 180"
    )
    
    # Live Alerts
    LIVE_ALERT_DEFAULT_COOLDOWN_MINUTES: int = Field(
        default=30,
        env="LIVE_ALERT_DEFAULT_COOLDOWN_MINUTES",
        description="Default cooldown period (minutes) between live alert triggers. Default: 30"
    )
    LIVE_ALERT_MAX_PER_USER: int = Field(
        default=50,
        env="LIVE_ALERT_MAX_PER_USER",
        description="Maximum number of active live alerts per user. Default: 50"
    )
    LIVE_ALERT_MAX_PER_IDENTITY: int = Field(
        default=5,
        env="LIVE_ALERT_MAX_PER_IDENTITY",
        description="Maximum number of active live alerts that can be created for the same identity. Default: 5, Max: 10"
    )
    
    # Related Identities
    RELATED_IDENTITY_MIN_CO_APPEARANCES: int = Field(
        default=3,
        env="RELATED_IDENTITY_MIN_CO_APPEARANCES",
        description="Minimum co-appearances required to establish related identity relationship. Default: 3"
    )
    RELATED_IDENTITY_TIME_WINDOW_MINUTES: int = Field(
        default=30,
        env="RELATED_IDENTITY_TIME_WINDOW_MINUTES",
        description="Time window (minutes) for considering identities as co-appearing. Default: 30"
    )
    
    # Multi-Camera Social Network Analysis
    MULTI_CAMERA_CO_APPEARANCE_ENABLED: bool = Field(
        default=True,
        env="MULTI_CAMERA_CO_APPEARANCE_ENABLED",
        description="Enable cross-camera co-appearance detection for social network analysis (default: True)"
    )
    MULTI_CAMERA_DISTANCE_METERS: float = Field(
        default=500.0,
        env="MULTI_CAMERA_DISTANCE_METERS",
        description="Maximum distance in meters between cameras to consider cross-camera co-appearance (default: 500)"
    )
    MULTI_CAMERA_TIME_WINDOW_MINUTES: int = Field(
        default=10,
        env="MULTI_CAMERA_TIME_WINDOW_MINUTES",
        description="Time window in minutes for cross-camera co-appearance detection (default: 10, larger than same-camera)"
    )
    MULTI_CAMERA_MIN_CO_APPEARANCES: int = Field(
        default=2,
        env="MULTI_CAMERA_MIN_CO_APPEARANCES",
        description="Minimum cross-camera co-appearances to establish relationship (default: 2, lower than same-camera)"
    )
    
    # Advanced Features
    AUTO_THRESHOLD_LEARNING_ENABLED: bool = Field(
        default=True,
        env="AUTO_THRESHOLD_LEARNING_ENABLED",
        description="Enable automatic learning of optimal thresholds per camera pair (default: True)"
    )
    TRAJECTORY_PREDICTION_ENABLED: bool = Field(
        default=True,
        env="TRAJECTORY_PREDICTION_ENABLED",
        description="Enable trajectory prediction for proactive relationship detection (default: True)"
    )
    ACTIVITY_CORRELATION_ENABLED: bool = Field(
        default=True,
        env="ACTIVITY_CORRELATION_ENABLED",
        description="Enable activity correlation analysis (xCCA) for causal relationship detection (default: True)"
    )
    
    # Feature Flags
    FACE_QUALITY_ENABLED: bool = Field(
        default=True,
        env="FACE_QUALITY_ENABLED",
        description="Enable face quality scoring. Default: True"
    )
    WATCHLIST_ENABLED: bool = Field(
        default=True,
        env="WATCHLIST_ENABLED",
        description="Enable watchlist functionality. Default: True"
    )
    LIVE_ALERTS_ENABLED: bool = Field(
        default=True,
        env="LIVE_ALERTS_ENABLED",
        description="Enable live search alerts. Default: True"
    )
    RELATED_IDENTITIES_ENABLED: bool = Field(
        default=True,
        env="RELATED_IDENTITIES_ENABLED",
        description="Enable related identities analysis. Default: True"
    )
    TEMPORAL_PATTERNS_ENABLED: bool = Field(
        default=True,
        env="TEMPORAL_PATTERNS_ENABLED",
        description="Enable temporal pattern analysis. Default: True"
    )
    CROSS_CAMERA_TRACKING_ENABLED: bool = Field(
        default=True,
        env="CROSS_CAMERA_TRACKING_ENABLED",
        description="Enable cross-camera tracking. Default: True"
    )
    BATCH_SEARCH_ENABLED: bool = Field(
        default=True,
        env="BATCH_SEARCH_ENABLED",
        description="Enable batch search functionality. Default: True"
    )
    EXPORT_RESULTS_ENABLED: bool = Field(
        default=True,
        env="EXPORT_RESULTS_ENABLED",
        description="Enable search results export (CSV, PDF, JSON). Default: True"
    )
    NEGATIVE_SEARCH_ENABLED: bool = Field(
        default=True,
        env="NEGATIVE_SEARCH_ENABLED",
        description="Enable negative search (exclude specific identities). Default: True"
    )
    
    # Face Quality Thresholds (for quality scoring)
    FACE_QUALITY_THRESHOLD_BLUR: float = Field(
        default=0.5,
        env="FACE_QUALITY_THRESHOLD_BLUR",
        description="Blur threshold for face quality assessment. Default: 0.5"
    )
    FACE_QUALITY_THRESHOLD_SIZE: int = Field(
        default=64,
        env="FACE_QUALITY_THRESHOLD_SIZE",
        description="Minimum face size (pixels) for quality assessment. Default: 64"
    )
    FACE_QUALITY_THRESHOLD_ANGLE: float = Field(
        default=30.0,
        env="FACE_QUALITY_THRESHOLD_ANGLE",
        description="Maximum face angle (degrees) for quality assessment. Default: 30.0"
    )
    FACE_QUALITY_THRESHOLD_LIGHTING: float = Field(
        default=0.4,
        env="FACE_QUALITY_THRESHOLD_LIGHTING",
        description="Lighting threshold for face quality assessment. Default: 0.4"
    )

    # =====================================================
    # Ollama Configuration (for SQL Agent)
    # =====================================================
    OLLAMA_BASE_URL: str = Field(
        default="http://ollama:11434",
        env="OLLAMA_BASE_URL"
    )
    OLLAMA_MODEL: str = Field(
        default="llama3.2:3b",
        env="OLLAMA_MODEL"
    )
    # Specialist model for SQL generation steps only (falls back to OLLAMA_MODEL when empty).
    # Lets a small fast model handle chat/intent while a text-to-SQL model writes queries.
    OLLAMA_SQL_MODEL: str = Field(
        default="",
        env="OLLAMA_SQL_MODEL"
    )
    OLLAMA_TEMPERATURE: float = Field(
        default=0.1,
        env="OLLAMA_TEMPERATURE"
    )
    OLLAMA_TIMEOUT: int = Field(
        default=120,
        env="OLLAMA_TIMEOUT"
    )

    # =====================================================
    # SQL Agent Configuration
    # =====================================================
    CHROMADB_PATH: str = Field(
        default="./sql_agent/chromadb_data",
        env="CHROMADB_PATH"
    )
    CHROMA_COLLECTION_NAME: str = Field(
        default="sql_knowledge_base",
        env="CHROMA_COLLECTION_NAME"
    )
    RAG_TOP_K: int = Field(
        default=5,
        env="RAG_TOP_K"
    )
    RAG_SIMILARITY_THRESHOLD: float = Field(
        default=0.3,
        env="RAG_SIMILARITY_THRESHOLD"
    )

    # =====================================================
    # File Upload Configuration
    # =====================================================
    ALLOWED_IMAGE_EXTENSIONS: str = Field(
        default=".jpg,.jpeg,.png,.webp",
        env="ALLOWED_IMAGE_EXTENSIONS"
    )
    
    @property
    def allowed_image_extensions_list(self) -> List[str]:
        """Parse ALLOWED_IMAGE_EXTENSIONS from comma-separated string"""
        return [ext.strip() for ext in self.ALLOWED_IMAGE_EXTENSIONS.split(",")]

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"
        case_sensitive = True
        # Allow reading from .env file in the same directory as this file
        env_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")


# Create global settings instance
settings = Settings()


# Helper function to print configuration (for debugging)
def print_config():
    """Print current configuration (without sensitive data)"""
    print("\n" + "="*60)
    print(f"🚀 {settings.APP_NAME} v{settings.VERSION}")
    print("="*60)
    print(f"Environment: {settings.ENVIRONMENT}")
    print(f"Debug Mode: {settings.DEBUG}")
    print(f"Host: {settings.HOST}:{settings.PORT}")
    print(f"Workers: {settings.WORKERS}")
    print(f"GPU Enabled: {settings.USE_GPU}")
    print("\n📊 Processing Configuration:")
    print(f"  Queue Workers: {settings.QUEUE_WORKERS}")
    print(f"  Max Queue Size: {settings.MAX_QUEUE_SIZE}")
    print(f"  Max Concurrent: {settings.MAX_CONCURRENT_REQUESTS}")
    print(f"  Batch Size: {settings.BATCH_SIZE}")
    print(f"  Batch Write Size: {settings.BATCH_WRITE_SIZE}")
    print("\n🎯 Face Recognition:")
    print(f"  Detection Model: {settings.DETECTION_MODEL}")
    print(f"  Recognition Model: {settings.RECOGNITION_MODEL}")
    print(f"  Similarity Threshold: {settings.SIMILARITY_THRESHOLD}")
    print(f"  Confidence Threshold: {settings.CONFIDENCE_THRESHOLD}")
    print(f"  Faces Directory: {settings.FACES_DIR}")
    print("\n🔍 Face Tracking (Optimization):")
    print(f"  Enabled: {settings.FACE_TRACKING_ENABLED}")
    print(f"  Tracking Window: {settings.FACE_TRACKING_WINDOW_SECONDS}s")
    print(f"  Max Entries: {settings.FACE_TRACKING_MAX_ENTRIES}")
    print(f"  Similarity Threshold: {settings.FACE_TRACKING_SIMILARITY_THRESHOLD}")
    print("\n💾 Storage:")
    print(f"  Directory: {settings.STORAGE_DIR}")
    print(f"  Save Images: {settings.SAVE_IMAGES}")
    print(f"  Max Storage: {settings.MAX_STORAGE_GB} GB")
    print("\n🗄️  Database:")
    print(f"  Pool Size: {settings.DB_POOL_SIZE}")
    print(f"  Max Overflow: {settings.DB_MAX_OVERFLOW}")
    print("\n📦 Cache:")
    print(f"  Redis URL: {settings.REDIS_URL}")
    print(f"  Max Connections: {settings.REDIS_MAX_CONNECTIONS}")
    print(f"  TTL: {settings.CACHE_TTL}s")
    print("\n🔄 Identity Management:")
    print(f"  Clustering Interval: {settings.CLUSTER_INTERVAL_HOURS}h")
    print(f"  Snapshot Retention: {settings.SNAPSHOT_RETENTION_DAYS} days")
    print(f"  Embedding Retention: {settings.EMBEDDING_RETENTION_MONTHS} months")
    print("\n🧹 Data Retention:")
    print(f"  Retention Days: {settings.DATA_RETENTION_DAYS}")
    print(f"  Cleanup Interval: {settings.CLEANUP_INTERVAL_HOURS}h")
    print("="*60 + "\n")


if __name__ == "__main__":
    # Load and print configuration
    print_config()

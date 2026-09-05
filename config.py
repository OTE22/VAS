"""
Centralized Configuration Management
====================================
Single source of truth for all configuration variables.
Loads from .env file in the root directory.
"""

import json
import os
from typing import List, Optional
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field, model_validator


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
    ENVIRONMENT: str = Field(default="production")
    DEBUG: bool = Field(default=False)

    # Development trace of the CONTEXT ENVELOPE each model call receives:
    # section presence and sizes in the log, and full tool-argument values.
    # Off in production — the values are the user's own words and the names of
    # people under surveillance, which the audit rules keep out of logs.
    SQL_AGENT_TRACE_CONTEXT: bool = Field(default=False)
    
    HOST: str = Field(default="0.0.0.0")
    PORT: int = Field(default=8000)
    WORKERS: int = Field(default=4)
    
    # GPU Configuration
    USE_GPU: bool = Field(default=False)
    
    # Application Info
    APP_NAME: str = "Face Recognition Service"
    VERSION: str = "5.0.0"

    # Runtime provenance — reported by the startup fingerprint and by
    # /health/detailed so that "is the runtime what the repository says?" is
    # answerable without docker inspect. Declared here rather than read from
    # os.environ because config.py is the ONE module allowed to touch the
    # environment; a second reader is a second source of truth.
    #
    # GIT_COMMIT is injected at build or run time (there is no git binary in
    # the production image). Empty means "not injected" — the fingerprint then
    # falls back to reading .git directly, which works in the dev stack because
    # it bind-mounts the repository.
    GIT_COMMIT: str = Field(default="")
    # Docker sets HOSTNAME to the container's short id. It is what maps a log
    # line back to a row of `docker ps`; it is NOT the image digest.
    HOSTNAME: str = Field(default="")
    
    # =====================================================
    # Logging
    # =====================================================
    # ONE rotating file, written by utils/logging.py, read by GET /api/logs.
    # Both ends resolve the path from these settings — neither hard-codes it,
    # and the API never accepts a path from a client.
    LOG_DIR: str = Field(default="/var/log/face-recognition")  # Docker path
    LOG_LEVEL: str = Field(default="INFO")
    LOG_FILE_NAME: str = Field(
        default="app.log",
        description="Active log file inside LOG_DIR. Rotated siblings are app.log.1, app.log.2, ..."
    )
    LOG_MAX_BYTES: int = Field(
        default=10 * 1024 * 1024,
        description="Rotate the active log file once it reaches this size. Default: 10 MiB"
    )
    LOG_BACKUP_COUNT: int = Field(
        default=5,
        description="How many rotated files to keep (app.log.1 ... app.log.N). Default: 5"
    )
    # Normally equal to LOG_LEVEL, so stdout and the file carry identical
    # records. Lower it ONLY to keep a verbose trace on disk that would drown
    # `docker logs` — the [UPLOAD_MATCH] per-candidate stages are the reason
    # this exists. Empty string means "same as LOG_LEVEL".
    LOG_FILE_LEVEL: str = Field(
        default="",
        description="Level for the rotating file. Empty = same as LOG_LEVEL (the default)."
    )
    LOGS_LIFE_TIME_HOURS: int = Field(default=48, description="Log retention period in hours (default: 48 hours)")

    # Bounds for GET /api/logs. A log read must never become a way to pin the
    # event loop or exhaust memory on a multi-gigabyte file.
    LOG_API_DEFAULT_PAGE_SIZE: int = Field(
        default=50,
        description="Log lines per page when the caller does not specify. Default: 50"
    )
    LOG_API_MAX_PAGE_SIZE: int = Field(
        default=500,
        description="Hard ceiling on log lines per page; larger requests are rejected. Default: 500"
    )
    LOG_API_MAX_SCAN_FILES: int = Field(
        default=6,
        description="Most files (active + rotated) a single log query may open. Default: 6"
    )
    LOG_API_MAX_SCAN_BYTES: int = Field(
        default=64 * 1024 * 1024,
        description="Most bytes a single log query may read across all files. Default: 64 MiB"
    )
    LOG_API_TIMEOUT_SECONDS: float = Field(
        default=10.0,
        description="Wall-clock budget for one log query; exceeded returns a partial-scan flag. Default: 10"
    )

    # Background Task Notifications
    BACKGROUND_TASK_NOTIFICATIONS_ENABLED: bool = Field(default=True, description="Enable real-time notifications for background tasks (default: True)")
    BACKGROUND_TASK_NOTIFICATION_LEAD_TIME_SECONDS: int = Field(default=60, description="Seconds before task start to send notification (default: 60 = 1 minute)")

    # =====================================================
    # Security & Authentication
    # =====================================================
    JWT_SECRET_KEY: str = Field(
        default="your-secret-key-change-in-production"
    )
    # Docker-secret file paths. When set, the file contents replace the inline
    # value above, so the secret never appears in compose files, `docker
    # inspect`, or the process environment.
    JWT_SECRET_KEY_FILE: str = Field(default="")
    JWT_ALGORITHM: str = Field(default="HS256")
    ACCESS_TOKEN_EXPIRE_MINUTES: int = Field(default=1440)  # 24 hours
    JWT_ISSUER: str = Field(default="face-recognition-service", description="JWT 'iss' claim — tokens from other issuers are rejected")
    JWT_AUDIENCE: str = Field(default="face-recognition-api", description="JWT 'aud' claim — tokens minted for another audience are rejected")

    # --- Authentication cookie ---
    AUTH_COOKIE_SECURE: bool = Field(default=False, description="Set True in production (HTTPS). Enables the Secure flag and the __Host- cookie prefix")
    AUTH_COOKIE_SAMESITE: str = Field(default="lax", description="Auth cookie SameSite policy: lax, strict or none")
    AUTH_COOKIE_HOST_PREFIX: bool = Field(default=True, description="Use the __Host- cookie prefix when Secure is enabled")

    # --- Login CSRF / origin validation ---
    AUTH_ALLOWED_ORIGINS: str = Field(default="", description="Comma-separated hosts allowed to submit credentials (the request Host is always allowed)")
    AUTH_TRUST_PROXY_HEADERS: bool = Field(default=True, description="Trust X-Real-IP from the reverse proxy for client IP attribution")
    AUTH_SAME_HOST_ORIGIN_TRUSTED: bool = Field(default=True, description="Treat the request Host as a valid credential-submission origin. Set False in production once AUTH_ALLOWED_ORIGINS lists every real hostname")

    # --- Brute-force / credential-stuffing protection ---
    AUTH_RATE_LIMIT_ENABLED: bool = Field(default=True, description="Enable login rate limiting (Redis-backed, shared across replicas)")
    AUTH_RATE_LIMIT_ACCOUNT_MAX: int = Field(default=8, description="Failed logins per account before throttling")
    AUTH_RATE_LIMIT_ACCOUNT_WINDOW: int = Field(default=900, description="Account throttle window in seconds")
    AUTH_RATE_LIMIT_IP_MAX: int = Field(default=30, description="Failed logins per source IP before throttling")
    AUTH_RATE_LIMIT_IP_WINDOW: int = Field(default=900, description="Source-IP throttle window in seconds")
    AUTH_RATE_LIMIT_GLOBAL_MAX: int = Field(default=600, description="Global login attempts per window (surge protection)")
    AUTH_RATE_LIMIT_GLOBAL_WINDOW: int = Field(default=60, description="Global surge window in seconds")

    # =====================================================
    # Bootstrap administrator
    # =====================================================
    # Used once, only when no administrator account exists. The password is
    # never logged and never generated-and-printed.
    BOOTSTRAP_ADMIN_ENABLED: bool = Field(default=True, description="Allow creating the first administrator when none exists")
    BOOTSTRAP_ADMIN_USERNAME: str = Field(default="admin")
    BOOTSTRAP_ADMIN_EMAIL: str = Field(default="admin@example.com")
    BOOTSTRAP_ADMIN_PASSWORD: str = Field(default="", description="First-admin password. Prefer BOOTSTRAP_ADMIN_PASSWORD_FILE")
    BOOTSTRAP_ADMIN_PASSWORD_FILE: str = Field(default="", description="Path to a Docker secret holding the first-admin password")
    BOOTSTRAP_ADMIN_REQUIRE_ROTATION: bool = Field(default=True, description="Force a password change on the bootstrapped account's first login")

    # =====================================================
    # Production posture
    # =====================================================
    # Consumed by backend/security/config_guard.py. In production the guard
    # refuses to start the process when these are unsafe.
    ENABLE_API_DOCS: bool = Field(default=True, description="Serve /docs, /redoc and /openapi.json. Must be false in production")
    ALLOW_MULTI_WORKER: bool = Field(default=False, description="Escape hatch for WORKERS>1. Only set once process-local state is externalized — see backend/core/runtime_settings.py")
    ALLOW_CPU_FALLBACK: bool = Field(default=True, description="Permit silent CPU inference when USE_GPU is set but CUDA is unavailable. Set False on real GPU deployments")

    # =====================================================
    # Database migrations
    # =====================================================
    # run    -> apply `alembic upgrade head`
    # verify -> compare current revision against head; a mismatch is fatal
    # skip   -> do nothing (the entrypoint already verified)
    MIGRATIONS_MODE: str = Field(default="run")
    MIGRATIONS_EXPECTED_HEAD: str = Field(default="", description="Pin the expected Alembic head so a drifted schema cannot serve traffic")
    # How long the migration job waits for PostgreSQL to accept connections.
    # Read via os.getenv at backend/utils/migrations.py:281-282 with no central
    # declaration, and set by no compose file or .env — invisible configuration.
    MIGRATION_DB_WAIT_SECONDS: int = Field(
        default=60, description="Seconds to wait for the database before a migration gives up")
    MIGRATION_DB_RETRY_INTERVAL_SECONDS: int = Field(
        default=2, description="Seconds between database connection attempts during migration")

    # =====================================================
    # Database Configuration (PostgreSQL)
    # =====================================================
    DATABASE_URL: str = Field(
        default="postgresql+asyncpg://postgres:admin@postgres:5432/face_recognition",  # Docker: postgres hostname
    )
    DB_HOST: str = Field(default="postgres")
    DB_PORT: int = Field(default=5432)
    POSTGRES_DB: str = Field(default="face_recognition")
    POSTGRES_USER: str = Field(default="postgres")
    POSTGRES_PASSWORD: str = Field(default="admin")
    POSTGRES_PASSWORD_FILE: str = Field(default="")
    DATABASE_URL_FILE: str = Field(default="")
    
    # Connection Pool Settings
    # Database Connection Pool
    # For 50 cameras: Recommended DB_POOL_SIZE=75, DB_MAX_OVERFLOW=150
    # Default values are conservative for smaller deployments
    DB_POOL_SIZE: int = Field(default=50)
    DB_MAX_OVERFLOW: int = Field(default=100)
    DB_POOL_RECYCLE: int = Field(default=3600)
    DB_POOL_PRE_PING: bool = Field(default=True)
    # These four sat as literals in db_connection.py, directly beside the four
    # above that were already settings — so half the pool was configurable and
    # half was not. DB_STATEMENT_TIMEOUT_MS is the 120 s ceiling the
    # intelligence endpoints reason about when choosing their own timeout.
    DB_POOL_TIMEOUT: int = Field(
        default=60,
        description="Seconds to wait for a free pooled connection before failing. Default: 60"
    )
    DB_CONNECT_TIMEOUT: int = Field(
        default=30,
        description="Seconds to wait when opening a new database connection. Default: 30"
    )
    DB_COMMAND_TIMEOUT: int = Field(
        default=120,
        description="Seconds asyncpg waits for a single command to return. Default: 120"
    )
    DB_STATEMENT_TIMEOUT_MS: int = Field(
        default=120000,
        description="PostgreSQL statement_timeout in milliseconds. Default: 120000 (120s)"
    )
    DB_IDLE_TX_TIMEOUT_MS: int = Field(
        default=300000,
        description=("PostgreSQL idle_in_transaction_session_timeout in milliseconds. "
                     "Default: 300000 (5 min)")
    )
    # Host substituted for "postgres" when running OUTSIDE the container
    # (alembic/env.py and backend/utils/migrations.py both detect the absence of
    # /.dockerenv). Previously read straight from os.getenv in two places with
    # no central declaration.
    LOCAL_DB_HOST: str = Field(
        default="localhost",
        description="Database host used when running outside Docker (alembic, CLI migrations)")

    # =====================================================
    # Redis Cache Configuration
    # =====================================================
    REDIS_URL: str = Field(default="redis://redis:6379/0")  # Docker: redis hostname
    REDIS_URL_FILE: str = Field(default="")
    REDIS_MAX_CONNECTIONS: int = Field(default=100)
    REDIS_POOL_SIZE: int = Field(default=50)
    CACHE_TTL: int = Field(default=3600, description="Cache TTL for dashboard data in seconds (default: 3600 = 1 hour)")
    CACHE_TTL_UNKNOWN: int = Field(default=108000, description="Cache TTL for unknown faces page in seconds (default: 108000 = 30 hours)")
    CACHE_LOCAL_SIZE: int = Field(default=50000)
    CACHE_VERSION: str = Field(default="v1")
    CACHE_WARNING_ENABLED: bool = Field(default=True)
    CACHE_WARNING_INTERVAL: int = Field(default=300)
    
    # =====================================================
    # Map Service Configuration
    # =====================================================
    MAP_MAX_COORDINATES: int = Field(default=10000, description="Maximum coordinates per map to prevent memory issues (default: 10000)")

    # --- MapLibre + Martin (offline basemaps) ---------------------------------
    # Root of the map dataset tree. The ONLY settable map path: production/ and
    # metadata/ are derived from it, so a deployment cannot point the archives
    # at one directory and their content ledger at another — a split that would
    # let an unverified archive be authorized by someone else's verdict file.
    # The isolated regression stack sets this to its own fixture tree.
    MAP_DATA_DIR: str = Field(
        default="/app/map-data",
        description="Root of the map dataset tree (production/ and metadata/ are derived from it)")
    # Free space that must REMAIN after a dataset is installed. Installing
    # stages a full copy and retains the archive it replaces, so a dataset
    # costs about twice its size in transit; without a floor a large install
    # can fill the volume out from under PostgreSQL and the logs.
    MAP_INSTALL_DISK_RESERVE_GB: float = Field(
        default=2.0, ge=0.0,
        description="Gigabytes that must remain free after installing a map dataset")
    # Martin's INTERNAL address, used only by the backend for availability
    # deep-checks (catalog + probe tile). Browsers never see it: they reach
    # Martin through nginx at /maps/ on the site origin.
    MAP_MARTIN_INTERNAL_URL: str = Field(
        default="http://martin:3000",
        description="Internal URL of the Martin tile server (backend-only; browsers use /maps/)")
    # How often the cached style-availability verdict is re-derived from
    # Martin's catalog + a representative tile. The deep check is deliberately
    # NOT run per request — /api/maps/availability returns the cached result.
    MAP_AVAILABILITY_REFRESH_SECONDS: int = Field(
        default=300, ge=30,
        description="Seconds between map-dataset availability deep-checks against Martin")
    MAP_DEFAULT_LAT: float = Field(default=33.87, description="Tileset center latitude (Lebanon)")
    MAP_DEFAULT_LON: float = Field(default=35.85, description="Tileset center longitude (Lebanon)")
    MAP_DEFAULT_ZOOM: int = Field(default=10)
    # Panning bounds = the tileset's actual footprint (from the z10 tile
    # numbering). Without these the map lets you pan off the dataset into
    # blank space that looks like a broken map.
    MAP_BOUNDS_SOUTH: float = Field(default=32.84)
    MAP_BOUNDS_WEST: float = Field(default=34.80)
    MAP_BOUNDS_NORTH: float = Field(default=34.89)
    MAP_BOUNDS_EAST: float = Field(default=36.92)
    
    # Animated Map Features
    
    # Co-Appearance Detection

    # =====================================================
    # Face Recognition Models
    # =====================================================
    DETECTION_MODEL: str = Field(default="/app/weights/det_10g.onnx")  # Docker path
    RECOGNITION_MODEL: str = Field(default="/app/weights/w600k_r50.onnx")  # Docker path
    SIMILARITY_THRESHOLD: float = Field(default=0.4)
    UNKNOWN_SIMILARITY_THRESHOLD: float = Field(default=0.35, description="Cosine similarity to match an existing UNKNOWN identity (below SIMILARITY_THRESHOLD creates fewer duplicate unknowns)")

    # --- Performance / concurrency ------------------------------------------
    # Threads dedicated to CPU-bound inference (ONNX + OpenCV release the GIL,
    # so a thread pool gives real parallelism without duplicating model memory).
    INFERENCE_WORKERS: int = Field(default=3)
    # Max frames being inferred simultaneously (global) and per pipeline
    MAX_CONCURRENT_INFERENCE: int = Field(default=3)
    MAX_CONCURRENT_INFERENCE_PER_PIPELINE: int = Field(default=2)
    # Webhook ingress limits
    WEBHOOK_MAX_BODY_MB: int = Field(default=25)
    # Must cover the sender's worst-case same-event retry horizon so a retried
    # event_id still deduplicates. VMS: 6 attempts x (5s connect + 30s read)
    # + 5 waits x max(backoff, Retry-After<=30) = 210 + 150 = 360s worst case;
    # 600 = 360 x ~1.67 safety margin. (Its re-arm path mints a NEW event_id,
    # so longer windows buy nothing.)
    WEBHOOK_DEDUP_TTL_SECONDS: int = Field(default=600)
    # Webhook ingress authentication. The endpoint had none: nginx rate-limits
    # it but does not authenticate it, so anyone who could reach the port could
    # enqueue frames and create identities.
    WEBHOOK_API_KEYS: str = Field(
        default="",
        description="Comma-separated ingest keys. A SET, so a key can be rotated "
                    "by appending the new one, rolling cameras, then dropping the old.")
    WEBHOOK_API_KEYS_FILE: str = Field(
        default="",
        description="Path to a Docker secret holding WEBHOOK_API_KEYS")
    WEBHOOK_AUTH_MODE: str = Field(
        default="enforce",
        description="enforce | log_only | off. log_only exists purely to migrate "
                    "a fleet of already-deployed cameras; production refuses to "
                    "start on anything but enforce unless the risk is acknowledged.")
    WEBHOOK_AUTH_TOKEN: str = Field(
        default="",
        description="Bearer token for external senders that present "
                    "`Authorization: Bearer <token>`. An ALIAS, not a second "
                    "credential store: it is appended to WEBHOOK_API_KEYS during "
                    "Settings construction, so there is exactly one credential set "
                    "at runtime and rotation, redaction and the weak/reused/published "
                    "checks all apply to it unchanged. Consequence: guard violations "
                    "report it positionally under WEBHOOK_API_KEYS, not by this name.")
    WEBHOOK_AUTH_TOKEN_FILE: str = Field(
        default="",
        description="Path to a Docker secret holding WEBHOOK_AUTH_TOKEN")
    WEBHOOK_AUTH_HEADER: str = Field(
        default="X-Webhook-Key",
        description="Header carrying the ingest key. Some camera firmware can only "
                    "send one fixed custom header name. `Authorization: Bearer <key>` "
                    "is ALWAYS accepted regardless of this value.")
    WEBHOOK_CREDENTIAL_CACHE_TTL_SECONDS: int = Field(
        default=30,
        description="How long a worker may serve issued ingest credentials from "
                    "its in-process cache. This IS the revocation latency: "
                    "deleting a credential in the admin UI takes effect on every "
                    "worker within this window. Lower means faster revocation and "
                    "more queries; the query is one indexed SELECT over a table "
                    "with tens of rows. Matches the pipeline-alias cache TTL so "
                    "the ingest path has ONE staleness number, not two.")
    WEBHOOK_AUTH_INSECURE_ACK: bool = Field(
        default=False,
        description="Explicitly acknowledge running the ingest webhook unauthenticated. "
                    "Turns a startup refusal into a logged warning. Same shape as "
                    "VECTOR_INDEX_FALLBACK: a weaker posture must be chosen, not defaulted into.")
    # SQL agent isolation
    SQL_AGENT_MAX_CONCURRENT: int = Field(default=2)
    SQL_AGENT_TOTAL_TIMEOUT: int = Field(default=300)
    SQL_AGENT_MAX_MODEL_CALLS: int = Field(default=24, ge=1, le=100,
        description="Total model attempts per agent run, including retries and fallback")
    SQL_AGENT_MAX_TOOL_CALLS: int = Field(default=12, ge=1, le=50,
        description="Maximum selected tools per agent run")
    SQL_AGENT_MAX_RUN_TOKENS: int = Field(default=65536, ge=1024, le=1000000,
        description="Reported token budget; checked before each subsequent model/tool call")
    SQL_AGENT_MEMORY_RETENTION_DAYS: int = Field(default=30, ge=1, le=365,
        description="Maximum lifetime of explicit user memories and idle conversation context")
    SQL_AGENT_MAX_QUERY_CHARS: int = Field(
        default=8000,
        ge=256,
        le=65536,
        description="Maximum natural-language SQL-agent query length across "
                    "REST, SSE and WebSocket transports.")

    # --- Bounded reasoning (PLAN -> ACT -> OBSERVE -> REPLAN -> ANSWER) ----
    # Three SEPARATE budgets, deliberately. Collapsing them would mean a
    # transient database blip could spend the turn's ability to think, and a
    # confused model could spend the whole budget on look-ups.
    # Defaults are the CONSERVATIVE ones because production does not override
    # them: the prod compose sets no SQL-agent tuning, so whatever is written
    # here is what a real deployment runs — on the local model, where each
    # extra step is seconds of CPU. The dev compose raises them explicitly.
    SQL_AGENT_MAX_REASONING_STEPS: int = Field(
        default=8,
        description="Total reasoning steps per turn (tool look-ups + re-plans). "
                    "The upper bound on how long one turn may think.")
    # How many ACTIONS one turn may take while pursuing the request.
    #
    # 1 (the default) is the single-action behaviour: act, observe, correct at
    # most once, answer. Above 1 the agent may act again after a SUCCESSFUL
    # action when the request is not yet carried out — "track Joey and send it
    # as a PDF" in one turn instead of two.
    #
    # This is a hard ceiling read by the graph's router, so a confused model
    # cannot spend more than this however it behaves. Each extra action costs
    # a full action's latency, so raise it deliberately.
    # A CEILING, not a target. A turn ends the moment it has enough to
    # answer; typical requests still finish in one or two actions. This only
    # makes "track Joey and send it as a PDF" possible in a single turn.
    SQL_AGENT_MAX_ACTIONS_PER_TURN: int = Field(default=3)

    SQL_AGENT_MAX_REPLANS: int = Field(
        default=1,
        description="Corrective re-plans after a failed action. Each needs a "
                    "reason from the Observation; there is no blind retry. "
                    "0 disables re-planning (failures answer honestly).")
    SQL_AGENT_MAX_EXECUTION_RETRIES: int = Field(
        default=1,
        description="Retries of the SAME SQL after a TRANSIENT database error "
                    "(dropped connection, pool timeout). Infrastructure, not "
                    "reasoning: these never consume the re-plan budget.")

    # --- Credentials used to execute LLM-generated SQL ---------------------
    # Deliberately separate from the application's own database role. The AST
    # guard in sql_agent/security/sql_guard.py is application code and can have
    # bugs; a role without write grants cannot be talked out of them by the
    # query text. Defence in depth, not a replacement.
    #
    # Left empty the agent falls back to the application role and logs a
    # warning. In production the config guard rejects that fallback.
    SQL_AGENT_DB_USER: str = Field(default="", description="Read-only role used to execute generated SQL. Must differ from POSTGRES_USER")
    SQL_AGENT_DB_PASSWORD: str = Field(default="", description="Password for SQL_AGENT_DB_USER. Prefer SQL_AGENT_DB_PASSWORD_FILE")
    SQL_AGENT_DB_PASSWORD_FILE: str = Field(default="", description="Path to a Docker secret holding the read-only role's password")

    # --- Identity auto-enrichment: confidently-matched runtime embeddings are added
    # to the identity so it learns the person's appearance range over time.
    # Auto-enrichment writes a RUNTIME observation permanently into an enrolled
    # person. Off by default: the upstream admission bar is SIMILARITY_THRESHOLD
    # (0.4), so one wrong attribution becomes a stored vector, and every later
    # photo of that wrong person then matches the identity at ~1.0. No column
    # distinguishes an auto-learned vector from an operator-enrolled one, so the
    # mistake is invisible afterwards. Turn on only with review in place.
    IDENTITY_AUTO_ENRICH_ENABLED: bool = Field(
        default=False,
        description=("Let recognition add high-confidence runtime embeddings to an "
                     "enrolled identity. Off by default: a single mis-attribution "
                     "becomes permanent and self-reinforcing. Default: false")
    )
    IDENTITY_ENRICH_MIN_SIMILARITY: float = Field(
        default=0.75,
        description=("Similarity an auto-enrichment candidate must clear. Raised to "
                     "match CONFIDENCE_HIGH_MIN: at the old 0.55 this sat only 0.15 "
                     "above the match threshold itself. Default: 0.75")
    )
    IDENTITY_ENRICH_MIN_QUALITY: float = Field(default=0.5)
    IDENTITY_INGEST_TOP_K: int = Field(
        default=5,
        description=("Candidate depth when matching an ingested face against known "
                     "identities. Raising it widens the pool the threshold filters. Default: 5")
    )
    # How similar a new view must already be to an identity's stored views
    # before enrichment treats it as redundant and skips it.
    IDENTITY_NEAR_DUPLICATE_MIN: float = Field(
        default=0.95,
        description=("Similarity at/above which an enrichment candidate is considered "
                     "a duplicate of a view this identity already has. Default: 0.95")
    )
    # IDENTITY_MAX_EMBEDDINGS was removed here: it capped enrichment growth at
    # 20 while MAX_EMBEDDINGS_PER_IDENTITY capped retention pruning at 10, so
    # enrichment grew an identity to 20 views and the nightly job cut it back
    # to 10 — two knobs disagreeing about one quantity. See that field below.

    # How good a match must be before it replaces an identity's displayed face,
    # when no quality score is available to compare. This branch previously
    # triggered on `similarity > 0.0`, i.e. always, so an identity's avatar
    # became whatever arrived last — a CORRECT match could show a different
    # person entirely, because best_snapshot_path feeds every search result card.
    IDENTITY_SNAPSHOT_REPLACE_MIN_SIMILARITY: float = Field(
        default=0.75,
        description=("Similarity required to replace an identity's best snapshot "
                     "when no quality score is available. Default: 0.75")
    )
    CONFIDENCE_THRESHOLD: float = Field(default=0.5)
    
    # =====================================================
    # Identity Index Configuration (FAISS / pgvector)
    # =====================================================
    IDENTITY_EMBEDDING_SIZE: int = Field(default=512)
    IDENTITY_INDEX_DB_PATH: str = Field(default="/app/database/identity_indexes")  # Docker path
    
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
        description="Vector search backend: 'pgvector' (RECOMMENDED for production) or 'faiss' (faster but requires sync logic)"
    )
    VECTOR_INDEX_AUTOSAVE_INTERVAL_SECONDS: float = Field(
        default=900.0,
        description=(
            "Seconds between vector-index snapshots. A 100k flat index takes "
            "~21s and ~201MB to write (measured), so this must be MUCH larger "
            "than one save or the loop saves continuously. Default 900s (15 "
            "min) is ~43x the measured save. Values below 120s are clamped. "
            "Overlapping runs are SKIPPED, never queued — losing a snapshot is "
            "not data loss, since the index rebuilds from PostgreSQL."
        )
    )
    VECTOR_INDEX_RECONCILE_INTERVAL_SECONDS: float = Field(
        default=3600.0,
        description="Seconds between index/PostgreSQL reconciliation passes."
    )
    VECTOR_INDEX_FALLBACK: str = Field(
        default="",
        description=(
            "Explicit opt-in to degrade when VECTOR_BACKEND=faiss cannot start "
            "(faiss missing, or an unimplemented index type). Empty (default) "
            "means FAIL STARTUP — a silent downgrade would serve searches from "
            "a different index than the operator configured. Set to 'pgvector' "
            "to allow degradation; it is logged at CRITICAL and reported as "
            "degraded health."
        )
    )
    
    # pgvector Configuration
    PGVECTOR_INDEX_TYPE: str = Field(
        default="hnsw",
        description="pgvector index type: 'hnsw' (fast, recommended) or 'ivfflat' (memory efficient)"
    )
    PGVECTOR_HNSW_M: int = Field(default=16, description="HNSW M parameter (connections per node, 16-64)")
    PGVECTOR_HNSW_EF_CONSTRUCTION: int = Field(default=100, description="HNSW efConstruction (build-time search width, 64-200, higher = better index quality)")
    PGVECTOR_HNSW_EF_SEARCH: int = Field(default=100, description="HNSW efSearch (search-time accuracy, 20-200, higher = more accurate but slower. RECOMMENDED: 100 for face recognition to match FAISS accuracy)")
    PGVECTOR_IVFFLAT_LISTS: int = Field(default=100, description="IVFFlat lists (clusters, sqrt(N) is good)")
    PGVECTOR_IVFFLAT_PROBES: int = Field(default=10, description="IVFFlat probes (clusters to search, 1-lists)")
    
    # The shipped FAISS implementation is FlatFaissIndex: exact search, one
    # supported index type. The ivf/hnsw/ivfpq knob family that used to sit
    # here configured index types base.py refuses to build, and the REPAIR_*
    # pair drove a repair loop replaced by reconcile — all were advertised as
    # editable in the admin UI while doing nothing (retired 2026-08).
    KNOWN_INDEX_TYPE: str = Field(default="flat", description="FAISS index type; 'flat' (exact) is the only supported value")

    # =====================================================
    # Queue & Processing Configuration
    # =====================================================
    MAX_QUEUE_SIZE: int = Field(default=10000)
    QUEUE_WORKERS: int = Field(default=50)
    BATCH_SIZE: int = Field(default=20)
    MAX_CONCURRENT_REQUESTS: int = Field(default=500)
    GPU_BATCH_SIZE: int = Field(default=32)
    CPU_BATCH_SIZE: int = Field(default=10)
    PIPELINE_BATCH_SIZE: int = Field(default=5)
    

    # =====================================================
    # Storage Configuration
    # =====================================================
    STORAGE_DIR: str = Field(default="/app/storage")  # Docker path
    SAVE_IMAGES: bool = Field(default=True)
    MAX_STORAGE_GB: int = Field(default=500)
    MAX_PHOTOS_PER_PERSON: int = Field(default=1)
    SAVE_UNKNOWN_FACES: bool = Field(default=False)
    MAX_FILE_SIZE: int = Field(default=10485760)  # 10MB in bytes
    
    # Webhook Debug Configuration
    SAVE_WEBHOOK_IMAGES: bool = Field(default=True, description="Save all images received via webhook for debugging")
    
    # Crop Debug Configuration
    SAVE_CROPPED_IMAGES: bool = Field(default=True, description="Save cropped person images for debugging")

    # -----------------------------------------------------------------
    # Operational surfaces observed by /metrics
    #
    # These were read directly from os.environ in backend/core/operational_metrics.py
    # with defaults that existed nowhere else, so the values Prometheus reported
    # could not be configured or even discovered from config.py.
    # -----------------------------------------------------------------
    BACKUP_DIR: str = Field(
        default="/backups", description="Directory the backup job writes to; age is exported as a metric")
    BACKUP_RETENTION_DAYS: int = Field(
        default=14, description="Days of backups to keep (also consumed by scripts/backup/backup.sh)")
    BACKUP_INTERVAL_SECONDS: int = Field(
        default=86400, description="Interval between backup runs (also consumed by backup-loop.sh)")
    TLS_CERT_PATH: str = Field(
        default="/etc/nginx/certs/server.crt",
        description="Certificate whose expiry is exported as a metric")

    # =====================================================
    # CORS Configuration
    # =====================================================
    CORS_ORIGINS: str = Field(default="*")

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

    # =====================================================
    # Derived filesystem layout
    # =====================================================
    # STORAGE_DIR is the ONLY externally settable root. Everything beneath it is
    # computed here and is read-only, so a compose file, a .env, a module or the
    # admin settings API cannot point one of them somewhere else.
    #
    # This is not hypothetical tidiness. `.env` once shipped
    # FACES_DIR=./assets/faces while compose set /app/storage/faces, and the two
    # halves of the application then disagreed about where a person's photos
    # lived. backend/security/config_guard.py rejects any environment that still
    # tries to set these, naming the variable — silently ignoring the value would
    # leave the same confusion with none of the evidence.

    @property
    def FACES_DIR(self) -> str:
        """Enrolled face images: <STORAGE_DIR>/faces/<identity_uuid>/image_NNN.ext"""
        return os.path.join(self.STORAGE_DIR, "faces")

    @property
    def UPLOAD_TEMP_DIR(self) -> str:
        """Staging area for in-flight uploads.

        Deliberately INSIDE faces/ so the final placement is an atomic
        os.replace on the same filesystem rather than a cross-device copy.
        """
        return os.path.join(self.FACES_DIR, ".incoming")

    @property
    def CONVERSATION_CACHE_DIR(self) -> str:
        """Chat working memory: <STORAGE_DIR>/conversation_cache/user_<id>/

        The session JSON files that remember "the last report", the working
        context and the transcript. This used to default to a RELATIVE
        "conversation_cache" resolved against the container's CWD — inside the
        writable layer, with no volume behind it — so every
        `--force-recreate` erased every user's conversational memory, and the
        dev stack hid it because the repo bind-mount made the files look
        persistent. Under STORAGE_DIR the existing storage volume covers it.

        SINGLE-WORKER durability only. A shared filesystem fixes container
        recreation, NOT distributed coordination: the per-file locks in
        conversation_memory.py are process-local threading.Locks. If replicas
        are ever enabled, working memory moves behind Redis/DB or a
        distributed lock must protect these files (see Docs/90 and the
        WORKERS guard in config_guard.py).
        """
        return os.path.join(self.STORAGE_DIR, "conversation_cache")

    @property
    def ARTIFACTS_DIR(self) -> str:
        """Generated agent documents: <STORAGE_DIR>/artifacts/<uuid>.<ext>

        Reports the SQL agent produced (PDF/Word). Never served by the generic
        /storage route — that one authenticates but does not check ownership,
        so a shared root would let any signed-in user read another user's
        query output. Reached only through the artifact id, which resolves to
        a row carrying the owner.
        """
        return os.path.join(self.STORAGE_DIR, "artifacts")

    @property
    def ARTIFACTS_TEMP_DIR(self) -> str:
        """Staging for artifacts being rendered.

        Inside artifacts/ for the same reason UPLOAD_TEMP_DIR is inside
        faces/: the commit step is then an os.replace on one filesystem, so a
        half-written PDF is never visible under its final name.
        """
        return os.path.join(self.ARTIFACTS_DIR, ".incoming")

    @property
    def PENDING_UPLOAD_DIR(self) -> str:
        """Uploads awaiting an administrator's identity decision.

        Deliberately OUTSIDE faces/: the gallery holds one directory per
        identity UUID and nothing else, and an upload parked for review has no
        identity yet. Same filesystem as faces/, so the confirmed photo still
        reaches the gallery by rename rather than a cross-device copy.
        """
        return os.path.join(self.STORAGE_DIR, "pending")

    @property
    def WEBHOOK_IMAGES_DIR(self) -> str:
        """Raw ingested frames, when SAVE_WEBHOOK_IMAGES is on (debug only)."""
        return os.path.join(self.STORAGE_DIR, "debug", "webhook_images")

    @property
    def CROPPED_IMAGES_DIR(self) -> str:
        """Person crops, when SAVE_CROPPED_IMAGES is on (debug only)."""
        return os.path.join(self.STORAGE_DIR, "debug", "cropped")

    @property
    def MODEL_CANDIDATE_DIR(self) -> str:
        """Candidate models awaiting promotion; was hard-coded in the trainer."""
        return os.path.join(self.ML_ARTIFACT_DIR, "candidates")

    @property
    def MAP_PRODUCTION_DIR(self) -> str:
        """The archives Martin serves. Martin mounts this same directory."""
        return os.path.join(self.MAP_DATA_DIR, "production")

    @property
    def MAP_METADATA_DIR(self) -> str:
        """Provenance and verdicts: datasets.json, checksums.txt, the ledger."""
        return os.path.join(self.MAP_DATA_DIR, "metadata")

    @property
    def MAP_CONTENT_LEDGER_PATH(self) -> str:
        """Which archives have had their CONTENT measured and passed.

        Derived rather than settable so the ledger can never be pointed at a
        different tree than the archives it authorizes: a verdict file that
        describes some other deployment's data would authorize bytes nobody
        here has ever inspected.
        """
        return os.path.join(self.MAP_METADATA_DIR, "content_verdicts.json")

    @property
    def is_production(self) -> bool:
        return str(self.ENVIRONMENT).strip().lower() in ("production", "prod")

    # =====================================================
    # Data Retention & Cleanup
    # =====================================================
    DATA_RETENTION_DAYS: int = Field(default=30)
    CLEANUP_INTERVAL_HOURS: int = Field(default=24)

    # =====================================================
    # Batch Processing
    # =====================================================
    BATCH_WRITE_SIZE: int = Field(default=50)
    BATCH_WRITE_INTERVAL: float = Field(default=1.0)
    BATCH_WRITE_MAX_WAIT: float = Field(default=5.0)

    # =====================================================
    # Face Tracking (Optimization)
    # =====================================================
    FACE_TRACKING_ENABLED: bool = Field(default=True)
    FACE_TRACKING_WINDOW_SECONDS: int = Field(default=0)
    FACE_TRACKING_MAX_ENTRIES: int = Field(default=5000)
    FACE_TRACKING_SIMILARITY_THRESHOLD: float = Field(default=0.95)
    SKIP_UNKNOWN_FACES: bool = Field(default=False)
    SHOW_UNKNOWN_FACES_ON_DASHBOARD: bool = Field(default=False, description="If True, unknown faces will appear on the main dashboard. If False (default), unknown faces are only visible in the Unknown Faces Center page.")
    FACE_TRACKING_MAX_MEMORY_MB: int = Field(default=2000)
    FACE_TRACKING_CLEANUP_INTERVAL: int = Field(default=300)
    
    # =====================================================
    # Dashboard Display Settings
    # =====================================================
    # float, not int: the settings registry advertises this as a float with a
    # 0.1 minimum, so the page accepts 2.5 — and an int field then made the
    # WebSocket broadcast throw on int("2.5"), swallowed as "could not
    # broadcast". Sub-hour windows are a legitimate thing to want.
    DASHBOARD_FACE_DISPLAY_HOURS: float = Field(default=3, description="How many hours of face detections to show on dashboard. Default: 3 hours. Faces older than this are hidden from the dashboard but still stored in database.")
    UNKNOWN_FACE_DISPLAY_HOURS: float = Field(default=24, description="How many hours unknown faces stay visible on the Unknown Faces page (display-only — data stays stored until retention deletes it). 0 = show all. A Show-all toggle on the page reveals older ones.")
    ALERT_NOTIFICATION_WINDOW_HOURS: float = Field(default=1.0, description="Hours between alert popups for the same person on the same camera. Default: 1 hour. Set to 0 to alert on every detection (not recommended - noisy).")
    DASHBOARD_CLEANUP_INTERVAL_SECONDS: int = Field(
        default=60,
        description=("How often the dashboard sweeps expired faces from the view. "
                     "Published to the browser as cleanup_interval_ms. Default: 60")
    )

    # =====================================================
    # Identity Management - Clustering & Merge Suggestions
    # =====================================================
    # These settings control the automatic merge suggestion system
    # The clustering job runs periodically to find duplicate identities
    CLUSTER_INTERVAL_HOURS: int = Field(default=24, description="Hours between clustering runs (default: 24)")
    CLUSTER_STARTUP_DELAY_HOURS: float = Field(default=7, description="Hours to wait after startup before first clustering run. Set to 0 for immediate. (default: 0)")
    CLUSTER_MIN_SIZE: int = Field(default=2, description="Minimum cluster size for merge suggestions (default: 2)")
    CLUSTER_EPS: float = Field(default=0.35, description="Epsilon parameter for DBSCAN clustering. Lower = stricter matching. (default: 0.35)")
    CLUSTER_MIN_SAMPLES: int = Field(default=2, description="Minimum samples per cluster for DBSCAN (default: 2)")
    CLUSTER_ACTIVE_WINDOW_DAYS: int = Field(
        default=90,
        description=("How far back an unknown identity must have been seen to be "
                     "considered for clustering. Default: 90")
    )
    CLUSTER_TRAINED_MODEL_MARGIN: float = Field(
        default=0.05,
        description=("How far below the configured similarity threshold a TRAINED "
                     "similarity model is allowed to merge, since it is more "
                     "accurate than the heuristic. Default: 0.05")
    )

    # Pipeline-Aware ML Clustering
    PIPELINE_AWARE_CLUSTERING_ENABLED: bool = Field(default=True, description="Enable pipeline-aware ML clustering for merge suggestions (default: True)")
    SIMILARITY_QUALITY_FLOOR: float = Field(
        default=0.7,
        description=("Fraction of a heuristic similarity score that survives when both "
                     "faces score zero on quality. 1.0 disables the quality penalty. "
                     "Default: 0.7")
    )
    PIPELINE_SIMILARITY_WEIGHT: float = Field(default=0.3, description="Weight for pipeline overlap in similarity calculation (0.0-1.0, default: 0.3)")
    EMBEDDING_SIMILARITY_WEIGHT: float = Field(default=0.7, description="Weight for embedding similarity in calculation (0.0-1.0, default: 0.7)")
    CROSS_PIPELINE_SIMILARITY_THRESHOLD: float = Field(default=0.50, description="Minimum similarity threshold for cross-pipeline matches (default: 0.50)")
    
    # ML Similarity Model Training
    SIMILARITY_MODEL_PATH: str = Field(default="models/similarity_model.pkl", description="Path to save/load the trained similarity model (default: models/similarity_model.pkl)")
    SIMILARITY_MODEL_MIN_SAMPLES: int = Field(default=50, description="Minimum training samples required before model can be trained (default: 50)")
    SIMILARITY_MODEL_AUTO_TRAIN: bool = Field(default=True, description="Automatically train model when enough samples are collected (default: True)")

    # =====================================================
    # Identity Management - Quality Thresholds
    # =====================================================
    IDENTITY_QUALITY_THRESHOLD_KNOWN: float = Field(
        default=0.5,
        description="Minimum quality score (0-1) to save embedding for KNOWN identities. Higher = stricter. Default: 0.5"
    )
    IDENTITY_QUALITY_THRESHOLD_UNKNOWN: float = Field(
        default=0.1,
        description="Minimum quality score (0-1) to save embedding for UNKNOWN identities. Lower = save more. Default: 0.1 (saves almost all)"
    )

    # Dedicated bar for the DESTRUCTIVE merge path, deliberately its own knob
    # rather than a silent reuse of SIMILARITY_THRESHOLD. The default is the
    # same number, and that equality is an argued choice, not an accident: a
    # merge asserts "these are the same person", which is exactly the claim
    # recognition itself makes at SIMILARITY_THRESHOLD — anything recognition
    # would call one person, a merge may combine without ceremony; anything it
    # would not, an administrator must explicitly override. On the enrollment
    # fixtures, two different photos of the same person score 0.4299 and
    # unrelated faces score below 0.05, so 0.40 separates the two cases with
    # real margin. Being a separate setting, hardening merges (e.g. to 0.55)
    # never loosens or tightens live recognition.
    MERGE_WARNING_MIN_SIMILARITY: float = Field(
        default=0.40,
        description=("Robust cross-identity similarity (0-1) below which a merge "
                     "is refused with MERGE_CONFIRMATION_REQUIRED until explicitly "
                     "overridden. Default: 0.40")
    )
    
    # =====================================================
    # Identity Management - Retention
    # =====================================================
    SNAPSHOT_RETENTION_DAYS: int = Field(default=90)
    EMBEDDING_RETENTION_MONTHS: int = Field(default=12)
    INACTIVE_THRESHOLD_DAYS: int = Field(default=180)
    IDENTITY_CLEANUP_INTERVAL_HOURS: int = Field(default=24)
    # Camera-derived vectors only. Enrollment embeddings (one per gallery
    # photo) are never pruned by retention — they are bounded by the image
    # cap and by the administrator who added them.
    MAX_EMBEDDINGS_PER_IDENTITY: int = Field(default=10)

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
        description="Minimum quality score (0-1) to attempt search. Faces below this are skipped. Default: 0.3"
    )
    SEARCH_QUALITY_WARNING_THRESHOLD: float = Field(
        default=0.6,
        description="Quality threshold (0-1) below which a warning is shown. Default: 0.6"
    )
    
    # Confidence Bands
    CONFIDENCE_VERY_HIGH_MIN: float = Field(
        default=0.90,
        description="Minimum similarity for 'Very High' confidence band. Default: 0.90"
    )
    CONFIDENCE_HIGH_MIN: float = Field(
        default=0.75,
        description="Minimum similarity for 'High' confidence band. Default: 0.75"
    )
    CONFIDENCE_MEDIUM_MIN: float = Field(
        default=0.60,
        description="Minimum similarity for 'Medium' confidence band. Default: 0.60"
    )
    CONFIDENCE_LOW_MIN: float = Field(
        default=0.40,
        description="Minimum similarity for 'Low' confidence band. Default: 0.40"
    )

    # Enrollment review bands
    #
    # Enrollment used to mint a new UUID for any unseen name with no similarity
    # check at all, so a second photo of an enrolled person under a new spelling
    # silently became a second identity. These two values decide when the server
    # stops and asks instead. They are NOT clamped to each other at read time:
    # backend/security/config_guard.py refuses to start on an inverted or
    # out-of-range pair, because silently correcting one would mean the operator
    # who typed it never learns the band they configured does not exist.
    ENROLL_STRONG_MATCH_MIN: float = Field(
        default=0.75,
        description=("Similarity (0-1) at/above which an upload is treated as an "
                     "existing person and adding to them is recommended. Must be "
                     ">= ENROLL_CANDIDATE_MIN. Default: 0.75")
    )
    # Deliberately equal to SIMILARITY_THRESHOLD, the bar at which recognition
    # itself calls two faces the same person. Anything recognition would
    # confuse, enrollment must ask about — a floor ABOVE it reopens the exact
    # defect this flow exists to close, because the duplicate identity gets
    # created silently and recognition then reports either name at random.
    # Measured on the enrollment fixtures: two different photos of the same
    # person score 0.4299, while unrelated faces score below 0.05. A floor of
    # 0.45 would have missed the headline case by 0.02.
    ENROLL_CANDIDATE_MIN: float = Field(
        default=0.40,
        description=("Similarity (0-1) floor for offering a candidate at all. "
                     "Below this an upload enrolls directly with no review. "
                     "Must be <= ENROLL_STRONG_MATCH_MIN, and should not exceed "
                     "SIMILARITY_THRESHOLD. Default: 0.40")
    )
    ENROLL_CANDIDATE_POOL: int = Field(
        default=25,
        description=("How many nearest embeddings to retrieve before collapsing "
                     "to one row per identity. Must exceed "
                     "ENROLL_MAX_CANDIDATES, since several embeddings of the "
                     "same person collapse to a single candidate. Default: 25")
    )
    ENROLL_MAX_CANDIDATES: int = Field(
        default=5,
        description=("How many candidate identities to show the administrator. "
                     "Default: 5")
    )

    # =====================================================
    # API pagination
    # =====================================================
    # One default and one ceiling for every listing endpoint. There was no
    # page-size field anywhere: ~40 routes carried their own literals, and four
    # groups (/admin/unknown, detections, conversations, cache-warm) had no
    # upper bound at all, so a client could ask for any page size it liked.
    API_DEFAULT_PAGE_SIZE: int = Field(
        default=25,
        description="Rows per page when a listing endpoint's caller does not specify. Default: 25"
    )
    API_MAX_PAGE_SIZE: int = Field(
        default=100,
        description=("Hard ceiling on rows per page; larger requests are rejected. "
                     "A few export endpoints declare a higher bound explicitly. Default: 100")
    )

    # Result depth
    #
    # ONE default for every search surface. These used to be route-local
    # literals: /api/search/advanced defaulted to 10 while the batch service
    # defaulted to 5, and the search page drives both from a single control —
    # so a batch silently ran at half the depth the operator asked for.
    SEARCH_DEFAULT_TOP_K: int = Field(
        default=10,
        description="Matches returned per query face when the caller does not specify. Default: 10"
    )
    SEARCH_MAX_TOP_K: int = Field(
        default=100,
        description="Hard ceiling on matches per query face; requests above this are rejected. Default: 100"
    )

    # Retrieval floor vs display floor. SEARCH_RETRIEVAL_FLOOR is how deep the
    # vector index is asked to look; SIMILARITY_THRESHOLD is what an operator
    # is allowed to see. Retrieval must stay BELOW display or raising the
    # display threshold would silently shrink the candidate pool it filters.
    # config_guard refuses to start if that ordering is inverted.
    SEARCH_RETRIEVAL_FLOOR: float = Field(
        default=0.2,
        description=("Similarity floor used when asking the vector index for "
                     "candidates, before the display threshold filters them. "
                     "Must be <= SIMILARITY_THRESHOLD. Default: 0.2")
    )
    SEARCH_CANDIDATE_MULTIPLIER: int = Field(
        default=2,
        description=("Over-fetch factor for an unfiltered search: the index is asked "
                     "for top_k x this, so post-filtering still fills the page. Default: 2")
    )
    SEARCH_FILTERED_CANDIDATE_MULTIPLIER: int = Field(
        default=6,
        description=("Over-fetch factor when database filters (camera, date, type) will "
                     "discard candidates after retrieval. Default: 6")
    )

    # Batch Search
    BATCH_SEARCH_MAX_IMAGES: int = Field(
        default=20,
        description="Maximum number of images allowed per batch search. Default: 20"
    )
    BATCH_SEARCH_TIMEOUT_SECONDS: int = Field(
        default=300,
        description="Timeout in seconds for batch search operations. Default: 300 (5 minutes)"
    )
    BATCH_SEARCH_MAX_CONCURRENCY: int = Field(
        default=5,
        description=("Maximum images processed concurrently within one batch search. "
                     "Bounded by the database pool. Default: 5")
    )

    # Search History
    SEARCH_HISTORY_RETENTION_DAYS: int = Field(
        default=90,
        description="Days to retain search history records. Default: 90"
    )
    SEARCH_HISTORY_MAX_PER_USER: int = Field(
        default=1000,
        description="Maximum search history records per user. Default: 1000"
    )

    # Audit log retention (chatbot/identity/settings audit tables)
    AUDIT_LOG_RETENTION_DAYS: int = Field(
        default=180,
        description="Days to retain audit-log records (chatbot, identity, settings). Default: 180"
    )
    TASK_HISTORY_RETENTION_DAYS: int = Field(
        default=30,
        description="Days to retain background-task history records. Default: 30"
    )
    
    # Live Alerts
    LIVE_ALERT_DEFAULT_COOLDOWN_MINUTES: int = Field(
        default=30,
        description="Default cooldown period (minutes) between live alert triggers. Default: 30"
    )
    LIVE_ALERT_MAX_PER_USER: int = Field(
        default=50,
        description="Maximum number of active live alerts per user. Default: 50"
    )
    LIVE_ALERT_MAX_PER_IDENTITY: int = Field(
        default=5,
        description="Maximum number of active live alerts that can be created for the same identity. Default: 5"
    )
    LIVE_ALERT_MIN_SIMILARITY: float = Field(
        default=0.75,
        description=("Default similarity an appearance must reach to trigger a live "
                     "alert, when the alert does not set its own. Default: 0.75")
    )
    LIVE_ALERT_CLIP_DURATION_SECONDS: int = Field(
        default=60,
        description="Default length of the clip recorded when a live alert fires. Default: 60"
    )

    # Notification transports. Declared (empty = not configured) so the
    # readiness probe on the live-alerts page can ever report true; it used to
    # read them with getattr(cfg, "SMTP_HOST", None) against fields that
    # existed nowhere, so smtp_ready/sms_ready were permanently False.
    SMTP_HOST: str = Field(
        default="",
        description="SMTP server hostname for email alerts. Empty disables email notification."
    )
    SMS_PROVIDER_URL: str = Field(
        default="",
        description="HTTP endpoint of the SMS provider. Empty disables SMS notification."
    )
    TWILIO_ACCOUNT_SID: str = Field(
        default="",
        description="Twilio account SID for SMS alerts. Empty disables Twilio."
    )

    # Intelligence endpoints
    INTEL_QUERY_TIMEOUT_SECONDS: float = Field(
        default=30.0,
        description=("Seconds a heavy intelligence analysis may run before it is "
                     "abandoned with a 503. Must stay below DB_STATEMENT_TIMEOUT_MS "
                     "or the database kills the query first. Default: 30")
    )

    # Related Identities
    RELATED_IDENTITY_MIN_CO_APPEARANCES: int = Field(
        default=3,
        description="Minimum co-appearances required to establish related identity relationship. Default: 3"
    )
    RELATED_IDENTITY_TIME_WINDOW_MINUTES: int = Field(
        default=30,
        description="Time window (minutes) for considering identities as co-appearing. Default: 30"
    )
    
    # Multi-Camera Social Network Analysis
    MULTI_CAMERA_CO_APPEARANCE_ENABLED: bool = Field(
        default=True,
        description="Enable cross-camera co-appearance detection for social network analysis (default: True)"
    )
    MULTI_CAMERA_DISTANCE_METERS: float = Field(
        default=500.0,
        description="Maximum distance in meters between cameras to consider cross-camera co-appearance (default: 500)"
    )
    MULTI_CAMERA_TIME_WINDOW_MINUTES: int = Field(
        default=10,
        description="Time window in minutes for cross-camera co-appearance detection (default: 10, larger than same-camera)"
    )
    MULTI_CAMERA_MIN_CO_APPEARANCES: int = Field(
        default=2,
        description="Minimum cross-camera co-appearances to establish relationship (default: 2, lower than same-camera)"
    )
    
    # Advanced Features
    AUTO_THRESHOLD_LEARNING_ENABLED: bool = Field(
        default=True,
        description="Enable automatic learning of optimal thresholds per camera pair (default: True)"
    )
    TRAJECTORY_PREDICTION_ENABLED: bool = Field(
        default=True,
        description="Enable trajectory prediction for proactive relationship detection (default: True)"
    )
    ACTIVITY_CORRELATION_ENABLED: bool = Field(
        default=True,
        description="Enable activity correlation analysis (xCCA) — temporal association evidence between identities (default: True)"
    )

    # Security Intelligence tunables (read per call via getattr — live-applyable)
    ANOMALY_DEVIATION_SIGMA: float = Field(
        default=2.0,
        description="Circular-hour deviations beyond this many effective standard deviations flag an off-schedule anomaly. Default: 2.0"
    )
    ANOMALY_MIN_BASELINE_SAMPLES: int = Field(
        default=5,
        description="Minimum baseline appearances before anomaly detection reports results instead of 'insufficient baseline'. Default: 5"
    )
    ANOMALY_MIN_STD_HOURS: float = Field(
        default=0.75,
        description="Floor (hours) on the baseline circular std so a metronomic history cannot flag minute-level jitter. Default: 0.75"
    )
    ANOMALY_MAX_ITEMS: int = Field(
        default=200,
        description="Maximum anomaly items returned per request. Default: 200"
    )
    PATTERN_SCAN_LIMIT: int = Field(
        default=50000,
        description="Maximum appearance rows scanned per pattern-detection request (newest first; response flags truncation). Default: 50000"
    )
    PATTERN_MAX_PER_TYPE: int = Field(
        default=100,
        description="Maximum patterns returned per detector type. Default: 100"
    )
    PATTERN_OFF_HOURS_START: int = Field(
        default=2,
        description="Start hour (0-23, SITE-LOCAL time as stored in appearance timestamps) of the unusual-timing window. Default: 2"
    )
    PATTERN_OFF_HOURS_END: int = Field(
        default=5,
        description="End hour (0-23, INCLUSIVE whole hour — 2/5 covers 02:00-05:59; may wrap midnight, e.g. 22-5) of the unusual-timing window. Default: 5"
    )
    PATTERN_RAPID_WINDOW_SECONDS: int = Field(
        default=300,
        description="Maximum seconds between two different-camera appearances for rapid-movement analysis. Default: 300"
    )
    PATTERN_RAPID_MIN_SPEED_KMH: float = Field(
        default=15.0,
        description="Implied speed (km/h, camera coordinates permitting) above which a transit is flagged as rapid movement. Default: 15"
    )
    NETWORK_FALLBACK_MAX_IDENTITIES: int = Field(
        default=200,
        description="Identity cap (most recently seen first) for the cold-path network build when the relationship cache is empty. Default: 200"
    )

    # ML pipeline (first release: RULES is the production decision system;
    # anomaly models cap at administrator-approved SHADOW)
    ML_WORKER_ID: str = Field(
        default="",
        description="Stable identity for the ML worker, so a container replacement updates one heartbeat row instead of creating a new one. Empty means derive it from hostname and pid. Default: \"\""
    )
    ML_DECISION_MODE: str = Field(
        default="rules",
        description="Decision mode: rules (default, production-safe) | shadow (after an approved anomaly model) | hybrid/ml (GATED this release — activation returns the unmet gates). Default: rules"
    )
    ML_INFERENCE_TIMEOUT_MS: int = Field(
        default=1500,
        description="Hard timeout for one model inference; on timeout the decision falls back to rules with a recorded reason. Default: 1500"
    )
    ML_SHADOW_TIMEOUT_MS: int = Field(
        default=3000,
        description="Bound on the entire shadow evaluation (features + inference + persistence); shadow never delays the live response beyond this. Default: 3000"
    )
    ML_MODEL_CACHE_TTL_SECONDS: int = Field(
        default=60,
        description="Process-local model cache revalidation interval when Redis version keys are unavailable. Default: 60"
    )
    ML_SUPERVISED_MIN_LABELS: int = Field(
        default=100,
        description="Reviewed manual labels required before supervised training may run (trainer returns a structured refusal below this). Default: 100"
    )
    ML_SUPERVISED_MIN_PER_CLASS: int = Field(
        default=25,
        description="Reviewed labels required per class for supervised training. Default: 25"
    )
    ML_COLLECTOR_LATE_GRACE_MINUTES: int = Field(
        default=120,
        description="Late-data window re-scanned on each collection run (snapshot uniqueness makes reprocessing idempotent). Default: 120"
    )
    ML_FEATURE_SAMPLED_FULL_VECTOR_RATE: float = Field(
        default=0.0,
        description="Fraction of predictions storing the FULL feature vector for audit sampling. Default 0.0 (DISABLED) — enabling requires a documented privacy/retention justification."
    )
    ML_GRAPH_MIN_NODES: int = Field(
        default=25,
        description="Graph features are unavailable (with reason) below this node count. Default: 25"
    )
    ML_GRAPH_MIN_EDGES: int = Field(
        default=50,
        description="Graph features are unavailable (with reason) below this edge count. Default: 50"
    )
    ML_GRAPH_MIN_OBSERVATION_DAYS: int = Field(
        default=14,
        description="Graph features require at least this observation span. Default: 14"
    )
    ML_GRAPH_MIN_PAIR_APPEARANCES: int = Field(
        default=3,
        description="Pair features require at least this many co-appearances. Default: 3"
    )
    ML_DRIFT_CHECK_INTERVAL_HOURS: int = Field(
        default=24,
        description="Cadence of the report-only scheduled drift check. Default: 24"
    )
    ML_JOB_POLL_SECONDS: float = Field(
        default=2.0, ge=0.2, le=60.0,
        description="Idle polling interval for the durable ML worker"
    )
    ML_JOB_LEASE_SECONDS: int = Field(
        default=60, ge=20, le=3600,
        description="Worker lease duration, renewed while a child job is alive"
    )
    ML_JOB_HEARTBEAT_SECONDS: float = Field(
        default=10.0, ge=1.0, le=300.0,
        description="How often the ML worker renews a running job lease"
    )
    ML_JOB_MAINTENANCE_SECONDS: float = Field(
        default=30.0, ge=5.0, le=3600.0,
        description="Cadence for stale-lease cleanup and scheduled-job checks"
    )
    ML_JOB_TERMINATE_GRACE_SECONDS: float = Field(
        default=15.0, ge=1.0, le=300.0,
        description="Grace period before a cancelled ML child process is killed"
    )
    ML_DRIFT_MIN_SAMPLES: int = Field(
        default=200,
        description="Below this sample count drift reports state insufficient_data instead of a verdict. Default: 200"
    )
    ML_DRIFT_PSI_WARNING: float = Field(
        default=0.1,
        description="PSI at or above this marks a drift report WARNING. Default: 0.1"
    )
    ML_DRIFT_PSI_CRITICAL: float = Field(
        default=0.25,
        description="PSI at or above this marks a drift report CRITICAL. Default: 0.25"
    )
    ML_PREDICTION_RETENTION_DAYS: int = Field(
        default=180,
        description="Retention for ml_predictions and shadow comparisons. Default: 180"
    )
    ML_SNAPSHOT_RETENTION_DAYS: int = Field(
        default=365,
        description="Retention for ml_feature_snapshots. Default: 365"
    )
    ML_DRIFT_REPORT_RETENTION_DAYS: int = Field(
        default=365,
        description="Retention for ml_drift_reports. Default: 365"
    )
    ML_ARTIFACT_DIR: str = Field(
        default="models/ml",
        description="Approved internal directory for ML artifacts; loads outside this prefix are refused. Default: models/ml"
    )
    # Scientific-readiness minimums — deliberately UNSET (0) by default: the
    # repository holds no validated policy for them. The readiness report and
    # the shadow evidence report expose the statistics; a verdict other than
    # INSUFFICIENT_EVIDENCE / NOT_CONFIGURED needs an administrator to set
    # these from reviewed policy. 0 = not configured.
    ML_SCIENTIFIC_MIN_HISTORY_DAYS: int = Field(
        default=0, description="Scientific gate: minimum history span in days (0 = not configured)")
    ML_SCIENTIFIC_MIN_MEDIAN_APPEARANCES: int = Field(
        default=0, description="Scientific gate: minimum median appearances per entity (0 = not configured)")
    ML_EVIDENCE_MIN_REVIEWED_TOTAL: int = Field(
        default=0, description="Evidence adequacy: minimum reviewed manual outcomes overall (0 = not configured)")
    ML_EVIDENCE_MIN_REVIEWED_PER_BAND: int = Field(
        default=0, description="Evidence adequacy: minimum reviewed manual outcomes per anomaly band (0 = not configured)")
    # Separation of duties for evidence-grade outcomes. OFF by default: today a
    # self-reviewed label (creator == reviewer) still counts and is REPORTED as
    # its own population. Turning this on makes review_label refuse to confirm
    # a label by the person who created it (SELF_REVIEW_REFUSED). A policy
    # decision - not set here.
    ML_EVIDENCE_REQUIRE_INDEPENDENT_REVIEW: bool = Field(
        default=False,
        description="Refuse confirming a label by its own creator (separation of duties); default off")
    ML_MAX_ARTIFACT_MB: int = Field(
        default=200,
        description="Maximum artifact size accepted at registration/load. Default: 200"
    )
    # Optional integrations — flags only; a flag being true does NOT mean the
    # dependency is installed or operational (four distinct statuses are
    # reported: configured / implemented / dependency available / operational).
    MLFLOW_ENABLED: bool = Field(
        default=True,
        description="Track experiments and mirror governed model versions to MLflow. Default: True"
    )
    OPTUNA_ENABLED: bool = Field(
        default=False,
        description="Optional Optuna hyperparameter tuning (requires the optuna package). Default: False"
    )
    XGBOOST_ENABLED: bool = Field(
        default=True,
        description="XGBoost tabular classification and regression (requires the xgboost package). Default: True"
    )
    SHAP_ENABLED: bool = Field(
        default=False,
        description="Optional SHAP explanations (requires the shap package; native importances are the fallback). Default: False"
    )
    MLFLOW_TRACKING_URI: str = Field(default="", description="Empty uses a durable SQL-backed MLflow store under ML_ARTIFACT_DIR; alternatively an administrator-managed HTTPS tracking service. Credentials belong in service environment, never this field.")
    MLFLOW_EXPERIMENT_NAME: str = Field(default="ml-platform", description="MLflow experiment for governed training runs")
    ML_TRAIN_MAX_THREADS: int = Field(default=2, ge=1, le=32)
    ML_OPTUNA_MAX_TRIALS: int = Field(default=30, ge=1, le=200)
    ML_OPTUNA_TIMEOUT_SECONDS: int = Field(default=600, ge=10, le=7200)
    ML_SHAP_MAX_ROWS: int = Field(default=100, ge=1, le=1000)
    ML_SHAP_BACKGROUND_ROWS: int = Field(default=50, ge=1, le=200)
    ML_DRIFT_MONITORING_ENABLED: bool = Field(default=False, description="Deferred production drift scheduling; requires real production inference samples")

    # Risk platform: timezones, anomaly context, assessments, thresholds
    DEFAULT_SITE_TIMEZONE: str = Field(
        default="UTC",
        description="IANA timezone used for business-hour evaluation when a pipeline has no timezone configured (e.g. 'Asia/Beirut'). Timestamps are ALWAYS stored in UTC; this only affects local-time interpretation. Default: UTC"
    )
    WEEKEND_DAYS: str = Field(
        default="5,6",
        description="Comma-separated weekday numbers treated as weekend (Monday=0). Default: '5,6' (Saturday, Sunday)"
    )
    ANOMALY_HOLIDAYS: str = Field(
        default="",
        description="Comma-separated ISO dates (site-local) treated as holidays by anomaly detection, e.g. '2026-12-25,2027-01-01'. Default: none"
    )
    ANOMALY_BASELINE_MAX_DAYS: int = Field(
        default=365,
        description="Rolling anomaly-baseline horizon in days — behavior older than this ages out of the baseline. Default: 365"
    )
    ASSESSMENT_DEDUP_WINDOW_MINUTES: int = Field(
        default=5,
        description="Threat assessments for the same subject within this window collapse onto one persisted row (idempotency). Default: 5"
    )
    THRESHOLD_MIN_SAMPLES_FOR_ACTIVATION: int = Field(
        default=10,
        description="Learned-threshold candidates below this sample count are refused activation. Default: 10"
    )
    API_RATE_LIMIT_ENABLED: bool = Field(
        default=True,
        description="Enable per-user + per-IP rate limiting on sensitive/expensive endpoints. Default: True. (The legacy RATE_LIMIT_ENABLED webhook knob it replaced was removed 2026-08.)"
    )
    RATE_LIMIT_DEFAULT_PER_MINUTE: int = Field(
        default=300,
        description="Per-minute request budget per user AND per IP for standard limited scopes. Default: 300"
    )
    RATE_LIMIT_HEAVY_PER_MINUTE: int = Field(
        default=60,
        description="Per-minute budget for heavy scopes (recalculation, graph/anomaly analysis, exports). Default: 60"
    )

    # Feature Flags
    FACE_QUALITY_ENABLED: bool = Field(
        default=True,
        description="Enable face quality scoring. Default: True"
    )
    FACE_QUALITY_SCORER: str = Field(
        default="full",
        description="full | legacy. 'full' measures blur, lighting, face pixel "
                    "resolution and pose from the real SCRFD face crop. 'legacy' "
                    "restores the old size+confidence score, which on the camera "
                    "path could not exceed 0.5. A no-redeploy rollback if the new "
                    "scorer ever misjudges a deployment's imagery."
    )
    WATCHLIST_ENABLED: bool = Field(
        default=True,
        description="Enable watchlist functionality. Default: True"
    )
    LIVE_ALERTS_ENABLED: bool = Field(
        default=True,
        description="Enable live search alerts. Default: True"
    )
    RELATED_IDENTITIES_ENABLED: bool = Field(
        default=True,
        description="Enable related identities analysis. Default: True"
    )
    TEMPORAL_PATTERNS_ENABLED: bool = Field(
        default=True,
        description="Enable temporal pattern analysis. Default: True"
    )
    CROSS_CAMERA_TRACKING_ENABLED: bool = Field(
        default=True,
        description="Enable cross-camera tracking. Default: True"
    )
    BATCH_SEARCH_ENABLED: bool = Field(
        default=True,
        description="Enable batch search functionality. Default: True"
    )
    EXPORT_RESULTS_ENABLED: bool = Field(
        default=True,
        description="Enable search results export (CSV, PDF, JSON). Default: True"
    )
    NEGATIVE_SEARCH_ENABLED: bool = Field(
        default=True,
        description="Enable negative search (exclude specific identities). Default: True"
    )
    
    # Face Quality Thresholds (for quality scoring)
    FACE_QUALITY_THRESHOLD_BLUR: float = Field(
        default=0.5,
        description="Blur threshold for face quality assessment. Default: 0.5"
    )
    # Defaults chosen to match the behaviour these fields now drive, so wiring
    # them up changed no scores. They were declared, described, and offered on
    # the settings page while face_quality.py used its own literals.
    FACE_QUALITY_THRESHOLD_SIZE: int = Field(
        default=50,
        description="Smallest face (shorter side, pixels) still worth scoring. Default: 50"
    )
    FACE_QUALITY_THRESHOLD_ANGLE: float = Field(
        default=30.0,
        description="Maximum face angle (degrees) for quality assessment. Default: 30.0"
    )
    FACE_QUALITY_THRESHOLD_LIGHTING: float = Field(
        default=0.5,
        description="Lighting score (0-1) below which a quality warning is raised. Default: 0.5"
    )

    # =====================================================
    # Ollama Configuration (for SQL Agent)
    # =====================================================
    OLLAMA_BASE_URL: str = Field(
        default="http://ollama:11434"
    )
    OLLAMA_MODEL: str = Field(
        default="llama3.2:3b"
    )
    # Specialist model for SQL generation steps only (falls back to OLLAMA_MODEL when empty).
    # Lets a small fast model handle chat/intent while a text-to-SQL model writes queries.
    OLLAMA_SQL_MODEL: str = Field(
        default=""
    )
    OLLAMA_TEMPERATURE: float = Field(
        default=0.1
    )
    OLLAMA_TIMEOUT: int = Field(
        default=120
    )

    # =====================================================
    # Development-only hosted LLM (NVIDIA NIM) for the SQL Agent
    # =====================================================
    # Lets a developer point SQL generation at build.nvidia.com's free
    # OpenAI-compatible endpoint to iterate on prompts and judge query quality
    # against a stronger model than the local Ollama ones.
    #
    # DEVELOPMENT ONLY, fail-closed. This system queries biometric data, and a
    # hosted endpoint means the schema and every question a user asks leave
    # the box. Three independent layers keep it out of production:
    #   1. the model registry refuses to register the provider when
    #      settings.is_production (sql_agent/llm/registry.py),
    #   2. the config guard fails a production boot (exit 78) when
    #      LLM_DEV_PROVIDER is set — no acknowledgement escape,
    #   3. LLM_DEV_PROVIDER is SECURITY_CRITICAL, so it cannot be persisted
    #      through the admin settings API and applied at a later boot.
    LLM_DEV_PROVIDER: str = Field(
        default="",
        description="Empty (default) = local Ollama only. 'nim' enables the "
                    "NVIDIA NIM development provider — refused in production.")
    NVIDIA_NIM_BASE_URL: str = Field(
        default="https://integrate.api.nvidia.com/v1",
        description="OpenAI-compatible base URL of the NIM endpoint")
    NVIDIA_NIM_API_KEY: str = Field(
        default="",
        description="API key from build.nvidia.com (free tier). Never logged; "
                    "redacted everywhere a setting is rendered.")
    NVIDIA_NIM_MODEL: str = Field(
        default="meta/llama-3.2-11b-vision-instruct",
        description="NIM model for chat/intent tasks")
    # Specialist for SQL generation only (falls back to NVIDIA_NIM_MODEL when
    # empty) — mirrors the OLLAMA_MODEL / OLLAMA_SQL_MODEL split.
    NVIDIA_NIM_SQL_MODEL: str = Field(
        default="openai/gpt-oss-120b"
    )
    NVIDIA_NIM_TIMEOUT: int = Field(
        default=60,
        description="Per-attempt timeout in seconds for NIM calls")

    # =====================================================
    # Development-only LLM tracing (Opik) for the SQL Agent
    # =====================================================
    # Records one trace per agent turn — every node, every model call with
    # its full prompt and answer, every tool proposal, timings — into a
    # self-hosted Opik instance, and lets Claude Code read them back through
    # the Opik MCP server (.mcp.json). See sql_agent/tracing.py and
    # Docs/90_AGENT_ARCHITECTURE.md "Tracing a turn with Opik".
    #
    # DEVELOPMENT ONLY, fail-closed, the same three layers as LLM_DEV_PROVIDER:
    #   1. sql_agent/tracing.py attaches no tracer when settings.is_production,
    #   2. the config guard fails a production boot (exit 78) when
    #      SQL_AGENT_OPIK_ENABLED is on — no acknowledgement escape,
    #   3. the keys are SECURITY_CRITICAL, so they cannot be persisted through
    #      the admin settings API and applied at a later boot.
    # A trace contains the user's words, the names of people under
    # surveillance, the SQL and its rows: the very content the audit rules
    # keep out of logs. The hosted service (comet.com) is refused everywhere;
    # only a self-hosted Opik is accepted, and the `opik` package itself is a
    # development extra (requirements-dev.txt) that production images never
    # carry.
    SQL_AGENT_OPIK_ENABLED: bool = Field(
        default=False,
        description="Attach an Opik tracer to every SQL-agent turn. "
                    "Development only — refused in production.")
    OPIK_URL_OVERRIDE: str = Field(
        default="http://host.docker.internal:5173/api/",
        description="API URL of a SELF-HOSTED Opik. The container reaches the "
                    "workstation's instance through host.docker.internal. "
                    "comet.com is refused.")
    OPIK_API_KEY: str = Field(
        default="",
        description="Only for an authenticated self-hosted Opik; the "
                    "open-source instance needs none. Never logged; redacted "
                    "wherever a setting is rendered.")
    OPIK_WORKSPACE: str = Field(
        default="default",
        description="Open-source Opik has exactly one workspace, 'default'.")
    OPIK_PROJECT_NAME: str = Field(
        default="face-detector-sql-agent",
        description="Opik project the agent's traces are filed under.")

    # =====================================================
    # SQL Agent Configuration
    # =====================================================
    CHROMADB_PATH: str = Field(
        default="./sql_agent/chromadb_data"
    )
    CHROMA_COLLECTION_NAME: str = Field(
        default="sql_knowledge_base"
    )
    RAG_TOP_K: int = Field(
        default=5
    )
    RAG_SIMILARITY_THRESHOLD: float = Field(
        default=0.3
    )

    # =====================================================
    # File Upload Configuration
    # =====================================================
    ALLOWED_IMAGE_EXTENSIONS: str = Field(
        default=".jpg,.jpeg,.png,.webp"
    )
    
    @property
    def allowed_image_extensions_list(self) -> List[str]:
        """Parse ALLOWED_IMAGE_EXTENSIONS from comma-separated string"""
        return [ext.strip() for ext in self.ALLOWED_IMAGE_EXTENSIONS.split(",")]

    @model_validator(mode="after")
    def _resolve_secret_files(self):
        """Let Docker secret files override inline values.

        Substitution only — this never raises a policy error, because config.py
        is imported by alembic/env.py, gunicorn.conf.py and every test, and a
        raising validator would turn all of them into import-time failures.
        Policy lives in backend/security/config_guard.py.

        This must run inside __init__ rather than at first use, because
        backend/auth/auth_service.py caches settings.JWT_SECRET_KEY at import.
        """
        from backend.security.secrets import resolve_secret

        for field, file_field in (
            ("JWT_SECRET_KEY", "JWT_SECRET_KEY_FILE"),
            ("POSTGRES_PASSWORD", "POSTGRES_PASSWORD_FILE"),
            ("DATABASE_URL", "DATABASE_URL_FILE"),
            ("REDIS_URL", "REDIS_URL_FILE"),
            ("BOOTSTRAP_ADMIN_PASSWORD", "BOOTSTRAP_ADMIN_PASSWORD_FILE"),
            ("WEBHOOK_API_KEYS", "WEBHOOK_API_KEYS_FILE"),
            ("WEBHOOK_AUTH_TOKEN", "WEBHOOK_AUTH_TOKEN_FILE"),
            # sql_agent/config.py used to open this file itself, so a mounted
            # secret was read by two different mechanisms with two different
            # failure modes. One resolver, one behaviour; the guard reports an
            # unreadable file at startup instead of the agent discovering it on
            # the first question a user asks.
            ("SQL_AGENT_DB_PASSWORD", "SQL_AGENT_DB_PASSWORD_FILE"),
        ):
            path = getattr(self, file_field, "") or ""
            if not path:
                continue
            resolved = resolve_secret(None, path, name=field)
            if resolved:
                object.__setattr__(self, field, resolved)

        # Fold WEBHOOK_AUTH_TOKEN into the ingest key SET.
        #
        # After this line there is exactly ONE credential source. That matters
        # for more than tidiness: `utils/logging.py` harvests literal secret
        # values from WEBHOOK_API_KEYS to redact them wherever they appear, and
        # its field-name patterns do not match "WEBHOOK_AUTH_TOKEN" (the \b
        # before TOKEN sits between two word characters). A token kept in its own
        # field would be written to the log in full. Folding also means
        # verify_key, the weak/reused/published checks and the rotation story all
        # apply to it with no extra code.
        #
        # AFTER the file loop on purpose, so a token supplied by Docker secret is
        # folded too, not just an inline one.
        token = (getattr(self, "WEBHOOK_AUTH_TOKEN", "") or "").strip()
        if token:
            existing = (getattr(self, "WEBHOOK_API_KEYS", "") or "").strip()
            # parse_keys() strips and deduplicates, so a token equal to a key
            # already present is a no-op rather than a duplicate entry.
            merged = f"{existing},{token}" if existing else token
            object.__setattr__(self, "WEBHOOK_API_KEYS", merged)
        return self

    # Pydantic v2 settings source configuration.
    #
    # This replaced a v1-style inner `class Config`. Under v2 that class was
    # still honoured for `env_file`/`case_sensitive`, but every
    # `Field(...)` kwarg alongside it was silently IGNORED — v2
    # spells that `validation_alias`. Binding therefore worked only because
    # each field name happened to equal its variable name, and a single rename
    # would have detached a setting from its environment variable with no error.
    # The `env=` kwargs are gone; binding is by field name, and
    # tests/test_config_contract.py proves every field still binds.
    #
    # `extra="ignore"`: a .env file legitimately carries keys this model does
    # not own (compose-interpolation credentials such as FR_APP_PASSWORD,
    # POSTGRES_SUPERUSER_PASSWORD, GRAFANA_ADMIN_PASSWORD). Forbidding extras
    # would turn an ordinary deployment file into an import-time crash.
    model_config = SettingsConfigDict(
        env_file=os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"),
        env_file_encoding="utf-8",
        case_sensitive=True,
        extra="ignore",
    )


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
    from backend.security.redaction import redact_url
    print(f"  Redis URL: {redact_url(settings.REDIS_URL)}")
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

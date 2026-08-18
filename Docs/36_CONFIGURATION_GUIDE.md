# Complete Configuration Guide

> **Vector backend note.** Where this document says *FAISS*, the live
> system uses **PostgreSQL + pgvector**. PostgreSQL is authoritative and
> the index is a disposable acceleration layer — see
> [`70_VECTOR_INDEX_CONTRACT.md`](70_VECTOR_INDEX_CONTRACT.md). The
> surrounding explanation of *what* the index does is still accurate.

**Face Recognition Surveillance System**  
**ITDIR-AI DEPARTMENT**

---

## 📋 Table of Contents

1. [Overview](#overview)
2. [Configuration Methods](#configuration-methods)
3. [Configuration Categories](#configuration-categories)
4. [Environment Variables](#environment-variables)
5. [Settings Management Interface](#settings-management-interface)
6. [Common Configuration Scenarios](#common-configuration-scenarios)
7. [Troubleshooting](#troubleshooting)

---

## Overview

The Face Recognition System uses a centralized configuration system that allows you to control every aspect of the system's behavior. All settings are defined in `config.py` with sensible defaults, and can be overridden through:

1. **Environment Variables** (`.env` file or Docker environment)
2. **Web Interface** (Admin Settings page)
3. **Direct Code Modification** (for advanced users)

### Configuration Priority

Settings are loaded in this order (highest to lowest priority):

1. **Environment Variables** (Docker/compose) — highest priority
2. **`.env` File** — if it exists in the project root
3. **`config.py` Defaults** — the declared value

An admin change made on the Settings page sits **above** all three. It is
stored in the database and pushed onto the live settings object; it does **not**
become an environment variable, and it is re-applied at startup
(`hydrate_from_db`) so it survives a restart. What "applied" means for each
setting — immediately, at the next job run, or at the next restart — is in
[78_SETTINGS_RUNTIME_MATRIX.md](78_SETTINGS_RUNTIME_MATRIX.md).

Startup hydration applies **every** stored value, whatever its apply mode.
A setting labelled "requires an API restart" really does take effect on the next
restart; it previously did not, because hydration skipped every non-dynamic key
and the restart re-read env/defaults instead. The two exceptions are deliberate:
security-critical keys are never applied in-process, and `container_recreate`
keys describe how the container was launched, so applying them would make the
reported value disagree with reality.

### `config.py` is the only interface

Nothing else declares configuration. No module calls `os.getenv` for a setting,
none supplies its own default via `getattr(settings, "X", fallback)`, and
nothing writes to `os.environ`. This is enforced by
`tests/test_config_single_source.py`, which scans the source rather than the
behaviour — the failures it prevents are silent at runtime.

Three further shapes count as a second declaration and are equally forbidden:

* **A literal that overrides the setting.** `min(settings.X, 10)` means the
  operator can raise X to anything and still be capped at 10, with nothing on
  the page to explain why.
* **A default argument.** `def search_known(..., threshold: float = 0.4)` wins
  for every caller that omits the argument, so the setting governs only the
  call sites that happen to pass it. Use `= None` and resolve from `settings`
  in the body.
* **A value captured at import.** `self.x = settings.X` inside the `__init__`
  of a module-level singleton runs exactly once. Use a property.
  `tests/test_runtime_editability.py` treats such an `__init__` as frozen.

Values are **refused, never clamped**. An out-of-range or inverted pair fails
validation (or startup, via `backend/security/config_guard.py`) with a message
naming the field. Silently correcting it would mean the operator who typed it
never learns that the configuration they wrote does not exist.

`tests/test_settings_change_behavior.py` closes the loop from the other side:
it changes each setting through the real runtime path and asserts the real
consumer's behaviour moved.

That matters because the alternative was not theoretical. `config.py` said the
vector backend was `pgvector` while fourteen call sites defaulted it to
`faiss`, three of them frozen at import; two maintenance scripts built their
DSN from raw `os.getenv` with the password defaulting to the literal `admin`,
so on any deployment using Docker secrets they connected as a different user
than the service they were maintaining; and `utils/performance_config.py`
wrote fifteen values into `os.environ` *after* the settings object had been
built, where nothing could ever read them.

The few files allowed to read the environment are listed in that test with the
reason for each — a Docker-secret reader that cannot import `config` without a
cycle, GPU/driver probes, the logging bootstrap (it must work when `config.py`
is the thing that is broken), and the settings API's own "what did the
container start with?" introspection.

---

## Configuration Methods

### Method 1: Environment Variables (Recommended)

Create a `.env` file in the project root:

```bash
# .env file
ENVIRONMENT=production
DEBUG=false
PORT=8000
WORKERS=8
```

**Advantages:**
- ✅ Easy to manage
- ✅ Works with Docker
- ✅ Can be version controlled (without sensitive values)
- ✅ No code changes needed

### Method 2: Web Interface (User-Friendly)

Access: **Admin → Settings**

**Advantages:**
- ✅ No technical knowledge required
- ✅ Visual interface
- ✅ Change tracking (audit log)
- ✅ Validation and error messages

**How to Use:**
1. Navigate to `/admin/settings`
2. Find the setting you want to change
3. Click "Edit"
4. Enter new value
5. Add reason (optional)
6. Click "Save"

### Method 3: Docker Compose

For Docker deployments, you can set environment variables in `docker-compose.yml`:

```yaml
environment:
  DATABASE_URL: postgresql+asyncpg://postgres:admin@postgres:5432/face_recognition
  REDIS_URL: redis://redis:6379/0
```

**Note:** Only essential variables that need container hostnames should be in docker-compose. All other settings use defaults from `config.py`.

---

## Configuration Categories

The system organizes settings into logical categories:

### 1. Server Configuration

Controls basic server behavior:

| Setting | Default | Description |
|---------|---------|-------------|
| `ENVIRONMENT` | `production` | Environment mode (production, development, testing) |
| `DEBUG` | `false` | Enable debug mode (shows detailed errors) |
| `HOST` | `0.0.0.0` | Server host address |
| `PORT` | `8000` | Server port number |
| `WORKERS` | `4` | Number of worker processes (1.5x CPU cores recommended) |
| `USE_GPU` | `false` | Enable GPU acceleration |

**Example:**
```bash
# For production with 8 CPU cores
WORKERS=12
ENVIRONMENT=production
DEBUG=false
```

### 2. Logging Configuration

Controls log file location and retention:

| Setting | Default | Description |
|---------|---------|-------------|
| `LOG_DIR` | `/var/log/face-recognition` | Directory for log files |
| `LOG_LEVEL` | `INFO` | Logging level (DEBUG, INFO, WARNING, ERROR, CRITICAL) |
| `LOGS_LIFE_TIME_HOURS` | `48` | How long to keep log files (hours) |

**Example:**
```bash
LOG_LEVEL=DEBUG
LOGS_LIFE_TIME_HOURS=168  # 7 days
```

### 3. Security & Authentication

Controls authentication and security:

| Setting | Default | Description |
|---------|---------|-------------|
| `JWT_SECRET_KEY` | `your-secret-key...` | Secret key for JWT tokens (CHANGE IN PRODUCTION!) |
| `JWT_ALGORITHM` | `HS256` | JWT algorithm |
| `ACCESS_TOKEN_EXPIRE_MINUTES` | `1440` | Token expiration time (24 hours) |

**⚠️ Security Warning:**
- Always change `JWT_SECRET_KEY` in production!
- Use a strong, random secret key
- Never commit secrets to version control

### 4. Database Configuration

Controls PostgreSQL database connection:

| Setting | Default | Description |
|---------|---------|-------------|
| `DATABASE_URL` | `postgresql+asyncpg://...` | Full database connection URL |
| `DB_HOST` | `postgres` | Database host (use `postgres` for Docker) |
| `DB_PORT` | `5432` | Database port |
| `POSTGRES_DB` | `face_recognition` | Database name |
| `POSTGRES_USER` | `postgres` | Database user |
| `POSTGRES_PASSWORD` | `admin` | Database password |
| `DB_POOL_SIZE` | `50` | Connection pool size |
| `DB_MAX_OVERFLOW` | `100` | Maximum overflow connections |
| `DB_POOL_TIMEOUT` | `60` | Seconds to wait for a free pooled connection |
| `DB_CONNECT_TIMEOUT` | `30` | Seconds to wait when opening a new connection |
| `DB_COMMAND_TIMEOUT` | `120` | Seconds asyncpg waits for one command |
| `DB_STATEMENT_TIMEOUT_MS` | `120000` | PostgreSQL `statement_timeout` (ms) |
| `DB_IDLE_TX_TIMEOUT_MS` | `300000` | PostgreSQL `idle_in_transaction_session_timeout` (ms) |

> The last five used to be literals in `db_connection.py`, sitting directly
> beside the pool settings above — half the pool was configurable and half was
> not. `DB_STATEMENT_TIMEOUT_MS` is the ceiling that
> `INTEL_QUERY_TIMEOUT_SECONDS` must stay below.

**For 50+ Cameras:**
```bash
DB_POOL_SIZE=75
DB_MAX_OVERFLOW=150
```

### 5. Redis Cache Configuration

Controls Redis caching:

| Setting | Default | Description |
|---------|---------|-------------|
| `REDIS_URL` | `redis://redis:6379/0` | Redis connection URL (use `redis` for Docker) |
| `REDIS_MAX_CONNECTIONS` | `100` | Maximum Redis connections |
| `CACHE_TTL` | `3600` | Cache time-to-live (seconds) |

### 6. Face Recognition Models

Controls AI model behavior:

| Setting | Default | Description |
|---------|---------|-------------|
| `DETECTION_MODEL` | `/app/weights/det_10g.onnx` | Path to face detection model |
| `RECOGNITION_MODEL` | `/app/weights/w600k_r50.onnx` | Path to face recognition model |
| `SIMILARITY_THRESHOLD` | `0.4` | Minimum similarity to match faces (0.0-1.0) |
| `CONFIDENCE_THRESHOLD` | `0.5` | Minimum confidence for face detection (0.0-1.0) |
| `FACES_DIR` | `<STORAGE_DIR>/faces` | **Derived, not settable.** Known face images |

**Tuning Similarity Threshold:**
- **Lower (0.3-0.4)**: More matches, but may have false positives
- **Higher (0.5-0.6)**: Fewer matches, but more accurate
- **Recommended**: Start with 0.4, adjust based on results

#### Enrollment review bands

When an uploaded photo is enrolled **by name** and that name is new, the face is
compared against everyone already on file before an identity is created. These
decide what happens next.

| Setting | Default | Description |
|---------|---------|-------------|
| `ENROLL_STRONG_MATCH_MIN` | `0.75` | At/above this, adding to the matched person is the recommended action |
| `ENROLL_CANDIDATE_MIN` | `0.40` | Below this, the upload enrolls directly with no review |
| `ENROLL_CANDIDATE_POOL` | `25` | Nearest embeddings retrieved before collapsing to one row per person |
| `ENROLL_MAX_CANDIDATES` | `5` | Candidate people shown to the administrator |

`ENROLL_CANDIDATE_MIN` defaults to exactly `SIMILARITY_THRESHOLD`: anything
recognition would treat as the same person, enrollment must ask about. Raising
it above `SIMILARITY_THRESHOLD` creates a range where recognition says "same
person" and enrollment stays silent — the duplicate identity is created without
a prompt, and recognition then reports either name for that face. Startup
reports that configuration (`ENROLL_CANDIDATE_MIN_ABOVE_RECOGNITION`).

Measured on the enrollment fixtures: two different photos of one person score
**0.4299**; unrelated faces score below **0.05**.

Two relationships abort startup rather than being silently corrected —
`ENROLL_CANDIDATE_MIN <= ENROLL_STRONG_MATCH_MIN` (otherwise the "uncertain"
band is empty and nothing is ever sent for review) and `ENROLL_CANDIDATE_POOL >
ENROLL_MAX_CANDIDATES` (otherwise one person's several photos can fill the pool
and hide every other candidate).

### 7. Vector Index Configuration

Controls the similarity-search index:

| Setting | Default | Description |
|---------|---------|-------------|
| `VECTOR_BACKEND` | `pgvector` | `pgvector` (search in PostgreSQL) or `faiss` (in-memory exact index) |
| `IDENTITY_EMBEDDING_SIZE` | `512` | Size of face embedding vectors |
| `IDENTITY_INDEX_DB_PATH` | `/app/database/identity_indexes` | FAISS snapshot directory (faiss backend only) |
| `KNOWN_INDEX_TYPE` | `flat` | FAISS index type; `flat` (exact) is the only supported value |

The shipped FAISS implementation (`FlatFaissIndex`) is an exact, disposable
acceleration layer rebuilt entirely from `identity_embeddings.embedding`. The
retired knob family — `REPAIR_FAISS_*`, `KNOWN_INDEX_{NLIST,NPROBE,HNSW_*,PQ_*}`,
`UNKNOWN_INDEX_TYPE`, `FAISS_LAZY_MARKING_THRESHOLD`,
`IDENTITY_INDEX_AUTO_SAVE_INTERVAL`, `DB_PATH` — was removed 2026-08: those
settings configured index types the code refuses to build, drove a repair loop
replaced by reconciliation, or pointed at the deleted display-name FaceDatabase.
Setting any of them in the environment has no effect. Autosave/reconcile cadence
lives in `VECTOR_INDEX_AUTOSAVE_INTERVAL_SECONDS` /
`VECTOR_INDEX_RECONCILE_INTERVAL_SECONDS`.

**See:** `70_VECTOR_INDEX_CONTRACT.md` for the authoritative contract

### 8. Queue & Processing Configuration

Controls image processing queue:

| Setting | Default | Description |
|---------|---------|-------------|
| `MAX_QUEUE_SIZE` | `10000` | Maximum queue size |
| `QUEUE_WORKERS` | `50` | Number of queue workers |
| `MAX_CONCURRENT_REQUESTS` | `500` | Maximum concurrent requests |
| `PIPELINE_BATCH_SIZE` | `5` | Batch size per pipeline |

**For 50+ Cameras:**
```bash
QUEUE_WORKERS=100
MAX_QUEUE_SIZE=50000
PIPELINE_BATCH_SIZE=20
```

### 9. Storage Configuration

Controls image storage:

| Setting | Default | Description |
|---------|---------|-------------|
| `STORAGE_DIR` | `/app/storage` | Directory for storing images |
| `SAVE_IMAGES` | `true` | Enable image saving |
| `MAX_STORAGE_GB` | `500` (compose sets `5000`) | **Reporting-only soft budget.** Enforces nothing — no code deletes, blocks or rejects on it. It is the denominator of `storage.app_usage_percent` ("how much of my allowance am I using"). Real capacity comes from the filesystem: `storage.disk_total_gb` / `disk_free_gb`, and `storage.usage_percent` is the volume's true utilisation. |
| `MAX_PHOTOS_PER_PERSON` | `1` | Maximum photos per person |
| `SAVE_UNKNOWN_FACES` | `false` | Save unknown face images |
| `MAX_FILE_SIZE` | `10485760` | Maximum file size (10MB) |

**Debug Settings:**
```bash
SAVE_WEBHOOK_IMAGES=true          # -> <STORAGE_DIR>/debug/webhook_images
SAVE_CROPPED_IMAGES=true          # -> <STORAGE_DIR>/debug/cropped
```

The destinations are derived from `STORAGE_DIR`; only the switches are
settings. Setting `WEBHOOK_IMAGES_DIR` or `CROPPED_IMAGES_DIR` in the
environment aborts startup -- see *Derived paths* below.

### 10. Face Tracking Configuration

Controls face tracking optimization:

| Setting | Default | Description |
|---------|---------|-------------|
| `FACE_TRACKING_ENABLED` | `true` | Enable face tracking |
| `FACE_TRACKING_WINDOW_SECONDS` | `0` | Tracking window (0 = disabled) |
| `FACE_TRACKING_MAX_ENTRIES` | `5000` | Maximum tracking entries |
| `FACE_TRACKING_SIMILARITY_THRESHOLD` | `0.95` | Tracking similarity threshold |
| `SKIP_UNKNOWN_FACES` | `false` | Skip processing unknown faces |
| `SHOW_UNKNOWN_FACES_ON_DASHBOARD` | `false` | Show unknown faces on dashboard |
| `FACE_TRACKING_MAX_MEMORY_MB` | `2000` | Maximum memory for tracking (MB) |

**Dashboard Visibility:**
- `SHOW_UNKNOWN_FACES_ON_DASHBOARD=false`: Unknown faces only in "Unknown Faces Center"
- `SHOW_UNKNOWN_FACES_ON_DASHBOARD=true`: Unknown faces appear on main dashboard

### 11. Identity Management Configuration

**Clustering & Merge Suggestions:**

| Setting | Default | Description |
|---------|---------|-------------|
| `CLUSTER_STARTUP_DELAY_HOURS` | `7` | Hours to wait after startup before first clustering (0 = immediate). **Float** — `0.5` is valid; it used to be rejected as an integer. |
| `CLUSTER_INTERVAL_HOURS` | `24` | How often to generate merge suggestions |
| `CLUSTER_MIN_SIZE` | `2` | Minimum cluster size |
| `CLUSTER_EPS` | `0.35` | Clustering epsilon (similarity threshold) |
| `CLUSTER_MIN_SAMPLES` | `2` | Minimum samples per cluster |
| `CLUSTER_ACTIVE_WINDOW_DAYS` | `90` | How far back an unknown identity must have been seen to be clustered |
| `CLUSTER_TRAINED_MODEL_MARGIN` | `0.05` | How far below the configured threshold a **trained** similarity model may merge. Derived, so raising the threshold raises this too — it used to be two independent literals (0.30/0.45) that ignored the settings entirely. |
| `SIMILARITY_QUALITY_FLOOR` | `0.7` | Fraction of a heuristic similarity score that survives when both faces score zero on quality. `1.0` disables the penalty. |

> The cross-camera/cross-pipeline merge bar is `CROSS_PIPELINE_SIMILARITY_THRESHOLD`
> and the same-camera bar is `UNKNOWN_SIMILARITY_THRESHOLD`. Three clustering
> sites previously hard-coded `0.50`/`0.35` and never consulted either.

**Pipeline-Aware ML Clustering (NEW):**

Advanced merge suggestion system that uses machine learning and pipeline filtering:

| Setting | Default | Description | Unit |
|---------|---------|-------------|------|
| `PIPELINE_AWARE_CLUSTERING_ENABLED` | `true` | Enable pipeline-aware ML clustering for merge suggestions | bool |
| `PIPELINE_SIMILARITY_WEIGHT` | `0.3` | Weight for pipeline overlap in similarity calculation (0.0-1.0) | ratio |
| `EMBEDDING_SIMILARITY_WEIGHT` | `0.7` | Weight for embedding similarity in calculation (0.0-1.0) | ratio |
| `CROSS_PIPELINE_SIMILARITY_THRESHOLD` | `0.50` | Minimum similarity threshold for cross-pipeline matches | ratio |

**ML Similarity Model Training (NEW):**

Trainable neural network that learns from user feedback to improve merge suggestion accuracy:

| Setting | Default | Description | Unit |
|---------|---------|-------------|------|
| `SIMILARITY_MODEL_PATH` | `models/similarity_model.pkl` | Path to save/load the trained ML similarity model | path |
| `SIMILARITY_MODEL_MIN_SAMPLES` | `50` | Minimum training samples required before model can be trained | samples |
| `SIMILARITY_MODEL_AUTO_TRAIN` | `true` | Automatically train model when enough samples are collected | bool |

**How ML Model Works:**
1. System automatically collects training data when you approve/reject merge suggestions
2. After 50+ samples, train the model via API: `POST /api/admin/merge-suggestions/train-model`
3. Model learns patterns from your feedback using neural network (MLP)
4. Improves merge suggestion accuracy over time

**Cross-Camera Detection (NEW):**
The merge suggestion system now supports cross-camera detection:
- **Same-camera matches**: Fast pattern-based filtering + FAISS verification (threshold: 0.35)
- **Cross-camera matches**: FAISS-only with higher threshold (0.50) for accuracy
- Cross-camera suggestions are marked in the API response with `is_cross_camera: true`
- Cross-camera suggestions have lower confidence scores (max 0.86) and require careful review

**Identity Retention:**

| Setting | Default | Description |
|---------|---------|-------------|
| `SNAPSHOT_RETENTION_DAYS` | `90` | How long to keep snapshots |
| `EMBEDDING_RETENTION_MONTHS` | `12` | How long to keep embeddings |
| `INACTIVE_THRESHOLD_DAYS` | `180` | Days before marking identity as inactive |
| `IDENTITY_CLEANUP_INTERVAL_HOURS` | `24` | Cleanup interval |
| `MAX_EMBEDDINGS_PER_IDENTITY` | `10` | Maximum embeddings per identity |

**Production Merge Features (No Config Needed):**
The following production-grade features work automatically:
- **Merge Preview API**: Preview what will happen before merging (`POST /api/admin/identities/merge-preview`)
- **AI Target Selection**: Automatic selection using type (KNOWN=5000pts), appearances (1000pts), pipeline diversity (200pts)
- **Type Promotion**: UNKNOWN + KNOWN → KNOWN automatically
- **Best Snapshot Selection**: Highest quality image selected automatically
- **Pipeline Preservation**: All `pipeline_id` values preserved during merge

### 12. Advanced Search Configuration

Controls advanced search features including quality scoring, confidence bands, watchlists, live alerts, and intelligence features:

#### Quality Scoring

| Setting | Default | Description | Unit |
|---------|---------|-------------|------|
| `SEARCH_MIN_QUALITY_THRESHOLD` | `0.3` | Minimum quality score (0-1) to attempt search. Faces below this are skipped | 0-1 |
| `SEARCH_QUALITY_WARNING_THRESHOLD` | `0.6` | Quality threshold (0-1) below which a warning is shown | 0-1 |
| `FACE_QUALITY_ENABLED` | `true` | Enable face quality scoring | bool |
| `FACE_QUALITY_THRESHOLD_BLUR` | `0.5` | Blur threshold for quality assessment | 0-1 |
| `FACE_QUALITY_THRESHOLD_SIZE` | `50` | Smallest face (shorter side, px) still worth scoring | pixels |
| `FACE_QUALITY_THRESHOLD_ANGLE` | `30.0` | Maximum face angle (degrees) for quality assessment | degrees |
| `FACE_QUALITY_THRESHOLD_LIGHTING` | `0.5` | Lighting score below which a quality warning is raised | 0-1 |

#### Confidence Bands

| Setting | Default | Description | Unit |
|---------|---------|-------------|------|
| `CONFIDENCE_VERY_HIGH_MIN` | `0.90` | Minimum similarity for 'Very High' confidence band | 0-1 |
| `CONFIDENCE_HIGH_MIN` | `0.75` | Minimum similarity for 'High' confidence band | 0-1 |
| `CONFIDENCE_MEDIUM_MIN` | `0.60` | Minimum similarity for 'Medium' confidence band | 0-1 |
| `CONFIDENCE_LOW_MIN` | `0.40` | Minimum similarity for 'Low' confidence band | 0-1 |

#### Batch Search

| Setting | Default | Description | Unit |
|---------|---------|-------------|------|
| `BATCH_SEARCH_MAX_IMAGES` | `20` | Maximum number of images allowed per batch search | count |
| `BATCH_SEARCH_TIMEOUT_SECONDS` | `300` | Timeout for a batch search. **Enforced** — the batch aborts with `TimeoutError`. | seconds |
| `BATCH_SEARCH_MAX_CONCURRENCY` | `5` | Images processed concurrently within one batch (also bounded by `DB_POOL_SIZE`) | count |
| `BATCH_SEARCH_ENABLED` | `true` | Enable batch search. When `false`, `/api/search/batch` returns **403**. | bool |

#### Result Depth

One default for every search surface. Single and batch search previously had
their own literals (10 and 5), so a batch driven from the same UI control ran at
half the requested depth.

| Setting | Default | Description | Unit |
|---------|---------|-------------|------|
| `SEARCH_DEFAULT_TOP_K` | `10` | Matches per query face when the caller does not specify | count |
| `SEARCH_MAX_TOP_K` | `100` | Hard ceiling; a larger `top_k` is rejected with **422** | count |
| `SEARCH_RETRIEVAL_FLOOR` | `0.2` | Similarity floor used when asking the vector index for candidates, before the display threshold filters them. Must be **≤ `SIMILARITY_THRESHOLD`** or startup is refused. | 0-1 |
| `SEARCH_CANDIDATE_MULTIPLIER` | `2` | Over-fetch factor for an unfiltered search | factor |
| `SEARCH_FILTERED_CANDIDATE_MULTIPLIER` | `6` | Over-fetch factor when database filters will discard candidates after retrieval | factor |

**Retrieval floor vs display threshold.** `SEARCH_RETRIEVAL_FLOOR` decides how
deep the index is searched; `SIMILARITY_THRESHOLD` decides what an operator is
allowed to see. Retrieval must sit at or below display — inverted, nothing
between the two bars is ever retrieved, so lowering `SIMILARITY_THRESHOLD` would
appear to do nothing. `backend/security/config_guard.py` refuses to start rather
than clamping either value.

#### API Pagination

| Setting | Default | Description | Unit |
|---------|---------|-------------|------|
| `API_DEFAULT_PAGE_SIZE` | `25` | Rows per page when a listing endpoint's caller does not specify | rows |
| `API_MAX_PAGE_SIZE` | `100` | Hard ceiling; a larger `page_size`/`limit` is rejected with **422** | rows |

Applied through `backend/utils/pagination.py`. Four route groups previously
accepted any page size at all: `/api/identities/admin/unknown`, `/api/detections`,
`/conversations` and `/api/cache/warm/{pipeline_id}`. A few export endpoints
declare a deliberately higher bound explicitly.

#### Search History

| Setting | Default | Description | Unit |
|---------|---------|-------------|------|
| `SEARCH_HISTORY_RETENTION_DAYS` | `90` | Days to retain search history records | days |
| `SEARCH_HISTORY_MAX_PER_USER` | `1000` | Maximum search history records per user. **Enforced** by the retention sweep (previously a no-op). | count |

#### Live Alerts

| Setting | Default | Description | Unit |
|---------|---------|-------------|------|
| `LIVE_ALERT_DEFAULT_COOLDOWN_MINUTES` | `30` | Default cooldown period (minutes) between live alert triggers. Prevents alert spam when same person detected multiple times | minutes |
| `LIVE_ALERT_MAX_PER_USER` | `50` | Maximum number of active live alerts per user. Prevents resource exhaustion | count |
| `LIVE_ALERT_MAX_PER_IDENTITY` | `5` | Maximum active alerts for one identity. Previously clamped to 10 by a literal in the service, so raising this past 10 did nothing. | count |
| `LIVE_ALERT_MIN_SIMILARITY` | `0.75` | Default similarity an appearance must reach to trigger an alert, when the alert does not set its own | 0-1 |
| `LIVE_ALERT_CLIP_DURATION_SECONDS` | `60` | Default length of the clip recorded when an alert fires | seconds |
| `LIVE_ALERTS_ENABLED` | `true` | Enable/disable live search alerts feature globally | bool |

**Notification transports** — empty disables the channel. These are read by the
readiness probe on the live-alerts page, which reported both channels as
unconfigured forever because the names were declared nowhere:

| Setting | Default | Description |
|---------|---------|-------------|
| `SMTP_HOST` | *(empty)* | SMTP server hostname for email alerts |
| `SMS_PROVIDER_URL` | *(empty)* | HTTP endpoint of the SMS provider |
| `TWILIO_ACCOUNT_SID` | *(empty)* | Twilio account SID for SMS alerts |

> `SMTP_PORT` was **removed** — there is no SMTP sender in the codebase, so a
> port for it was a knob wired to nothing.

**Live Alert Configuration Details:**

- **Cooldown Period**: Time between alerts for the same person. Example: If set to 30 minutes and person detected at 10:00 AM, next alert only triggers after 10:30 AM (if detected again).
- **User Limits**: Each user can create up to `LIVE_ALERT_MAX_PER_USER` active alerts. When limit reached, user must delete or expire old alerts before creating new ones.
- **Feature Flag**: When `LIVE_ALERTS_ENABLED=false`, all live alert endpoints return 503 (Service Unavailable).

**See Also:**
- **Live Alerts Guide**: `40_LIVE_ALERTS_GUIDE.md` - Complete guide to creating and managing live alerts

#### Related Identities

| Setting | Default | Description | Unit |
|---------|---------|-------------|------|
| `RELATED_IDENTITY_MIN_CO_APPEARANCES` | `3` | Minimum co-appearances required to establish related identity relationship | count |
| `RELATED_IDENTITY_TIME_WINDOW_MINUTES` | `30` | Time window (minutes) for considering identities as co-appearing | minutes |
| `RELATED_IDENTITIES_ENABLED` | `true` | Enable related identities analysis | bool |

#### Feature Flags

| Setting | Default | Description | Unit |
|---------|---------|-------------|------|
| `WATCHLIST_ENABLED` | `true` | Enable watchlist functionality | bool |
| `TEMPORAL_PATTERNS_ENABLED` | `true` | Enable temporal pattern analysis | bool |
| `CROSS_CAMERA_TRACKING_ENABLED` | `true` | Enable cross-camera tracking | bool |
| `EXPORT_RESULTS_ENABLED` | `true` | Enable search results export (CSV, PDF, JSON) | bool |
| `NEGATIVE_SEARCH_ENABLED` | `true` | Enable negative search (exclude specific identities) | bool |

**Example Configuration:**
```bash
# Quality thresholds
SEARCH_MIN_QUALITY_THRESHOLD=0.3
SEARCH_QUALITY_WARNING_THRESHOLD=0.6
FACE_QUALITY_ENABLED=true

# Confidence bands
CONFIDENCE_VERY_HIGH_MIN=0.90
CONFIDENCE_HIGH_MIN=0.75
CONFIDENCE_MEDIUM_MIN=0.60
CONFIDENCE_LOW_MIN=0.40

# Batch search
BATCH_SEARCH_MAX_IMAGES=20
BATCH_SEARCH_TIMEOUT_SECONDS=300

# Search history
SEARCH_HISTORY_RETENTION_DAYS=90
SEARCH_HISTORY_MAX_PER_USER=1000

# Live alerts
LIVE_ALERT_DEFAULT_COOLDOWN_MINUTES=30
LIVE_ALERT_MAX_PER_USER=50

# Related identities
RELATED_IDENTITY_MIN_CO_APPEARANCES=3
RELATED_IDENTITY_TIME_WINDOW_MINUTES=30

# Feature flags
WATCHLIST_ENABLED=true
LIVE_ALERTS_ENABLED=true
RELATED_IDENTITIES_ENABLED=true
TEMPORAL_PATTERNS_ENABLED=true
CROSS_CAMERA_TRACKING_ENABLED=true
BATCH_SEARCH_ENABLED=true
EXPORT_RESULTS_ENABLED=true
NEGATIVE_SEARCH_ENABLED=true
```

**See Also:**
- **Advanced Search Intelligence Guide**: `39_ADVANCED_SEARCH_INTELLIGENCE_GUIDE.md` - Complete guide to all advanced search features
- **Search by Image Guide**: `38_SEARCH_BY_IMAGE_GUIDE.md` - Quick search functionality

### 13. Data Retention Configuration

Controls automatic data cleanup:

| Setting | Default | Description |
|---------|---------|-------------|
| `DATA_RETENTION_DAYS` | `30` | Days to keep detection data |
| `CLEANUP_INTERVAL_HOURS` | `24` | Cleanup job interval |
| `BATCH_WRITE_SIZE` | `50` | Batch write size |
| `BATCH_WRITE_INTERVAL` | `1.0` | Batch write interval (seconds) |

---

## Environment Variables

### Creating `.env` File

1. Copy `.env.example` to `.env` (if available)
2. Or create new `.env` file in project root
3. Add your configuration:

```bash
# .env
ENVIRONMENT=production
PORT=8000
WORKERS=8
SIMILARITY_THRESHOLD=0.4
```

### Docker Environment Variables

For Docker, you can set environment variables in `docker-compose.yml`:

```yaml
environment:
  DATABASE_URL: postgresql+asyncpg://postgres:admin@postgres:5432/face_recognition
  REDIS_URL: redis://redis:6379/0
```

**Note:** Only essential variables (DATABASE_URL, REDIS_URL) should be in docker-compose. All other settings use defaults from `config.py`.

### Path Differences: Docker vs Local

**Docker (default paths):**
```bash
DETECTION_MODEL=/app/weights/det_10g.onnx
STORAGE_DIR=/app/storage
LOG_DIR=/var/log/face-recognition
```

**Local Development:**
```bash
DETECTION_MODEL=./weights/det_10g.onnx
STORAGE_DIR=/absolute/path/to/storage
LOG_DIR=./logs
```

`STORAGE_DIR` must be absolute. A relative root makes every stored path
depend on the working directory, which differs between gunicorn, alembic
and a maintenance script -- so the preflight rejects it.

### Derived paths

These are computed from `STORAGE_DIR` and have no setter. A compose file,
`.env`, module or admin API cannot point them elsewhere:

| Derived | Resolves to |
|---------|-------------|
| `FACES_DIR` | `<STORAGE_DIR>/faces` |
| `UPLOAD_TEMP_DIR` | `<STORAGE_DIR>/faces/.incoming` |
| `PENDING_UPLOAD_DIR` | `<STORAGE_DIR>/pending` |
| `WEBHOOK_IMAGES_DIR` | `<STORAGE_DIR>/debug/webhook_images` |
| `CROPPED_IMAGES_DIR` | `<STORAGE_DIR>/debug/cropped` |
| `MODEL_CANDIDATE_DIR` | `<ML_ARTIFACT_DIR>/candidates` |

Leaving one of these in the environment is reported rather than ignored:
a matching value is a warning (inert -- it will not track a `STORAGE_DIR`
change), a divergent value aborts startup with exit code 78. Silently
ignoring it is the worse outcome: `.env` once shipped
`FACES_DIR=./assets/faces` while compose set `/app/storage/faces`, so the
two halves of the application disagreed about where a person's photos
lived -- and `./assets` is a read-only mount, so enrollment simply failed.

To move face storage, move `STORAGE_DIR`. Everything follows.

---

## Settings Management Interface

### Accessing Settings

**Path:** Admin → Settings  
**URL:** `/admin/settings`  
**Requirement:** Admin role

### Features

1. **View All Settings** - Browse all configuration variables
2. **Filter by Category** - Find settings by category
3. **Edit Settings** - Change values with validation
4. **Audit Log** - Track all changes with who/when/why
5. **Search** - Find specific settings quickly

### How to Edit a Setting

1. Navigate to Settings page
2. Find the setting (use category filter if needed)
3. Click "Edit" button
4. Enter new value
5. Add reason (optional, but recommended)
6. Click "Save"

### Setting Types

- **String**: Text values (e.g., `"production"`)
- **Integer**: Whole numbers (e.g., `8000`)
- **Float**: Decimal numbers (e.g., `0.4`)
- **Boolean**: `true` or `false`
- **JSON**: Complex data structures

### Sensitive Settings

Settings marked with 🔒 **Sensitive** hide their values for security:
- `JWT_SECRET_KEY`
- `POSTGRES_PASSWORD`
- `DATABASE_URL`

### Readonly Settings

Settings marked with 🔒 **Readonly** cannot be modified:
- Database connection settings (for safety)
- System-protected settings

---

## Common Configuration Scenarios

### Scenario 1: Production Deployment

```bash
ENVIRONMENT=production
DEBUG=false
WORKERS=8
LOG_LEVEL=INFO
SIMILARITY_THRESHOLD=0.4
```

### Scenario 2: Development/Testing

```bash
ENVIRONMENT=development
DEBUG=true
LOG_LEVEL=DEBUG
SIMILARITY_THRESHOLD=0.3
SAVE_WEBHOOK_IMAGES=true
SAVE_CROPPED_IMAGES=true
```

### Scenario 3: High-Load (50+ Cameras)

```bash
WORKERS=12
DB_POOL_SIZE=75
DB_MAX_OVERFLOW=150
QUEUE_WORKERS=100
MAX_QUEUE_SIZE=50000
BATCH_SIZE=50
MAX_CONCURRENT_REQUESTS=1000
```

### Scenario 4: Large-Scale (1M+ Known Faces)

Use `VECTOR_BACKEND=pgvector` with the `PGVECTOR_HNSW_*` parameters — the
pgvector HNSW index is the supported approximate-search path at this scale.
The old FAISS ivf/hnsw/ivfpq knobs were removed; the shipped FAISS index is
exact-only.

### Scenario 5: Show Unknown Faces on Dashboard

```bash
SHOW_UNKNOWN_FACES_ON_DASHBOARD=true
SAVE_UNKNOWN_FACES=true
```

### Scenario 6: Debugging Recognition Issues

```bash
LOG_LEVEL=DEBUG
SAVE_WEBHOOK_IMAGES=true
SAVE_CROPPED_IMAGES=true
SIMILARITY_THRESHOLD=0.3  # Lower threshold for testing
```

---

## Troubleshooting

### Settings Not Applied

**Problem:** Changes in `.env` file not taking effect

**Solutions:**
1. Restart the application
2. Check file location (must be in project root)
3. Verify syntax (no spaces around `=`)
4. Check for typos in variable names

### Settings Page Shows Wrong Values

**Problem:** Web interface shows different values than `.env` file

**Solution:** Settings in the database (from web interface) override `.env` file. To use `.env` values:
1. Delete settings from database, OR
2. Update via web interface to match `.env`

### Docker Container Can't Find Files

**Problem:** Path errors in Docker

**Solution:** Use absolute Docker paths (starting with `/app/`), and set
only the root -- the gallery and debug stores are derived from it:
```bash
# Wrong: relative, and FACES_DIR is not settable at all
FACES_DIR=./storage/faces

# Correct
STORAGE_DIR=/app/storage      # FACES_DIR becomes /app/storage/faces
```

### FAISS Index Not Working

**Problem:** Recognition not working after changing index type

**Solutions:**
1. Restart application
2. Reload known faces: `POST /api/admin/identities/load-known-faces`
3. Verify indexes: `GET /api/admin/identities/verify-indexes`
4. Check logs for errors

### High Memory Usage

**Problem:** System using too much memory

**Solutions:**
1. Reduce `FACE_TRACKING_MAX_ENTRIES`
2. Reduce `FACE_TRACKING_MAX_MEMORY_MB`
3. Reduce `REDIS_MAX_CONNECTIONS`
4. Lower `API_MAX_PAGE_SIZE` so listing endpoints return smaller pages

### Slow Recognition

**Problem:** Face recognition is slow

**Solutions:**
1. Enable GPU: `USE_GPU=true`
2. Reduce `SEARCH_RETRIEVAL_FLOOR` scope or `SEARCH_MAX_TOP_K` (a smaller candidate pool per query)
3. Increase `QUEUE_WORKERS`
4. Raise `SIMILARITY_THRESHOLD` (fewer results survive the display filter)

> The FAISS index-type knobs (`KNOWN_INDEX_TYPE=hnsw`, `ivfpq`) were removed:
> the shipped index refuses to build anything but `flat`. See §Vector Search.

---

## Best Practices

### 1. Start with Defaults

Begin with default values and adjust only what's needed.

### 2. Document Changes

When changing settings via web interface, always add a reason in the "Change Reason" field.

### 3. Test Changes

Test configuration changes in development before applying to production.

### 4. Monitor Performance

After changing settings, monitor:
- Response times
- Memory usage
- Error rates
- Recognition accuracy

### 5. Backup Configuration

Keep backups of:
- `.env` file
- Database settings (export from Settings page)
- Docker compose configuration

### 6. Version Control

Commit `.env.example` (without secrets) to version control, but never commit actual `.env` files with passwords/keys.

---

## Quick API Reference

### Identity Merge APIs (Production Grade)

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/admin/identities/merge-preview` | POST | Preview merge before executing (RECOMMENDED) |
| `/api/admin/identities/merge-multiple` | POST | Merge 2+ identities with AI selection |
| `/api/admin/identities/merge` | POST | Simple merge of 2 identities |
| `/api/admin/merge-suggestions` | GET | Get auto-generated merge suggestions |
| `/api/admin/merge-suggestions/generate-pipeline-aware` | POST | Generate pipeline-aware ML merge suggestions (filters by user pipelines) |
| `/api/admin/merge-suggestions/train-model` | POST | Train the ML similarity model using collected user feedback |
| `/api/admin/merge-suggestions/model-status` | GET | Get status of the ML similarity model (training samples, ready to train) |

### Settings APIs

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/settings` | GET | Get all settings |
| `/api/settings/{key}` | GET | Get specific setting |
| `/api/settings/{key}` | PUT | Update setting value |
| `/api/settings/categories` | GET | List all categories |

### Search APIs

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/search/by-image` | POST | Quick search for identities by uploading a face image |
| `/api/search/advanced` | POST | Advanced multi-face search with quality scoring, watchlists, and intelligence features |
| `/api/search/config` | GET | Get search configuration (quality thresholds, confidence bands) |
| `/api/search/quality-check` | POST | Check image quality without performing search |
| `/api/search/batch` | POST | Batch search multiple images at once |
| `/api/search/history` | GET | Get search history |
| `/api/search/export` | POST | Export search results (CSV, PDF, JSON) |
| `/api/watchlists` | GET/POST | Manage watchlists (VIP, Threat, POI) |
| `/api/live-alerts` | GET/POST | Manage live search alerts |
| `/api/identities/{id}/related` | GET | Get related identities |
| `/api/identities/{id}/temporal-patterns` | GET | Get temporal patterns |
| `/api/identities/{id}/cross-camera` | GET | Get cross-camera tracking data |

### FAISS Management APIs

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/admin/identities/verify-indexes` | GET | Verify FAISS index integrity |
| `/api/admin/identities/repair-indexes` | POST | Repair FAISS indexes |
| `/api/admin/identities/load-known-faces` | POST | Reload known faces from disk |

---

## Related Documentation

- **Settings Management Guide**: `24_SETTINGS_MANAGEMENT_GUIDE.md`
- **FAISS Production Scaling**: `70_VECTOR_INDEX_CONTRACT.md`
- **50 Cameras Scalability**: `32_50_CAMERAS_SCALABILITY_ANALYSIS.md`
- **FAISS Repair Guide**: `70_VECTOR_INDEX_CONTRACT.md`
- **Multi-Identity Merge Guide**: `28_MULTI_IDENTITY_MERGE_GUIDE.md`
- **Advanced Merge Flow Guide**: `37_ADVANCED_MERGE_FLOW_GUIDE.md`
- **Search by Image Guide**: `38_SEARCH_BY_IMAGE_GUIDE.md` - Quick search functionality
- **Advanced Search Intelligence Guide**: `39_ADVANCED_SEARCH_INTELLIGENCE_GUIDE.md` - Production-grade advanced search with watchlists, alerts, and intelligence
- **Live Alerts Guide**: `40_LIVE_ALERTS_GUIDE.md` - Complete guide to live search alerts with backend-driven defaults
- **Pipeline-Aware ML Clustering Guide**: `41_PIPELINE_AWARE_ML_CLUSTERING_GUIDE.md` - Pipeline-aware merge suggestions with ML-based similarity
- **ML Similarity Model Guide**: `42_ML_SIMILARITY_MODEL_GUIDE.md` - Trainable neural network for improving merge suggestion accuracy

---

**Last Updated:** January 2025  
**Version:** 5.0.0


# Complete Configuration Guide

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

1. **Environment Variables** - Highest priority
2. **`.env` File** - If exists in project root
3. **`config.py` Defaults** - Fallback values

**Note:** The web interface (Settings page) stores values in the database and applies them as environment variables, so they take precedence over `config.py` defaults.

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
| `REDIS_POOL_SIZE` | `50` | Redis connection pool size |
| `CACHE_TTL` | `3600` | Cache time-to-live (seconds) |
| `CACHE_LOCAL_SIZE` | `50000` | Local in-memory cache size |

### 6. Face Recognition Models

Controls AI model behavior:

| Setting | Default | Description |
|---------|---------|-------------|
| `DETECTION_MODEL` | `/app/weights/det_10g.onnx` | Path to face detection model |
| `RECOGNITION_MODEL` | `/app/weights/w600k_r50.onnx` | Path to face recognition model |
| `SIMILARITY_THRESHOLD` | `0.4` | Minimum similarity to match faces (0.0-1.0) |
| `CONFIDENCE_THRESHOLD` | `0.5` | Minimum confidence for face detection (0.0-1.0) |
| `FACES_DIR` | `/app/assets/faces` | Directory for known face images |
| `DB_PATH` | `/app/database/face_database` | Path to face database |

**Tuning Similarity Threshold:**
- **Lower (0.3-0.4)**: More matches, but may have false positives
- **Higher (0.5-0.6)**: Fewer matches, but more accurate
- **Recommended**: Start with 0.4, adjust based on results

### 7. FAISS Index Configuration

Controls the face recognition index system:

| Setting | Default | Description |
|---------|---------|-------------|
| `IDENTITY_EMBEDDING_SIZE` | `512` | Size of face embedding vectors |
| `IDENTITY_INDEX_DB_PATH` | `/app/database/identity_indexes` | Path to FAISS index files |
| `IDENTITY_INDEX_AUTO_SAVE_INTERVAL` | `300` | Auto-save interval (seconds) |
| `REPAIR_FAISS_ON_STARTUP` | `true` | Repair indexes on startup |
| `REPAIR_FAISS_INTERVAL_HOURS` | `24` | Background repair interval (hours) |
| `FAISS_LAZY_MARKING_THRESHOLD` | `1` | Lazy marking threshold (for demo: 1, production: 100+) |

**Index Types:**

| Type | Best For | Speed | Memory | Accuracy |
|------|----------|-------|--------|----------|
| `flat` | <100K faces | Medium | High | 100% |
| `ivf` | 100K-1M faces | Fast | Medium | 95-99% |
| `hnsw` | 1M-10M faces | Fastest | High | 95-99% |
| `ivfpq` | 10M+ faces | Medium | Low | 90-95% |

**Index Type Settings:**

For `ivf` (IndexIVFFlat):
```bash
KNOWN_INDEX_TYPE=ivf
KNOWN_INDEX_NLIST=1000      # sqrt(N) recommended
KNOWN_INDEX_NPROBE=20       # 10-50, higher = better accuracy
```

For `hnsw` (IndexHNSWFlat):
```bash
KNOWN_INDEX_TYPE=hnsw
KNOWN_INDEX_HNSW_M=32       # 16-64, higher = better accuracy
KNOWN_INDEX_HNSW_EF_CONSTRUCTION=200
KNOWN_INDEX_HNSW_EF_SEARCH=64
```

For `ivfpq` (IndexIVFPQ):
```bash
KNOWN_INDEX_TYPE=ivfpq
KNOWN_INDEX_PQ_M=64         # 8, 16, 32, 64
KNOWN_INDEX_PQ_BITS=8       # Standard: 8
```

**See:** `30_FAISS_PRODUCTION_SCALING.md` for detailed scaling guide

### 8. Queue & Processing Configuration

Controls image processing queue:

| Setting | Default | Description |
|---------|---------|-------------|
| `MAX_QUEUE_SIZE` | `10000` | Maximum queue size |
| `QUEUE_WORKERS` | `50` | Number of queue workers |
| `BATCH_SIZE` | `20` | Batch processing size |
| `MAX_CONCURRENT_REQUESTS` | `500` | Maximum concurrent requests |
| `GPU_BATCH_SIZE` | `32` | Batch size for GPU processing |
| `CPU_BATCH_SIZE` | `10` | Batch size for CPU processing |
| `PIPELINE_BATCH_SIZE` | `5` | Batch size per pipeline |

**For 50+ Cameras:**
```bash
QUEUE_WORKERS=100
MAX_QUEUE_SIZE=50000
BATCH_SIZE=50
```

### 9. Storage Configuration

Controls image storage:

| Setting | Default | Description |
|---------|---------|-------------|
| `STORAGE_DIR` | `/app/storage` | Directory for storing images |
| `SAVE_IMAGES` | `true` | Enable image saving |
| `MAX_STORAGE_GB` | `500` | Maximum storage size (GB) |
| `MAX_PHOTOS_PER_PERSON` | `1` | Maximum photos per person |
| `SAVE_UNKNOWN_FACES` | `false` | Save unknown face images |
| `MAX_FILE_SIZE` | `10485760` | Maximum file size (10MB) |

**Debug Settings:**
```bash
SAVE_WEBHOOK_IMAGES=true          # Save raw webhook images
WEBHOOK_IMAGES_DIR=./debug/webhook_images
SAVE_CROPPED_IMAGES=true          # Save cropped person images
CROPPED_IMAGES_DIR=./debug/cropped
```

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
| `FACE_TRACKING_CLEANUP_INTERVAL` | `300` | Cleanup interval (seconds) |

**Dashboard Visibility:**
- `SHOW_UNKNOWN_FACES_ON_DASHBOARD=false`: Unknown faces only in "Unknown Faces Center"
- `SHOW_UNKNOWN_FACES_ON_DASHBOARD=true`: Unknown faces appear on main dashboard

### 11. Identity Management Configuration

**Clustering & Merge Suggestions:**

| Setting | Default | Description |
|---------|---------|-------------|
| `CLUSTER_STARTUP_DELAY_HOURS` | `0` | Hours to wait after startup before first clustering (0 = immediate) |
| `CLUSTER_INTERVAL_HOURS` | `24` | How often to generate merge suggestions |
| `CLUSTER_MIN_SIZE` | `2` | Minimum cluster size |
| `CLUSTER_EPS` | `0.35` | Clustering epsilon (similarity threshold) |
| `CLUSTER_MIN_SAMPLES` | `2` | Minimum samples per cluster |

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
| `FACE_QUALITY_THRESHOLD_SIZE` | `64` | Minimum face size (pixels) for quality assessment | pixels |
| `FACE_QUALITY_THRESHOLD_ANGLE` | `30.0` | Maximum face angle (degrees) for quality assessment | degrees |
| `FACE_QUALITY_THRESHOLD_LIGHTING` | `0.4` | Lighting threshold for quality assessment | 0-1 |

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
| `BATCH_SEARCH_TIMEOUT_SECONDS` | `300` | Timeout in seconds for batch search operations | seconds |
| `BATCH_SEARCH_ENABLED` | `true` | Enable batch search functionality | bool |

#### Search History

| Setting | Default | Description | Unit |
|---------|---------|-------------|------|
| `SEARCH_HISTORY_RETENTION_DAYS` | `90` | Days to retain search history records | days |
| `SEARCH_HISTORY_MAX_PER_USER` | `1000` | Maximum search history records per user | count |

#### Live Alerts

| Setting | Default | Description | Unit |
|---------|---------|-------------|------|
| `LIVE_ALERT_DEFAULT_COOLDOWN_MINUTES` | `30` | Default cooldown period (minutes) between live alert triggers. Prevents alert spam when same person detected multiple times | minutes |
| `LIVE_ALERT_MAX_PER_USER` | `50` | Maximum number of active live alerts per user. Prevents resource exhaustion | count |
| `LIVE_ALERTS_ENABLED` | `true` | Enable/disable live search alerts feature globally | bool |

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
| `BATCH_WRITE_MAX_WAIT` | `5.0` | Maximum wait time (seconds) |

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
FACES_DIR=/app/assets/faces
STORAGE_DIR=/app/storage
LOG_DIR=/var/log/face-recognition
```

**Local Development:**
```bash
DETECTION_MODEL=./weights/det_10g.onnx
FACES_DIR=./assets/faces
STORAGE_DIR=./storage
LOG_DIR=./logs
```

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
REPAIR_FAISS_ON_STARTUP=true
REPAIR_FAISS_INTERVAL_HOURS=24
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

```bash
KNOWN_INDEX_TYPE=ivf
KNOWN_INDEX_NLIST=1000
KNOWN_INDEX_NPROBE=20
REPAIR_FAISS_ON_STARTUP=false  # Faster startup
REPAIR_FAISS_INTERVAL_HOURS=12
FAISS_LAZY_MARKING_THRESHOLD=100
```

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

**Solution:** Use Docker paths (starting with `/app/`):
```bash
# Wrong (local path)
FACES_DIR=./assets/faces

# Correct (Docker path)
FACES_DIR=/app/assets/faces
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
3. Use `ivfpq` index type (smaller memory footprint)
4. Reduce `CACHE_LOCAL_SIZE`

### Slow Recognition

**Problem:** Face recognition is slow

**Solutions:**
1. Enable GPU: `USE_GPU=true`
2. Use faster index type: `KNOWN_INDEX_TYPE=hnsw`
3. Increase `QUEUE_WORKERS`
4. Reduce `SIMILARITY_THRESHOLD` (fewer comparisons)

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
- **FAISS Production Scaling**: `30_FAISS_PRODUCTION_SCALING.md`
- **50 Cameras Scalability**: `32_50_CAMERAS_SCALABILITY_ANALYSIS.md`
- **FAISS Repair Guide**: `33_FAISS_REPAIR_AND_SYNCHRONIZATION.md`
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


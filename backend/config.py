"""
Backend Configuration
=====================
Re-exports configuration from central config.py for backward compatibility.
All configuration should be imported from the root config.py.

This module declares NO defaults of its own and has NO import side effects.
It used to run `os.makedirs(settings.STORAGE_DIR)` at import, which meant that
importing a constant created a directory — as a side effect of `import` in
alembic, in gunicorn's config, and in every test collection. Each writer
creates the directory it needs (`exist_ok=True`), and the image creates the
tree, so nothing depended on it happening here.
"""

from config import settings

# =====================================================
# Re-export all settings for backward compatibility
# =====================================================

# Data Retention
#
# DATA_RETENTION_DAYS and CLEANUP_INTERVAL_HOURS are NOT re-exported. Both are
# registered `next_job_run`, so an admin edit takes effect on the next cleanup
# pass — but a module-level copy freezes at import, and /api/stats was reporting
# the frozen number from two fields while a third read the live one. Read them
# as `settings.X` where they are used.
BATCH_WRITE_SIZE = settings.BATCH_WRITE_SIZE   # api_restart: frozen on purpose

# Redis availability
try:
    import redis.asyncio as redis
    REDIS_AVAILABLE = True
except ImportError:
    REDIS_AVAILABLE = False

CACHE_ENABLED = REDIS_AVAILABLE and settings.REDIS_URL is not None

# Face tracking configuration
FACE_TRACKING_ENABLED = settings.FACE_TRACKING_ENABLED
FACE_TRACKING_WINDOW_SECONDS = settings.FACE_TRACKING_WINDOW_SECONDS
FACE_TRACKING_MAX_ENTRIES = settings.FACE_TRACKING_MAX_ENTRIES
FACE_TRACKING_SIMILARITY_THRESHOLD = settings.FACE_TRACKING_SIMILARITY_THRESHOLD
FACE_TRACKING_MAX_MEMORY_MB = settings.FACE_TRACKING_MAX_MEMORY_MB
FACE_TRACKING_CLEANUP_INTERVAL = settings.FACE_TRACKING_CLEANUP_INTERVAL

# Security configuration
MAX_FILE_SIZE = settings.MAX_FILE_SIZE
ALLOWED_IMAGE_EXTENSIONS = set(settings.allowed_image_extensions_list)

# Export settings object for direct access
__all__ = [
    'settings',
    'BATCH_WRITE_SIZE',
    'REDIS_AVAILABLE',
    'CACHE_ENABLED',
    'FACE_TRACKING_ENABLED',
    'FACE_TRACKING_WINDOW_SECONDS',
    'FACE_TRACKING_MAX_ENTRIES',
    'FACE_TRACKING_SIMILARITY_THRESHOLD',
    'FACE_TRACKING_MAX_MEMORY_MB',
    'FACE_TRACKING_CLEANUP_INTERVAL',
    'MAX_FILE_SIZE',
    'ALLOWED_IMAGE_EXTENSIONS',
]


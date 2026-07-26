"""
Backend Configuration
=====================
Re-exports configuration from central config.py for backward compatibility.
All configuration should be imported from the root config.py.
"""

import os
from config import settings

# Create storage directory
os.makedirs(settings.STORAGE_DIR, exist_ok=True)

# =====================================================
# Re-export all settings for backward compatibility
# =====================================================

# Data Retention
DATA_RETENTION_DAYS = settings.DATA_RETENTION_DAYS
CLEANUP_INTERVAL_HOURS = settings.CLEANUP_INTERVAL_HOURS
BATCH_WRITE_SIZE = settings.BATCH_WRITE_SIZE

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
    'DATA_RETENTION_DAYS',
    'CLEANUP_INTERVAL_HOURS',
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


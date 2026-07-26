"""
Performance Configuration Helper
=================================
Auto-adjusts configuration based on GPU availability and load requirements.
Uses central config.py as the source of truth.
"""

import os
import logging
from typing import Dict, Any

logger = logging.getLogger(__name__)

# Try to import central config
try:
    from config import settings
    USE_CENTRAL_CONFIG = True
except ImportError:
    USE_CENTRAL_CONFIG = False
    settings = None


def get_optimized_config() -> Dict[str, Any]:
    """
    Get optimized configuration based on GPU availability and system resources.
    Optimized for 50+ cameras and 20 concurrent users.
    
    Returns:
        Dictionary of optimized configuration values
    """
    # Detect GPU
    has_gpu = False
    try:
        from utils.gpu_detection import detect_gpu
        has_gpu, _, _ = detect_gpu()
    except Exception:
        pass
    
    # Check environment override or use central config
    use_gpu_env = os.getenv("USE_GPU", "").lower()
    if USE_CENTRAL_CONFIG and settings:
        use_gpu = settings.USE_GPU
    else:
        use_gpu = use_gpu_env in ("true", "1", "yes")
    
    if use_gpu_env in ("true", "1", "yes"):
        has_gpu = True
    elif use_gpu_env in ("false", "0", "no"):
        has_gpu = False
    else:
        has_gpu = use_gpu
    
    # Helper function to get config value
    def get_config_value(key: str, default: Any) -> Any:
        """Get config value from central config or environment variable"""
        if USE_CENTRAL_CONFIG and settings and hasattr(settings, key):
            return getattr(settings, key)
        return os.getenv(key, default)
    
    config = {}
    
    if has_gpu:
        logger.info("🚀 GPU detected - applying GPU-optimized configuration")
        config = {
            # Server workers
            "WORKERS": int(get_config_value("WORKERS", "16")),
            
            # Database pool (50+ cameras need more connections)
            "DB_POOL_SIZE": int(get_config_value("DB_POOL_SIZE", "50")),
            "DB_MAX_OVERFLOW": int(get_config_value("DB_MAX_OVERFLOW", "100")),
            
            # Redis connections
            "REDIS_MAX_CONNECTIONS": int(get_config_value("REDIS_MAX_CONNECTIONS", "100")),
            "REDIS_POOL_SIZE": int(get_config_value("REDIS_POOL_SIZE", "50")),
            "CACHE_LOCAL_SIZE": int(get_config_value("CACHE_LOCAL_SIZE", "50000")),
            
            # Queue and processing (GPU can handle more)
            "MAX_QUEUE_SIZE": int(get_config_value("MAX_QUEUE_SIZE", "5000")),
            "QUEUE_WORKERS": int(get_config_value("QUEUE_WORKERS", "30")),
            "BATCH_SIZE": int(get_config_value("BATCH_SIZE", "20")),
            "GPU_BATCH_SIZE": int(get_config_value("GPU_BATCH_SIZE", "32")),
            "MAX_CONCURRENT_REQUESTS": int(get_config_value("MAX_CONCURRENT_REQUESTS", "300")),
            
            # Batch writing (larger batches for efficiency)
            "BATCH_WRITE_SIZE": int(get_config_value("BATCH_WRITE_SIZE", "50")),
            "BATCH_WRITE_INTERVAL": float(get_config_value("BATCH_WRITE_INTERVAL", "1.0")),
            
            # Face tracking (more entries for 50+ cameras)
            "FACE_TRACKING_MAX_ENTRIES": int(get_config_value("FACE_TRACKING_MAX_ENTRIES", "5000")),
            "FACE_TRACKING_MAX_MEMORY_MB": int(get_config_value("FACE_TRACKING_MAX_MEMORY_MB", "2000")),
        }
    else:
        logger.info("💻 CPU mode - applying CPU-optimized configuration")
        config = {
            # Server workers
            "WORKERS": int(get_config_value("WORKERS", "8")),
            
            # Database pool
            "DB_POOL_SIZE": int(get_config_value("DB_POOL_SIZE", "30")),
            "DB_MAX_OVERFLOW": int(get_config_value("DB_MAX_OVERFLOW", "60")),
            
            # Redis connections
            "REDIS_MAX_CONNECTIONS": int(get_config_value("REDIS_MAX_CONNECTIONS", "50")),
            "REDIS_POOL_SIZE": int(get_config_value("REDIS_POOL_SIZE", "25")),
            "CACHE_LOCAL_SIZE": int(get_config_value("CACHE_LOCAL_SIZE", "20000")),
            
            # Queue and processing
            "MAX_QUEUE_SIZE": int(get_config_value("MAX_QUEUE_SIZE", "2000")),
            "QUEUE_WORKERS": int(get_config_value("QUEUE_WORKERS", "15")),
            "BATCH_SIZE": int(get_config_value("BATCH_SIZE", "10")),
            "CPU_BATCH_SIZE": int(get_config_value("CPU_BATCH_SIZE", "10")),
            "MAX_CONCURRENT_REQUESTS": int(get_config_value("MAX_CONCURRENT_REQUESTS", "150")),
            
            # Batch writing
            "BATCH_WRITE_SIZE": int(get_config_value("BATCH_WRITE_SIZE", "25")),
            "BATCH_WRITE_INTERVAL": float(get_config_value("BATCH_WRITE_INTERVAL", "2.0")),
            
            # Face tracking
            "FACE_TRACKING_MAX_ENTRIES": int(get_config_value("FACE_TRACKING_MAX_ENTRIES", "2000")),
            "FACE_TRACKING_MAX_MEMORY_MB": int(get_config_value("FACE_TRACKING_MAX_MEMORY_MB", "1000")),
        }
    
    return config


def apply_optimized_config():
    """
    Apply optimized configuration to environment variables.
    This should be called early in application startup.
    """
    config = get_optimized_config()
    
    for key, value in config.items():
        if key not in os.environ:
            os.environ[key] = str(value)
            logger.debug(f"Set {key} = {value}")
    
    logger.info("✅ Performance configuration applied")
    return config


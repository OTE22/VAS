"""
Backend Core Module
===================
Exports all core classes and instances.
"""

# Import all core modules
from backend.core.face_tracker import FaceTracker, face_tracker
from backend.core.cache_manager import CacheManager, cache_manager
from backend.core.circuit_breaker import CircuitState, CircuitBreaker, db_circuit_breaker
from backend.core.websocket_manager import WebSocketManager, ws_manager
from backend.core.background_task_notifier import BackgroundTaskNotifier, background_task_notifier, TaskType
from backend.core.processing_queue import ProcessingQueue, processing_queue
from backend.core.model_manager import ModelManager, model_manager
from backend.core.batch_writer import BatchDatabaseWriter, batch_writer
from backend.core.data_retention import DataRetentionManager, retention_manager
from backend.core.production_cache import ProductionCacheManager, production_cache_manager
from backend.core.face_recognition_cache import FaceRecognitionCache
# face_recognition_cache will be set during startup in lifespan.py
# Import it, but it may be None until startup
try:
    from backend.core.face_recognition_cache import face_recognition_cache
except (ImportError, AttributeError):
    face_recognition_cache = None
from backend.core.system_metrics import SystemMetricsCollector, metrics_collector

# Identity management (optional - may not be available)
try:
    from backend.core.identity_clustering import IdentityClusteringService, clustering_service
except ImportError:
    clustering_service = None

try:
    from backend.core.identity_retention import IdentityRetentionManager, identity_retention_manager
except ImportError:
    identity_retention_manager = None

# Identity service and index (set during startup in lifespan.py)
try:
    from backend.core.identity_service import identity_service
except (ImportError, AttributeError):
    identity_service = None

try:
    from backend.core.identity_index import identity_index
except (ImportError, AttributeError):
    identity_index = None

__all__ = [
    'FaceTracker', 'face_tracker',
    'CacheManager', 'cache_manager',
    'CircuitState', 'CircuitBreaker', 'db_circuit_breaker',
    'WebSocketManager', 'ws_manager',
    'ProcessingQueue', 'processing_queue',
    'ModelManager', 'model_manager',
    'BatchDatabaseWriter', 'batch_writer',
    'DataRetentionManager', 'retention_manager',
    'ProductionCacheManager', 'production_cache_manager',
    'FaceRecognitionCache', 'face_recognition_cache',
    'SystemMetricsCollector', 'metrics_collector',
    'IdentityClusteringService', 'clustering_service',
    'IdentityRetentionManager', 'identity_retention_manager',
]


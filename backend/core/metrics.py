"""
Prometheus Metrics
==================
All Prometheus metrics initialization and management.
"""

from prometheus_client import Counter, Histogram, Gauge

# Global metrics variables
metrics_requests_total = None
metrics_processing_time = None
metrics_queue_size = None
metrics_faces_detected = None
metrics_faces_skipped = None
metrics_faces_batch_skipped = None
metrics_active_pipelines = None
metrics_cache_hits = None
metrics_cache_misses = None
metrics_db_operations = None
metrics_cleanup_operations = None
metrics_websocket_connections = None
metrics_worker_active = None
metrics_cache_hit_rate = None
metrics_cache_size = None
metrics_cache_write_behind = None
metrics_cache_circuit_state = None
metrics_event_loop_lag = None
metrics_request_duration = None


def initialize_metrics():
    """Initialize all Prometheus metrics with duplicate protection"""
    global metrics_requests_total, metrics_processing_time, metrics_queue_size
    global metrics_faces_detected, metrics_faces_skipped, metrics_faces_batch_skipped
    global metrics_active_pipelines, metrics_cache_hits, metrics_cache_misses
    global metrics_db_operations, metrics_cleanup_operations, metrics_websocket_connections
    global metrics_worker_active, metrics_cache_hit_rate, metrics_cache_size
    global metrics_cache_write_behind, metrics_cache_circuit_state
    global metrics_event_loop_lag, metrics_request_duration

    # Helper function to safely create metrics
    def safe_metric(metric_class, name, documentation, **kwargs):
        try:
            return metric_class(name, documentation, **kwargs)
        except ValueError as e:
            if "Duplicated timeseries" in str(e):
                # Get existing metric
                from prometheus_client import REGISTRY
                return REGISTRY.get(name)
            else:
                raise

    # Initialize all metrics
    metrics_requests_total = safe_metric(
        Counter, 'face_recognition_requests_total', 'Total webhook requests', labelnames=['pipeline_id', 'status']
    )
    metrics_processing_time = safe_metric(
        Histogram, 'face_recognition_processing_seconds', 'Processing time in seconds'
    )
    metrics_queue_size = safe_metric(
        Gauge, 'face_recognition_queue_size', 'Current queue size'
    )
    metrics_event_loop_lag = safe_metric(
        Gauge, 'face_recognition_event_loop_lag_seconds',
        'Event loop scheduling lag (drift of a 1s sleep); >0.5s means the loop is blocked'
    )
    metrics_request_duration = safe_metric(
        Histogram, 'face_recognition_request_duration_seconds',
        'HTTP request duration', labelnames=['method', 'status']
    )
    metrics_faces_detected = safe_metric(
        Counter, 'face_recognition_faces_detected_total', 'Total faces detected', labelnames=['name']
    )
    metrics_faces_skipped = safe_metric(
        Counter, 'face_recognition_faces_skipped_total', 'Faces skipped (already tracked)', labelnames=['name']
    )
    metrics_faces_batch_skipped = safe_metric(
        Counter, 'face_recognition_batch_duplicates_total', 'Faces skipped (duplicate in same batch)', labelnames=['name']
    )
    metrics_active_pipelines = safe_metric(
        Gauge, 'face_recognition_active_pipelines', 'Number of active pipelines'
    )
    metrics_cache_hits = safe_metric(
        Counter, 'face_recognition_cache_hits_total', 'Cache hits'
    )
    metrics_cache_misses = safe_metric(
        Counter, 'face_recognition_cache_misses_total', 'Cache misses'
    )
    metrics_db_operations = safe_metric(
        Histogram, 'face_recognition_db_operations_seconds', 'Database operation time'
    )
    metrics_cleanup_operations = safe_metric(
        Counter, 'face_recognition_cleanup_total', 'Total cleanup operations'
    )
    metrics_websocket_connections = safe_metric(
        Gauge, 'face_recognition_websocket_connections', 'Active WebSocket connections'
    )
    metrics_worker_active = safe_metric(
        Gauge, 'face_recognition_worker_active', 'Active worker count', labelnames=['worker_id']
    )
    metrics_cache_hit_rate = safe_metric(
        Gauge, 'face_recognition_cache_hit_rate', 'Cache hit rate percentage', labelnames=['type']
    )
    metrics_cache_size = safe_metric(
        Gauge, 'face_recognition_cache_size', 'Cache size', labelnames=['type']
    )
    metrics_cache_write_behind = safe_metric(
        Counter, 'face_recognition_cache_write_behind_total', 'Write-behind operations'
    )
    metrics_cache_circuit_state = safe_metric(
        Gauge, 'face_recognition_cache_circuit_state', 'Cache circuit breaker state (0=closed, 1=open, 2=half_open)'
    )


# Initialize metrics at module level
initialize_metrics()


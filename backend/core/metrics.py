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
metrics_queue_capacity = None
metrics_db_pool_in_use = None
metrics_db_pool_size = None
metrics_db_operation_failures = None
metrics_vector_search = None
metrics_identity_ops = None
metrics_inference_in_flight = None
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
# Background-service supervision (set by backend.core.service_supervisor):
# these are what let Prometheus alert on a dead or failing background loop —
# previously a loop could be dead for a week with no observable signal.
metrics_bg_last_success = None
metrics_bg_failures = None
metrics_bg_service_up = None
metrics_map_availability_invalid = None
metrics_vector_index_size = None
metrics_vector_index_drift = None
metrics_vector_index_pending = None
metrics_vector_index_last_rebuild = None
metrics_vector_index_recovery_failures = None
metrics_process_rss = None
# Security-intelligence endpoints (set below; observed in routes/intelligence):
# per-feature request counts and latency — these five endpoints previously ran
# with zero observability.
metrics_intel_requests = None
metrics_intel_duration = None
# Risk platform (assessments, distributed locks, rate limiting):
metrics_assessments = None
metrics_assessment_duration = None
metrics_lock_contention = None
metrics_rate_limited = None
# Ingest credential checks. Deliberately NOT labelled by pipeline_id:
# the caller chooses that value, so it would be an unbounded-cardinality
# lever pointed straight at the metrics registry.
metrics_webhook_auth = None
metrics_webhook_auth_source = None


def initialize_metrics():
    """Initialize all Prometheus metrics with duplicate protection"""
    global metrics_requests_total, metrics_processing_time, metrics_queue_size
    global metrics_faces_detected, metrics_faces_skipped, metrics_faces_batch_skipped
    global metrics_active_pipelines, metrics_cache_hits, metrics_cache_misses
    global metrics_db_operations, metrics_cleanup_operations, metrics_websocket_connections
    global metrics_worker_active, metrics_cache_hit_rate, metrics_cache_size
    global metrics_cache_write_behind, metrics_cache_circuit_state
    global metrics_event_loop_lag, metrics_request_duration
    global metrics_bg_last_success, metrics_bg_failures, metrics_bg_service_up
    global metrics_map_availability_invalid
    # Without these globals the assignments below bind function-locals: the
    # collectors still land in the Prometheus REGISTRY (so /metrics prints their
    # HELP lines) while the module attributes stay None, and every publisher
    # silently no-ops. A metric that exists but never gets a value is worse than
    # a missing one — it reads as "index size is zero".
    global metrics_vector_index_size, metrics_vector_index_drift
    global metrics_vector_index_pending, metrics_vector_index_last_rebuild
    global metrics_vector_index_recovery_failures
    global metrics_process_rss
    global metrics_intel_requests, metrics_intel_duration
    global metrics_assessments, metrics_assessment_duration
    global metrics_lock_contention, metrics_rate_limited
    global metrics_webhook_auth, metrics_webhook_auth_source
    global metrics_queue_capacity, metrics_db_pool_in_use, metrics_db_pool_size
    global metrics_db_operation_failures, metrics_vector_search
    global metrics_identity_ops, metrics_inference_in_flight

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
    metrics_bg_last_success = safe_metric(
        Gauge, 'fr_background_task_last_success_timestamp',
        'Unix time of the last successful background cycle', labelnames=['service']
    )
    metrics_bg_failures = safe_metric(
        Counter, 'fr_background_task_failures_total',
        'Background service cycle failures', labelnames=['service']
    )
    metrics_bg_service_up = safe_metric(
        Gauge, 'fr_background_service_up',
        '1 while the supervised loop is alive, 0 once stopped', labelnames=['service']
    )
    # An availability verdict that says "unavailable" without a registered
    # reason code is a bug in map_availability, not a data fault. The style is
    # still reported closed, so nothing breaks visibly — which is exactly why
    # it needs a counter to be noticed at all.
    metrics_map_availability_invalid = safe_metric(
        Counter, 'fr_map_availability_state_invalid_total',
        'Times a basemap style was unavailable with no composable reason',
        labelnames=['style']
    )
    # -- Vector search index -------------------------------------------------
    # PostgreSQL is authoritative; the index is a derived cache. Before these,
    # nothing anywhere answered "how many vectors are indexed?" or "does the
    # index match the database?" — that was only discoverable by hand.
    metrics_vector_index_size = safe_metric(
        Gauge, 'fr_vector_index_size',
        'Vectors currently held by the search index', labelnames=['backend']
    )
    metrics_vector_index_drift = safe_metric(
        Gauge, 'fr_vector_index_drift',
        'Entries that disagree with PostgreSQL (missing + stale + mismatched) '
        'at the last reconciliation', labelnames=['backend']
    )
    metrics_vector_index_pending = safe_metric(
        Gauge, 'fr_vector_index_pending',
        'Embedding rows committed to PostgreSQL but not confirmed in the index '
        '(vector_index_sync_state <> synced)', labelnames=['backend']
    )
    metrics_vector_index_last_rebuild = safe_metric(
        Gauge, 'fr_vector_index_last_rebuild_timestamp',
        'Unix time of the last successful rebuild from PostgreSQL',
        labelnames=['backend']
    )
    metrics_vector_index_recovery_failures = safe_metric(
        Counter, 'fr_vector_index_recovery_failures_total',
        'Snapshots rejected on load (checksum, ntotal/keys mismatch, parse '
        'failure) and quarantined', labelnames=['backend', 'reason']
    )
    metrics_process_rss = safe_metric(
        Gauge, 'fr_process_rss_bytes',
        'Worker process resident set size (from /proc/self/status)'
    )
    metrics_queue_size = safe_metric(
        Gauge, 'face_recognition_queue_size', 'Current queue size'
    )
    # Capacity next to depth, so utilization is computable in PromQL
    # (queue_size / queue_capacity) instead of hardcoding MAX_QUEUE_SIZE
    # into every dashboard and alert.
    metrics_queue_capacity = safe_metric(
        Gauge, 'face_recognition_queue_capacity',
        'Configured maximum queue size (MAX_QUEUE_SIZE)'
    )
    # The DB pool was only visible in /health/detailed JSON; exhaustion had no
    # Prometheus signal at all while every scrape itself takes a connection.
    metrics_db_pool_in_use = safe_metric(
        Gauge, 'fr_db_pool_in_use',
        'Database connections currently checked out (incl. overflow)'
    )
    metrics_db_pool_size = safe_metric(
        Gauge, 'fr_db_pool_size',
        'Configured database pool size (excl. overflow)'
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
    metrics_db_operation_failures = safe_metric(
        Counter, 'face_recognition_db_operation_failures_total',
        'Batch write flushes that failed or timed out',
        labelnames=['reason']  # timeout | error — bounded
    )
    # The query that runs on every recognized face had no timing at all —
    # the index gauges say how BIG the index is, nothing said how SLOW it is.
    metrics_vector_search = safe_metric(
        Histogram, 'fr_vector_search_seconds',
        'pgvector similarity search duration (identity matching hot path)'
    )
    # Destructive identity operations were invisible to Prometheus entirely.
    # op is a fixed enum — NEVER an identity id.
    metrics_identity_ops = safe_metric(
        Counter, 'fr_identity_ops_total',
        'Identity lifecycle operations executed',
        labelnames=['op']  # promote | merge | merge_multiple | unmerge
    )
    # Utilization of the 3-thread inference bottleneck: in-flight crops
    # currently holding the inference semaphore.
    metrics_inference_in_flight = safe_metric(
        Gauge, 'fr_inference_in_flight',
        'Face crops currently in the inference pool (bounded by MAX_CONCURRENT_INFERENCE)'
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
    # Was a Counter fed the queue DEPTH every 30s — a monotonically climbing
    # number with no meaning. The quantity is a level, so it is a Gauge.
    metrics_cache_write_behind = safe_metric(
        Gauge, 'face_recognition_cache_write_behind_queue',
        'Write-behind queue depth (pending deferred cache writes)'
    )
    metrics_cache_circuit_state = safe_metric(
        Gauge, 'face_recognition_cache_circuit_state', 'Cache circuit breaker state (0=closed, 1=open, 2=half_open)'
    )
    metrics_intel_requests = safe_metric(
        Counter, 'fr_intel_requests_total',
        'Security-intelligence endpoint requests',
        labelnames=['feature', 'result']  # result: success | error | timeout
    )
    metrics_intel_duration = safe_metric(
        Histogram, 'fr_intel_duration_seconds',
        'Security-intelligence endpoint duration', labelnames=['feature'],
        buckets=(0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0)
    )
    metrics_assessments = safe_metric(
        Counter, 'fr_assessments_total',
        'Threat-assessment outcomes',
        # result: created | deduplicated | persist_error | insufficient_data
        # | acknowledged | resolved | reopened
        labelnames=['result']
    )
    metrics_assessment_duration = safe_metric(
        Histogram, 'fr_assessment_scoring_seconds',
        'Unified risk-engine scoring + persistence duration',
        buckets=(0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0)
    )
    metrics_lock_contention = safe_metric(
        Counter, 'fr_distributed_lock_contention_total',
        'Distributed lock acquisition conflicts', labelnames=['lock']
    )
    metrics_rate_limited = safe_metric(
        Counter, 'fr_rate_limit_rejections_total',
        'Requests rejected with 429', labelnames=['scope']
    )
    metrics_webhook_auth = safe_metric(
        Counter, 'fr_webhook_auth_total',
        'Pipeline ingest credential checks by outcome '
        '(ok, missing, invalid, would_reject, unenforced)',
        labelnames=['result']
    )
    metrics_webhook_auth_source = safe_metric(
        Counter, 'fr_webhook_auth_source_total',
        'Successful ingest auth by credential source. Answers the one question '
        'a per-credential label would have served - "can the environment keys '
        'be retired yet?" - at a cardinality of exactly two. Deliberately NOT '
        'labelled by credential name: prometheus_client never reclaims a series '
        'within a process, so issuing and revoking would grow it monotonically '
        'per worker. Per-credential attribution lives in the logs and in '
        'webhook_credentials.last_used_at.',
        labelnames=['source']
    )


# Initialize metrics at module level
initialize_metrics()


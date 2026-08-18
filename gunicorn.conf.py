"""
Gunicorn configuration for Face Recognition Service v4.0
Optimized for 100+ concurrent requests with high reliability
"""
import multiprocessing
import os
import logging

# =====================================================
# Server Socket
# =====================================================
# No try/except around this import, and no os.getenv fallback anywhere in this
# file. This module IS the production entry point: if central configuration
# cannot be loaded, the correct outcome is a failure to start, not a server
# bound to 0.0.0.0:8000 with default credentials. The previous version caught
# ImportError and silently continued on os.getenv defaults, which meant a
# broken config.py produced a running-but-misconfigured service.
from config import settings

bind = f"{settings.HOST}:{settings.PORT}"
backlog = 2048  # Maximum number of pending connections

# =====================================================
# Worker Processes
# =====================================================
cpu_count = multiprocessing.cpu_count()
USE_GPU = settings.USE_GPU

# WORKERS is taken literally. It used to fall back to an auto-derived count
# (8-26 processes) whenever WORKERS was not positive — which would silently
# break the single-worker invariant this deployment depends on: admin settings
# propagation, SQL-agent cancellation and single-flight job guards are all
# process-local (see backend/core/runtime_settings.py). Raising worker count by
# accident is not a performance decision, it is a correctness change.
workers = settings.WORKERS
if workers < 1:
    raise SystemExit(
        f"WORKERS={settings.WORKERS} is not a valid process count. "
        "Set WORKERS=1 (the supported deployment) or a deliberate positive "
        "number together with ALLOW_MULTI_WORKER=true."
    )

if USE_GPU:
    print(f"🚀 GPU mode: Using {workers} workers (CPU cores: {cpu_count})")
else:
    print(f"💻 CPU mode: Using {workers} workers (CPU cores: {cpu_count})")

worker_class = "uvicorn.workers.UvicornWorker"
# Increased for 50+ cameras and 20 concurrent users
worker_connections = 2000 if USE_GPU else 1000  # GPU can handle more connections
threads = 1  # Threads per worker (keep at 1 for async)

# Worker lifecycle
#
# max_requests recycling is only safe when ANOTHER worker can cover the gap.
# With a single worker (the current CPU deployment: WORKERS=1 is load-bearing
# because of process-local state), a recycle is a full outage: observed live
# 2026-07-28, "Maximum request limit of 606 exceeded" took the only worker
# down for 63 seconds (shutdown 10:12:22 -> startup complete 10:13:25) and
# gunicorn logged the planned recycle as "Worker exited with code 1". The
# dashboard's polling reaches ~600 requests in well under an hour, so this
# repeated all day and read as mysterious crashes.
#
# Recycling exists to paper over slow memory leaks; this process has been
# observed stable at ~730MB RSS under sustained test load. If a leak appears,
# fix the leak or schedule an off-hours restart — do not take a planned
# outage every few hundred requests.
max_requests = 1000 if workers > 1 else 0   # 0 = recycling disabled
max_requests_jitter = 200 if workers > 1 else 0
# Long-running SSE streams need a generous request timeout. This was
# written `600 if USE_GPU else 600` — a ternary with identical branches,
# which reads as a deliberate GPU/CPU distinction and is not one.
timeout = 600  # 10 minutes, both CPU and GPU
graceful_timeout = 60  # Increased for graceful shutdown of long-running requests
keepalive = 10  # Increased keepalive for better connection reuse

# =====================================================
# Process Naming
# =====================================================
proc_name = "face-recognition-service"

# =====================================================
# Logging
# =====================================================
# ROOT-CAUSE FIX: access/error logs previously went to FILES ONLY
# (/var/log/face-recognition/{access,error}.log), so HTTP access lines and
# uvicorn/gunicorn lifecycle output were invisible in `docker logs`.
# "-" sends them to stdout/stderr -> the container's log stream. The
# application's own logs go to stdout via utils/logging.py (which also keeps
# a bounded rotating file copy in LOG_DIR for persistence).
loglevel = settings.LOG_LEVEL.lower()

accesslog = "-"   # stdout -> Docker logs
errorlog = "-"    # stderr -> Docker logs
# Worker stdout/stderr (print(), tracebacks) already reach Docker because the
# server runs in the foreground (daemon=False) and workers inherit stdio.
# capture_output=True is only meaningful when errorlog is a FILE — combined
# with errorlog="-" it can loop output back into itself, so keep it off.
capture_output = False
enable_stdio_inheritance = True

# Access log format
access_log_format = (
    '%(h)s %(l)s %(u)s %(t)s "%(r)s" %(s)s %(b)s "%(f)s" "%(a)s" '
    '%(D)s %(p)s'
)
# Format explanation:
# %(h)s - Remote address
# %(l)s - '-'
# %(u)s - User name
# %(t)s - Date of the request
# %(r)s - Status line (e.g. GET / HTTP/1.1)
# %(s)s - Status
# %(b)s - Response length
# %(f)s - Referer
# %(a)s - User agent
# %(D)s - Time to serve request in microseconds
# %(p)s - Process ID

# =====================================================
# Server Mechanics
# =====================================================
daemon = False  # Run in foreground (systemd handles daemonization)
pidfile = None
worker_tmp_dir = "/tmp"  # Temporary directory for worker processes
umask = 0o022
user = None  # Run as current user (set in systemd)
group = None
tmp_upload_dir = None

# =====================================================
# SSL (if needed)
# =====================================================
# keyfile = "/path/to/keyfile.pem"
# certfile = "/path/to/certfile.pem"
# ca_certs = "/path/to/ca_certs.pem"
# cert_reqs = 0  # SSL_CERT_NONE
# ssl_version = 2  # TLS

# =====================================================
# Security
# =====================================================
limit_request_line = 4096
limit_request_fields = 100
limit_request_field_size = 8190

# =====================================================
# Development Settings
# =====================================================
reload = False  # Auto-reload on code changes (set to True for development)
reload_engine = "auto"
reload_extra_files = []

# =====================================================
# Server Performance
# =====================================================
# Preload application before forking workers
# This saves memory and speeds up worker spawning
# For GPU scenarios, preloading helps share GPU resources efficiently
preload_app = True if USE_GPU else False

# Reuse port for better performance (Linux 3.9+)
reuse_port = True if hasattr(os, 'SO_REUSEPORT') else False

# =====================================================
# Worker Lifecycle Hooks
# =====================================================

def _log_configuration_summary():
    """Emit the startup banner through the central logger.

    Called from on_starting, NOT at module import. Importing this file is
    how tooling introspects the configuration (tests/test_config_contract.py
    execs it and parses stdout), and the QueueListener writes from a
    THREAD — so logging at import made this module's stdout arrive in a
    non-deterministic order and corrupted anything parsing it.
    """
    from utils.logging import setup_logging
    setup_logging(log_to_file=True)
    _conf_log = logging.getLogger("gunicorn.conf")
    _conf_log.info("\n" + "=" * 70)
    _conf_log.info("📋 Gunicorn Configuration Loaded")
    _conf_log.info("=" * 70)
    _conf_log.info(f"Workers: {workers} (based on {cpu_count} CPU cores)")
    _conf_log.info(f"Worker Class: {worker_class}")
    _conf_log.info(f"Bind Address: {bind}")
    _conf_log.info(f"Timeout: {timeout}s")
    _conf_log.info(f"Max Requests per Worker: {max_requests}")
    _conf_log.info(f"Access Log: {accesslog}")
    _conf_log.info(f"Error Log: {errorlog}")
    _conf_log.info(f"Log Level: {loglevel}")
    _conf_log.info(f"Preload App: {preload_app}")
    _conf_log.info("=" * 70 + "\n")


def on_starting(server):
    """
    Called just before the master process is initialized.
    """

    _log_configuration_summary()
    # Second layer of the fail-closed configuration gate. docker-entrypoint.sh
    # is the primary one, but docker-compose.gpu.yml overrides `entrypoint:`
    # and would otherwise skip it entirely. Runs once in the master, before any
    # worker is forked, so sys.exit here aborts the whole process rather than
    # triggering an endless worker-respawn loop.
    import sys

    try:
        from backend.security.config_guard import ConfigGuardError, enforce

        enforce()
    except ConfigGuardError as exc:
        sys.stderr.write(exc.report())
        sys.stderr.write("Refusing to start: configuration is unsafe for production.\n")
        sys.exit(78)  # EX_CONFIG
    except Exception as exc:  # guard itself is broken — do not mask it
        sys.stderr.write(f"Configuration preflight could not run: {type(exc).__name__}: {exc}\n")
        sys.exit(78)

    print("=" * 70)
    print("🚀 Starting Face Recognition Service v4.0")
    print("=" * 70)
    print(f"📊 Configuration:")
    print(f"   - Workers: {workers}")
    print(f"   - Worker Class: {worker_class}")
    print(f"   - Bind: {bind}")
    print(f"   - Timeout: {timeout}s")
    print(f"   - Graceful Timeout: {graceful_timeout}s")
    print(f"   - Max Requests: {max_requests}")
    print(f"   - Preload App: {preload_app}")
    print("=" * 70)


def on_reload(server):
    """
    Called when a worker is reloaded.
    """
    print("🔄 Reloading workers...")


def when_ready(server):
    """
    Called just after the server is started.
    """
    print("=" * 70)
    print("✅ Face Recognition Service is ready!")
    print(f"🌐 Listening on: {bind}")
    print(f"👷 Active workers: {workers}")
    print("=" * 70)


def _worker_context(worker):
    """One-line diagnostic context for lifecycle events.

    Every worker lifecycle line carries the same fields so the NEXT unexplained
    shutdown can be attributed from the log alone — the 2026-07-28 incident
    took log archaeology to trace to max_requests recycling because the events
    logged nothing but a pid.
    """
    import os as _os
    import threading as _threading
    parts = [f"pid={worker.pid}", f"ppid={_os.getppid()}"]
    try:
        parts.append(f"age_s={int(worker.age)}")
    except Exception:
        pass
    try:
        parts.append(f"requests_handled={worker.nr}")
    except Exception:
        pass
    parts.append(f"threads={_threading.active_count()}")
    try:
        import resource
        rss_kb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        parts.append(f"peak_rss_mb={rss_kb // 1024}")
    except Exception:
        pass
    return " ".join(parts)


def worker_int(worker):
    """Called when a worker receives SIGINT or SIGQUIT."""
    print(f"⚠️  Worker interrupted signal=SIGINT/SIGQUIT {_worker_context(worker)}")


def worker_abort(worker):
    """Called when a worker receives SIGABRT — almost always the gunicorn
    heartbeat timeout, i.e. the event loop was blocked past `timeout` seconds.
    This is the line to look for when a worker dies without a graceful
    shutdown sequence."""
    print(f"❌ Worker ABORTED signal=SIGABRT probable_cause=event_loop_blocked_past_timeout "
          f"{_worker_context(worker)}")


def pre_fork(server, worker):
    """
    Called before a worker is forked.
    """
    pass


def post_fork(server, worker):
    """
    Called after a worker is forked.

    Re-arms logging in the child. A QueueListener runs in a THREAD, and threads
    do not survive fork(). With preload_app=True (this config enables it for
    GPU) setup_logging() runs in the master, so each worker inherits a listener
    object whose thread is dead — the idempotence guard then short-circuits and
    the worker logs NOTHING, silently. Rebuilding it here is what keeps worker
    logs flowing to stdout and the rotating file.
    """
    from utils.logging import reinitialize_after_fork
    reinitialize_after_fork()
    logging.getLogger(__name__).info("Worker %s spawned", worker.pid)


def post_worker_init(worker):
    """
    Called after a worker has initialized the application.
    """
    print(f"✅ Worker {worker.pid} initialized and ready")


def worker_exit(server, worker):
    """Called in the worker process as it exits (any reason)."""
    print(f"👋 Worker exiting {_worker_context(worker)} "
          f"(recycle_note: with max_requests>0 a planned recycle also passes here)")


def child_exit(server, worker):
    """
    Called after a worker has been exited, in the master process.
    """
    pass


def nworkers_changed(server, new_value, old_value):
    """
    Called when the number of workers changes.
    """
    print(f"📊 Workers changed: {old_value} → {new_value}")


def pre_exec(server):
    """
    Called before a new master process is forked.
    """
    print("🔄 Preparing to exec new master process...")


def pre_request(worker, req):
    """
    Called before a worker processes a request.
    """
    # Log webhook requests for monitoring
    if "/webhook/" in req.path:
        worker.log.debug(f"Processing webhook: {req.path}")


def post_request(worker, req, environ, resp):
    """
    Called after a worker processes a request.
    """
    pass


# =====================================================
# Environment Variables
# =====================================================
raw_env = [
    f"WORKERS={workers}",
    f"WORKER_CLASS={worker_class}",
]

# =====================================================
# Paste Deployment Configuration
# =====================================================
raw_paste_global_conf = []

# =====================================================
# Server Hooks
# =====================================================
# You can define custom server hooks here

# =====================================================
# Debugging
# =====================================================
# Set to True to enable additional debugging
# spew = False

# =====================================================
# Configuration Summary
# =====================================================

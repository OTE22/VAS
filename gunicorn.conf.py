"""
Gunicorn configuration for Face Recognition Service v4.0
Optimized for 100+ concurrent requests with high reliability
"""
import multiprocessing
import os

# =====================================================
# Server Socket
# =====================================================
# Import settings from central config
try:
    from config import settings
    bind = f"{settings.HOST}:{settings.PORT}"
except ImportError:
    # Fallback to environment variables if config not available
    bind = os.getenv("HOST", "0.0.0.0") + ":" + os.getenv("PORT", "8000")
backlog = 2048  # Maximum number of pending connections

# =====================================================
# Worker Processes
# =====================================================
# Optimized for 50+ cameras and 20 concurrent users
# GPU scenarios can handle more workers efficiently
cpu_count = multiprocessing.cpu_count()

# Check if GPU is available - Use central config
try:
    USE_GPU = settings.USE_GPU
    workers = settings.WORKERS if settings.WORKERS > 0 else (max(16, int(cpu_count * 2) + 10) if USE_GPU else max(8, int(cpu_count * 1.5)))
except NameError:
    # Fallback if settings not imported
    USE_GPU = os.getenv("USE_GPU", "false").lower() == "true"
    if USE_GPU:
        workers = int(os.getenv("WORKERS", max(16, int(cpu_count * 2) + 10)))
    else:
        workers = int(os.getenv("WORKERS", max(8, int(cpu_count * 1.5))))

if USE_GPU:
    print(f"🚀 GPU mode: Using {workers} workers (CPU cores: {cpu_count})")
else:
    print(f"💻 CPU mode: Using {workers} workers (CPU cores: {cpu_count})")

worker_class = "uvicorn.workers.UvicornWorker"
# Increased for 50+ cameras and 20 concurrent users
worker_connections = 2000 if USE_GPU else 1000  # GPU can handle more connections
threads = 1  # Threads per worker (keep at 1 for async)

# Worker lifecycle
# Optimized for high-throughput scenarios
max_requests = 1000 if USE_GPU else 500  # GPU workers can handle more requests
max_requests_jitter = 200  # Reduced jitter for more predictable restarts
# Increased timeout for long-running SSE streams (up to 10 minutes)
timeout = 600 if USE_GPU else 600  # 10 minutes for streaming requests
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
try:
    loglevel = settings.LOG_LEVEL.lower()
except NameError:
    loglevel = os.getenv("LOG_LEVEL", "info").lower()

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

def on_starting(server):
    """
    Called just before the master process is initialized.
    """
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


def worker_int(worker):
    """
    Called when a worker receives SIGINT or SIGQUIT.
    """
    print(f"⚠️  Worker {worker.pid} interrupted by user")


def worker_abort(worker):
    """
    Called when a worker receives SIGABRT.
    Usually happens when the worker times out.
    """
    print(f"❌ Worker {worker.pid} aborted (timeout or crash)")
    print(f"   Worker age: {worker.age}")


def pre_fork(server, worker):
    """
    Called before a worker is forked.
    """
    pass


def post_fork(server, worker):
    """
    Called after a worker is forked.
    """
    print(f"👷 Worker {worker.pid} spawned")


def post_worker_init(worker):
    """
    Called after a worker has initialized the application.
    """
    print(f"✅ Worker {worker.pid} initialized and ready")


def worker_exit(server, worker):
    """
    Called when a worker is exited.
    """
    print(f"👋 Worker {worker.pid} exited gracefully")


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
print("\n" + "=" * 70)
print("📋 Gunicorn Configuration Loaded")
print("=" * 70)
print(f"Workers: {workers} (based on {cpu_count} CPU cores)")
print(f"Worker Class: {worker_class}")
print(f"Bind Address: {bind}")
print(f"Timeout: {timeout}s")
print(f"Max Requests per Worker: {max_requests}")
print(f"Access Log: {accesslog}")
print(f"Error Log: {errorlog}")
print(f"Log Level: {loglevel}")
print(f"Preload App: {preload_app}")
print("=" * 70 + "\n")

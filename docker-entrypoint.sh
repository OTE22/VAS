#!/bin/bash
set -e

# Fix permissions for mounted volumes (especially on Windows)
# This runs as root before switching to appuser

echo "🔧 Fixing permissions for database and storage directories..."

# Ensure directories exist (unified storage structure)
mkdir -p /app/database/face_database
mkdir -p /app/storage
mkdir -p /app/storage/faces  # Known faces (uploaded persons)
mkdir -p /var/log/face-recognition
# ChromaDB downloads its ONNX embedder here on first use. Previously created by
# an `entrypoint:` override in docker-compose.gpu.yml, which had the side effect
# of skipping this script entirely.
mkdir -p /home/appuser/.cache/chroma/onnx_models
chown -R 1000:1000 /home/appuser/.cache/chroma 2>/dev/null || true
chmod -R 755 /home/appuser/.cache/chroma 2>/dev/null || true

# Fix permissions - make writable by all (safe in container)
# Use more aggressive approach for Windows bind mounts
chmod -R 777 /app/database /app/storage /var/log/face-recognition 2>/dev/null || true

# Change ownership to appuser (UID 1000)
chown -R appuser:appuser /app/database /app/storage /var/log/face-recognition 2>/dev/null || true

# Also ensure parent directories are writable
chmod 777 /app/database 2>/dev/null || true
chmod 777 /app/storage 2>/dev/null || true

echo "✅ Permissions fixed!"

# =====================================================
# Fail-closed configuration preflight
# =====================================================
# Runs once, in PID 1, before anything binds a port or opens a connection.
# This is the primary gate: a check inside the application lifespan would run
# per gunicorn worker, and the master would respawn the dying workers forever
# while `restart: unless-stopped` kept the container nominally "up".
#
# Exits 78 (EX_CONFIG) when production configuration is unsafe. No-op when
# ENVIRONMENT is not production. Set CONFIG_PREFLIGHT=0 to bypass — intended
# only for one-shot maintenance commands, never for serving traffic.
if [ "${CONFIG_PREFLIGHT:-1}" = "1" ]; then
    echo "🔒 Configuration preflight (ENVIRONMENT=${ENVIRONMENT:-unset})..."
    if python -m backend.security.config_guard; then
        echo "✅ Configuration preflight passed"
    else
        status=$?
        echo "❌ Configuration preflight FAILED (exit ${status}) — refusing to start." >&2
        exit "$status"
    fi
fi

# For Windows Docker, running as root is acceptable since we're in a container
# If gosu fails, we'll run as root (which is fine for containerized apps on Windows)
if command -v gosu >/dev/null 2>&1 && gosu appuser true 2>/dev/null; then
    echo "✅ Switching to appuser..."
    exec gosu appuser "$@"
else
    echo "⚠️  Running as root (gosu unavailable or failed - acceptable in Windows Docker containers)"
    exec "$@"
fi


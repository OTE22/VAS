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

# Fix permissions - make writable by all (safe in container)
# Use more aggressive approach for Windows bind mounts
chmod -R 777 /app/database /app/storage /var/log/face-recognition 2>/dev/null || true

# Change ownership to appuser (UID 1000)
chown -R appuser:appuser /app/database /app/storage /var/log/face-recognition 2>/dev/null || true

# Also ensure parent directories are writable
chmod 777 /app/database 2>/dev/null || true
chmod 777 /app/storage 2>/dev/null || true

echo "✅ Permissions fixed!"

# For Windows Docker, running as root is acceptable since we're in a container
# If gosu fails, we'll run as root (which is fine for containerized apps on Windows)
if command -v gosu >/dev/null 2>&1 && gosu appuser true 2>/dev/null; then
    echo "✅ Switching to appuser..."
    exec gosu appuser "$@"
else
    echo "⚠️  Running as root (gosu unavailable or failed - acceptable in Windows Docker containers)"
    exec "$@"
fi


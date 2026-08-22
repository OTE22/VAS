#!/bin/bash
set -e

# Fix permissions for mounted volumes (especially on Windows)
# This runs as root before switching to appuser

echo "🔧 Fixing permissions for database and storage directories..."

# Ensure directories exist (unified storage structure).
#
# STORAGE_DIR is the only configurable root; the gallery, the upload staging
# area and the debug stores are DERIVED from it in config.py and cannot be set
# independently. This used to read ${FACES_DIR:-/app/storage/faces}, which was
# worse than a duplicated default: once FACES_DIR stopped being a setting, that
# expansion always produced the literal fallback, so a deployment with a
# non-default STORAGE_DIR had its permissions fixed on a directory the
# application never writes to.
#
# The shell cannot import config.py, so the derivation is spelled out here and
# tests/test_deployment_surface.py holds it to the same layout.
STORAGE_ROOT="${STORAGE_DIR:-/app/storage}"
mkdir -p "$STORAGE_ROOT"
mkdir -p "$STORAGE_ROOT/faces"                    # known faces (enrolled people)
mkdir -p "$STORAGE_ROOT/faces/.incoming"          # UPLOAD_TEMP_DIR: same
                                                  # filesystem as faces/, so the
                                                  # final os.replace is atomic
mkdir -p "$STORAGE_ROOT/pending"                  # PENDING_UPLOAD_DIR: uploads
                                                  # awaiting an admin's identity
                                                  # decision. Outside faces/,
                                                  # which holds identity UUID
                                                  # folders and nothing else
mkdir -p "$STORAGE_ROOT/debug/webhook_images"
mkdir -p "$STORAGE_ROOT/debug/cropped"
mkdir -p /var/log/face-recognition
# The cache ROOT, not just one subdirectory.
#
# This script runs as root, so `mkdir -p .../chroma/onnx_models` created the
# parent `/home/appuser/.cache` owned by root:root 755. Chowning only `chroma`
# left the parent unwritable by appuser, so HuggingFace could not create its
# sibling `huggingface/` directory and SentenceTransformer failed with
# PermissionError. `generate_query_embedding` catches that and returns None, so
# the chatbot's semantic query-history search was silently disabled — no crash,
# no visible symptom, one ERROR line.
mkdir -p /home/appuser/.cache
mkdir -p "${HF_HOME:-/home/appuser/.cache/huggingface}"
# ChromaDB downloads its ONNX embedder here on first use. Previously created by
# an `entrypoint:` override in docker-compose.gpu.yml, which had the side effect
# of skipping this script entirely.
mkdir -p /home/appuser/.cache/chroma/onnx_models
chown -R 1000:1000 /home/appuser/.cache 2>/dev/null || true
chmod -R 755 /home/appuser/.cache 2>/dev/null || true

# Fix permissions - make writable by all (safe in container)
# Use more aggressive approach for Windows bind mounts
chmod -R 777 /app/database "$STORAGE_ROOT" /var/log/face-recognition 2>/dev/null || true

# Change ownership to appuser (UID 1000)
chown -R appuser:appuser /app/database "$STORAGE_ROOT" /var/log/face-recognition 2>/dev/null || true

# ML registry artifacts + Parquet datasets (ML_ARTIFACT_DIR, /app/models/ml).
# A named volume mounted here is created by Docker as root; the registry
# refuses to write anywhere else, so the directory must be appuser's.
mkdir -p /app/models/ml/candidates /app/models/ml/datasets 2>/dev/null || true
chown -R appuser:appuser /app/models/ml 2>/dev/null || true

# Also ensure parent directories are writable
chmod 777 /app/database 2>/dev/null || true
chmod 777 "$STORAGE_ROOT" 2>/dev/null || true

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

# Model cache must be writable BY THE USER THAT WILL RUN THE APP.
#
# Checked unconditionally, as the current user, because compose pins
# `user: "1000:1000"` — this script usually runs as appuser already and the
# gosu branch below never executes. Checked here rather than at first use
# because the downstream failure is silent: SentenceTransformer raises
# PermissionError, generate_query_embedding catches it and returns None, and
# semantic query-history search simply never matches anything again.
HF_CACHE_DIR="${HF_HOME:-/home/appuser/.cache/huggingface}"
mkdir -p "$HF_CACHE_DIR" 2>/dev/null || true
if [ -w /home/appuser/.cache ] && [ -w "$HF_CACHE_DIR" ]; then
    echo "✅ Model cache writable as $(id -un): $HF_CACHE_DIR"
else
    echo "❌ Model cache is NOT writable as $(id -un): /home/appuser/.cache," \
         "$HF_CACHE_DIR. Sentence-transformer downloads will fail and semantic" \
         "query-history search will silently return no matches." >&2
    ls -ld /home/appuser/.cache "$HF_CACHE_DIR" >&2 2>/dev/null || true
    exit 78   # EX_CONFIG, matching the configuration preflight above
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


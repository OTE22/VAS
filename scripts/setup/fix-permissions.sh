#!/bin/bash
# Fix host-side permissions for the bind-mounted directories and the ChromaDB
# cache volume. Run on the host before starting the containers.
#
# The container runs as uid 1000 (appuser), pinned by `user: "1000:1000"` in
# every compose file, so anything it must write has to be writable by 1000.

set -uo pipefail

# The repository root — NOT the directory holding this script. This used to be
# `dirname $0`, i.e. scripts/setup/, so it created scripts/setup/database,
# scripts/setup/storage and scripts/setup/logs and chmod'ed those, leaving the
# real directories untouched.
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

echo "🔧 Fixing permissions for Docker volumes..."
echo "   repository root: $ROOT"

mkdir -p "$ROOT/database/face_database" "$ROOT/storage" "$ROOT/logs"

if [[ "$OSTYPE" != "msys" && "$OSTYPE" != "win32" && "$OSTYPE" != "cygwin" ]]; then
    for dir in database storage logs; do
        echo "Setting permissions for $dir..."
        chmod -R 775 "$ROOT/$dir" 2>/dev/null || sudo chmod -R 775 "$ROOT/$dir"
        # 775 + ownership, not 777: the container writes as uid 1000, so giving
        # it ownership is sufficient and world-writable buys nothing but risk.
        chown -R 1000:1000 "$ROOT/$dir" 2>/dev/null || sudo chown -R 1000:1000 "$ROOT/$dir"
    done
else
    echo "Windows detected — Docker Desktop handles bind-mount permissions."
    echo "If problems persist, confirm Docker Desktop can access $ROOT."
fi

echo ""
echo "🔧 Fixing the ChromaDB cache volume..."

# Resolve the volume name from the compose project rather than hardcoding it.
# This used to be a literal `face_detector_chromadb_cache`, which is now an
# ORPHANED volume from an older project layout: the development stack declares
# `name: face_detector_dev`, so the live volume is
# face_detector_dev_chromadb_cache. The hardcoded form silently "fixed"
# permissions on a volume nothing mounts.
COMPOSE_FILE="$ROOT/docker/docker-compose.cpu.yml"
PROJECT="$(grep -m1 '^name:' "$COMPOSE_FILE" 2>/dev/null | awk '{print $2}')"
if [ -z "${PROJECT:-}" ]; then
    echo "⚠️  Could not read the project name from $COMPOSE_FILE — skipping."
    echo "   (Every compose file must declare `name:`; without it dev and"
    echo "    production share volumes.)"
    exit 1
fi
VOLUME="${PROJECT}_chromadb_cache"

if docker volume inspect "$VOLUME" >/dev/null 2>&1; then
    docker run --rm --user root -v "${VOLUME}:/data" alpine \
        sh -c "mkdir -p /data/onnx_models && chown -R 1000:1000 /data && chmod -R 775 /data" \
        && echo "✅ $VOLUME" \
        || echo "⚠️  Could not fix $VOLUME — stop the containers and retry."
else
    echo "ℹ️  $VOLUME does not exist yet; it is created on first start."
fi

echo ""
echo "✅ Permissions fixed."
echo ""
echo "Start the containers with:"
echo "  docker compose -f docker/docker-compose.cpu.yml up -d"

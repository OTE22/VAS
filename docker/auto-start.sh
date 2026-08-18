#!/bin/bash
# Start the DEVELOPMENT stack, using the GPU if this machine can.
#
#   ./docker/auto-start.sh              # foreground
#   ./docker/auto-start.sh -d           # detached
#   ./docker/auto-start.sh -d --build   # rebuild first
#
# This starts DEVELOPMENT only. Production is deliberately not automated here:
# it needs secrets, TLS material and database roles in a specific order, and a
# script that appears to do it in one step invites skipping them. See
# Docs/61_DEPLOYMENT_RUNBOOK.md.
#
# Replaces docker/start.sh, which was a second, less capable implementation of
# this same job. Two launchers drifted apart; one is enough.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$PROJECT_ROOT"

# The GPU file is an OVERRIDE layered on the CPU stack, never used alone: on
# its own it declares no database, no proxy and no dependencies. The project
# name comes from the `name:` key in docker-compose.cpu.yml, so both paths use
# the same volumes and therefore the same database.
BASE="-f docker/docker-compose.cpu.yml"
GPU_OVERRIDE="-f docker/docker-compose.gpu.yml"

echo "=========================================="
echo "  Face Recognition Service — development"
echo "=========================================="
echo ""

if ! docker info > /dev/null 2>&1; then
    echo "❌ Docker is not running. Start Docker Desktop or the daemon." >&2
    exit 1
fi

# `docker compose` (v2 plugin). The v1 `docker-compose` binary is not used:
# it ignores the top-level `name:` key that keeps the development and
# production stacks on separate volumes.
if ! docker compose version > /dev/null 2>&1; then
    echo "❌ The Docker Compose v2 plugin is required (\`docker compose\`)." >&2
    echo "   The legacy \`docker-compose\` binary ignores the project name key" >&2
    echo "   this project relies on to keep dev and production data apart." >&2
    exit 1
fi

# Shared webhook network (external): VMS -> nginx, alias face-webhook.
# Race-safe: a concurrent deployment may create it between our inspect and our
# create. That specific failure is fine IFF the network then exists; any other
# failure is fatal. Never blanket-ignore `docker network create` errors.
if ! docker network inspect webhook_integration > /dev/null 2>&1; then
    echo "Provisioning shared network: webhook_integration"
    if ! create_err=$(docker network create webhook_integration 2>&1 >/dev/null); then
        if ! docker network inspect webhook_integration > /dev/null 2>&1; then
            echo "ERROR: cannot create network webhook_integration: $create_err" >&2
            exit 1
        fi
        echo "webhook_integration was created concurrently by another deployment"
    fi
fi
echo "✅ Shared network webhook_integration present"

HAS_GPU=false
GPU_NAME=""
if command -v nvidia-smi > /dev/null 2>&1 && nvidia-smi > /dev/null 2>&1; then
    HAS_GPU=true
    GPU_NAME=$(nvidia-smi --query-gpu=name --format=csv,noheader | head -n1)
fi

# A GPU is not enough: the container runtime has to be able to hand it over.
HAS_NVIDIA_RUNTIME=false
if [ "$HAS_GPU" = true ] && docker info 2>/dev/null | grep -qi "nvidia"; then
    HAS_NVIDIA_RUNTIME=true
fi

if [ "$HAS_GPU" = true ] && [ "$HAS_NVIDIA_RUNTIME" = true ]; then
    echo "✅ GPU: $GPU_NAME (NVIDIA container runtime available)"
    echo "🚀 Starting CPU stack + GPU override"
    echo "   ALLOW_CPU_FALLBACK is false on this path: if CUDA is not usable the"
    echo "   API refuses to start rather than quietly running on the CPU."
    echo ""
    exec docker compose $BASE $GPU_OVERRIDE up "$@"
fi

if [ "$HAS_GPU" = true ]; then
    echo "⚠️  GPU present ($GPU_NAME) but the NVIDIA container runtime is not."
    echo "   Install the NVIDIA Container Toolkit, then re-run:"
    echo "     bash docker/verify-nvidia-docker.sh"
    echo "     Docs/04_SETUP_NVIDIA_DOCKER.md"
    echo "   Falling back to CPU."
else
    echo "ℹ️  No NVIDIA GPU detected — starting the CPU stack."
fi
echo ""
exec docker compose $BASE up "$@"

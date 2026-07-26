#!/bin/bash
# Automatic Docker startup script with GPU detection
# Usage: ./docker/auto-start.sh [docker-compose-args]
# Examples:
#   ./docker/auto-start.sh              # Start in foreground
#   ./docker/auto-start.sh -d           # Start in background (detached)
#   ./docker/auto-start.sh --build      # Force rebuild

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

cd "$PROJECT_ROOT"

echo "=========================================="
echo "  Face Recognition Service"
echo "  Automatic Docker Startup"
echo "=========================================="
echo ""

# Check if Docker is running
if ! docker info > /dev/null 2>&1; then
    echo "❌ Error: Docker is not running!"
    echo "   Please start Docker Desktop or Docker daemon"
    exit 1
fi

# Check for GPU
HAS_GPU=false
GPU_NAME=""
if command -v nvidia-smi &> /dev/null; then
    if nvidia-smi &> /dev/null; then
        HAS_GPU=true
        GPU_NAME=$(nvidia-smi --query-gpu=name --format=csv,noheader | head -n1)
    fi
fi

# Check for nvidia-docker runtime
HAS_NVIDIA_DOCKER=false
if [ "$HAS_GPU" = true ]; then
    # Check for NVIDIA Container Toolkit (replaces nvidia-docker2)
    if docker info 2>/dev/null | grep -q "nvidia"; then
        HAS_NVIDIA_DOCKER=true
    fi
fi

# Determine which configuration to use
if [ "$HAS_GPU" = true ] && [ "$HAS_NVIDIA_DOCKER" = true ]; then
    echo "✅ NVIDIA GPU detected: $GPU_NAME"
    echo "✅ NVIDIA Docker runtime available"
    echo "🚀 Starting with GPU support..."
    echo ""
    echo "📋 Configuration:"
    echo "   - Workers: 16"
    echo "   - Queue Workers: 50"
    echo "   - Max Queue: 10,000"
    echo "   - GPU Acceleration: Enabled"
    echo ""
    docker-compose -f docker/docker-compose.gpu.yml up --build "$@"
    exit 0
elif [ "$HAS_GPU" = true ] && [ "$HAS_NVIDIA_DOCKER" = false ]; then
    echo "⚠️  GPU detected ($GPU_NAME) but NVIDIA Container Toolkit not available"
    echo "   To enable GPU support, install NVIDIA Container Toolkit:"
    echo "   See: Docs/SETUP_NVIDIA_DOCKER.md"
    echo "   Or run: sudo apt-get install nvidia-container-toolkit"
    echo ""
    echo "💻 Falling back to CPU mode..."
    echo ""
elif [ "$HAS_GPU" = false ]; then
    echo "ℹ️  No NVIDIA GPU detected"
    echo "💻 Starting with CPU support..."
    echo ""
fi

echo "📋 Configuration:"
echo "   - Workers: 8"
echo "   - Queue Workers: 15"
echo "   - Max Queue: 2,000"
echo "   - GPU Acceleration: Disabled"
echo ""

docker-compose -f docker/docker-compose.cpu.yml up --build "$@"


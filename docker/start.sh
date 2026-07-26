#!/bin/bash
# Automatic Docker startup script
# Detects GPU and starts appropriate Docker Compose configuration

set -e

echo "=========================================="
echo "Face Recognition Service - Docker Startup"
echo "=========================================="
echo ""

# Check for GPU
if command -v nvidia-smi &> /dev/null; then
    if nvidia-smi &> /dev/null; then
        echo "✅ NVIDIA GPU detected"
        echo "🚀 Starting with GPU support..."
        docker-compose -f docker/docker-compose.gpu.yml up --build "$@"
        exit 0
    fi
fi

echo "ℹ️  No GPU detected"
echo "💻 Starting with CPU support..."
docker-compose -f docker/docker-compose.cpu.yml up --build "$@"


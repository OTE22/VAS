#!/bin/bash

# Production Startup Script for Face Recognition Service
# Handles 100+ concurrent requests, runs 24/7

set -e

echo "🚀 Starting Face Recognition Service (Production Mode)"

# Check if .env exists
if [ ! -f .env ]; then
    echo "⚠️  .env file not found. Copying from .env.example..."
    cp .env.example .env
    echo "📝 Please edit .env file with your configuration"
    exit 1
fi

# Check if weights exist
if [ ! -f "weights/det_10g.onnx" ] || [ ! -f "weights/w600k_r50.onnx" ]; then
    echo "⚠️  Model weights not found. Downloading..."
    bash download.sh
fi

# Create necessary directories
mkdir -p storage/{logs,images}
mkdir -p database/face_database

# Load environment variables
export $(cat .env | grep -v '^#' | xargs)

# Install dependencies if needed
if [ ! -d "venv" ]; then
    echo "📦 Creating virtual environment..."
    python3 -m venv venv
    source venv/bin/activate
    pip install --upgrade pip
    # Install dependencies based on GPU availability
    if command -v nvidia-smi &> /dev/null && nvidia-smi &> /dev/null; then
        pip install -r requirements-gpu.txt
    else
        pip install -r requirements-cpu.txt
    fi
else
    source venv/bin/activate
fi

# Database schema: Alembic only (the application never runs create_all;
# startup refuses a database that is not at the code's migration head).
echo "🗄️  Applying database migrations (alembic upgrade head)..."
python -m backend.utils.migrations --upgrade-head || { echo "❌ migrations failed — refusing to start"; exit 1; }

# Start the application
echo "🔥 Starting Face Recognition API..."
echo "   - Host: ${HOST:-0.0.0.0}"
echo "   - Port: ${PORT:-8000}"
echo "   - Workers: ${WORKERS:-4}"
echo "   - Queue Workers: ${QUEUE_WORKERS:-10}"
echo "   - Max Concurrent: ${MAX_CONCURRENT_REQUESTS:-100}"

# Run with uvicorn for production
uvicorn backend.main:app \
    --host ${HOST:-0.0.0.0} \
    --port ${PORT:-8000} \
    --workers ${WORKERS:-4} \
    --log-level ${LOG_LEVEL:-info} \
    --access-log \
    --use-colors

# Alternative: Run with gunicorn for even better production performance
# gunicorn backend.main:app \
#     --workers ${WORKERS:-4} \
#     --worker-class uvicorn.workers.UvicornWorker \
#     --bind ${HOST:-0.0.0.0}:${PORT:-8000} \
#     --timeout 120 \
#     --graceful-timeout 30 \
#     --keep-alive 5 \
#     --max-requests 1000 \
#     --max-requests-jitter 100 \
#     --access-logfile storage/logs/access.log \
#     --error-logfile storage/logs/error.log \
#     --log-level ${LOG_LEVEL:-info}

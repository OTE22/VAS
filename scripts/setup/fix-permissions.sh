#!/bin/bash
# Fix permissions for Docker volumes
# Run this script on the host before starting containers

echo "🔧 Fixing permissions for Docker volumes..."

# Get the current directory
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"

# Create directories if they don't exist
mkdir -p "$SCRIPT_DIR/database/face_database"
mkdir -p "$SCRIPT_DIR/storage"
mkdir -p "$SCRIPT_DIR/logs"

# Fix permissions (UID 1000 = appuser in container)
# On Linux/Mac:
if [[ "$OSTYPE" != "msys" && "$OSTYPE" != "win32" ]]; then
    echo "Setting permissions for database directory..."
    chmod -R 777 "$SCRIPT_DIR/database" 2>/dev/null || sudo chmod -R 777 "$SCRIPT_DIR/database"
    chmod -R 777 "$SCRIPT_DIR/storage" 2>/dev/null || sudo chmod -R 777 "$SCRIPT_DIR/storage"
    chmod -R 777 "$SCRIPT_DIR/logs" 2>/dev/null || sudo chmod -R 777 "$SCRIPT_DIR/logs"
    
    # Try to change ownership to UID 1000 (if user exists)
    if id -u 1000 >/dev/null 2>&1; then
        echo "Setting ownership to UID 1000..."
        chown -R 1000:1000 "$SCRIPT_DIR/database" 2>/dev/null || sudo chown -R 1000:1000 "$SCRIPT_DIR/database"
        chown -R 1000:1000 "$SCRIPT_DIR/storage" 2>/dev/null || sudo chown -R 1000:1000 "$SCRIPT_DIR/storage"
        chown -R 1000:1000 "$SCRIPT_DIR/logs" 2>/dev/null || sudo chown -R 1000:1000 "$SCRIPT_DIR/logs"
    fi
else
    # Windows - permissions are handled differently
    echo "Windows detected - permissions should be handled by Docker Desktop"
    echo "If issues persist, ensure Docker Desktop has access to the directory"
fi

echo ""
echo "🔧 Fixing ChromaDB cache volume permissions..."
# Fix ChromaDB cache Docker volume permissions
docker run --rm --user root -v face_detector_chromadb_cache:/data alpine sh -c "mkdir -p /data/onnx_models && chown -R 1000:1000 /data && chmod -R 777 /data" 2>/dev/null || echo "⚠️  Could not fix ChromaDB cache volume (container may need to be stopped first)"

echo "✅ Permissions fixed!"
echo ""
echo "You can now start the containers with:"
echo "  docker-compose up -d"


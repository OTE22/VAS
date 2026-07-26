#!/bin/bash
# Run Database Migrations in Docker Container
# ============================================
# This script checks migration status and runs all pending migrations

set -e

CONTAINER_NAME="face_recognition_api"
ALEMBIC_DIR="/app/alembic"
VERSIONS_DIR="$ALEMBIC_DIR/versions"

echo "=========================================="
echo "🔍 Checking Docker Container Status"
echo "=========================================="

# Check if container is running
if ! docker ps --format '{{.Names}}' | grep -q "^${CONTAINER_NAME}$"; then
    echo "❌ Container '${CONTAINER_NAME}' is not running!"
    echo "   Please start it with: docker-compose up -d"
    exit 1
fi

echo "✅ Container '${CONTAINER_NAME}' is running"
echo ""

# Check if migration file exists in container
echo "=========================================="
echo "📋 Checking Migration Files in Container"
echo "=========================================="

MIGRATION_FILES=$(docker exec ${CONTAINER_NAME} ls -1 ${VERSIONS_DIR}/ 2>/dev/null | grep '\.py$' || echo "")

if [ -z "$MIGRATION_FILES" ]; then
    echo "⚠️  No migration files found in container"
    echo "   Copying migration files..."
    
    # Copy migration files
    docker cp alembic/versions/001_add_pgvector_embedding_column.py ${CONTAINER_NAME}:${VERSIONS_DIR}/
    docker cp alembic/versions/002_add_pipeline_coordinates.py ${CONTAINER_NAME}:${VERSIONS_DIR}/
    
    echo "✅ Migration files copied"
else
    echo "✅ Found migration files:"
    echo "$MIGRATION_FILES" | while read file; do
        echo "   • $file"
    done
fi

echo ""
echo "=========================================="
echo "📍 Checking Current Migration Status"
echo "=========================================="

# Check current revision
CURRENT_REV=$(docker exec ${CONTAINER_NAME} bash -c "cd ${ALEMBIC_DIR} && python -m alembic current" 2>&1 | grep -v "^INFO:" | head -1 || echo "None")

if [ -z "$CURRENT_REV" ] || [ "$CURRENT_REV" = "None" ]; then
    echo "ℹ️  No migrations applied yet (fresh database)"
else
    echo "✅ Current revision: $CURRENT_REV"
fi

echo ""
echo "=========================================="
echo "📜 Migration History"
echo "=========================================="

docker exec ${CONTAINER_NAME} bash -c "cd ${ALEMBIC_DIR} && python -m alembic history --verbose" 2>&1 | grep -v "^INFO:" | head -20

echo ""
echo "=========================================="
echo "🔄 Running Migrations"
echo "=========================================="

# Run migrations
echo "Executing: alembic upgrade head"
echo ""

docker exec ${CONTAINER_NAME} bash -c "cd ${ALEMBIC_DIR} && python -m alembic upgrade head"

MIGRATION_EXIT_CODE=$?

echo ""
if [ $MIGRATION_EXIT_CODE -eq 0 ]; then
    echo "=========================================="
    echo "✅ Migrations Completed Successfully!"
    echo "=========================================="
    
    # Show final status
    echo ""
    echo "📍 Final Migration Status:"
    docker exec ${CONTAINER_NAME} bash -c "cd ${ALEMBIC_DIR} && python -m alembic current" 2>&1 | grep -v "^INFO:"
    
    echo ""
    echo "✅ Database is now up to date!"
else
    echo "=========================================="
    echo "❌ Migration Failed!"
    echo "=========================================="
    exit 1
fi


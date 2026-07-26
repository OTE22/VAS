#!/bin/bash
# Run Alembic Migrations Inside Docker Container
# ==============================================
# This script helps you run Alembic migrations inside the Docker container
# Works with alembic.ini located in the alembic/ folder

set -e

CONTAINER_NAME="face_recognition_api"
ALEMBIC_DIR="/app/alembic"
WORK_DIR="/app"

echo "=========================================="
echo "🔍 Checking Docker Container Status"
echo "=========================================="

# Check if container is running
if ! docker ps --format '{{.Names}}' | grep -q "^${CONTAINER_NAME}$"; then
    echo "❌ Container '${CONTAINER_NAME}' is not running!"
    echo "   Please start it with: docker-compose -f docker/docker-compose.cpu.yml up -d"
    exit 1
fi

echo "✅ Container '${CONTAINER_NAME}' is running"
echo ""

# Function to run alembic command
run_alembic() {
    local cmd=$1
    echo "Executing: alembic $cmd"
    echo ""
    docker exec -w ${ALEMBIC_DIR} ${CONTAINER_NAME} python -m alembic $cmd
}

# Parse command line arguments
case "${1:-help}" in
    current)
        echo "=========================================="
        echo "📍 Current Migration Status"
        echo "=========================================="
        run_alembic "current"
        ;;
    
    history)
        echo "=========================================="
        echo "📜 Migration History"
        echo "=========================================="
        run_alembic "history --verbose"
        ;;
    
    upgrade)
        echo "=========================================="
        echo "⬆️  Upgrading Database"
        echo "=========================================="
        if [ -n "$2" ]; then
            run_alembic "upgrade $2"
        else
            run_alembic "upgrade head"
        fi
        echo ""
        echo "✅ Upgrade completed!"
        ;;
    
    downgrade)
        echo "=========================================="
        echo "⬇️  Downgrading Database"
        echo "=========================================="
        if [ -n "$2" ]; then
            run_alembic "downgrade $2"
        else
            echo "❌ Please specify revision (e.g., -1 for one step back)"
            exit 1
        fi
        echo ""
        echo "✅ Downgrade completed!"
        ;;
    
    revision)
        echo "=========================================="
        echo "📝 Creating New Migration"
        echo "=========================================="
        if [ -n "$2" ]; then
            if [ "$2" = "--autogenerate" ] || [ "$2" = "-m" ]; then
                # Autogenerate migration
                if [ "$2" = "--autogenerate" ]; then
                    message="${3:-auto_migration}"
                    run_alembic "revision --autogenerate -m '$message'"
                else
                    message="$3"
                    run_alembic "revision -m '$message'"
                fi
            else
                # Manual revision with message
                run_alembic "revision -m '$2'"
            fi
        else
            echo "Usage: $0 revision -m 'migration message'"
            echo "   or: $0 revision --autogenerate -m 'migration message'"
            exit 1
        fi
        echo ""
        echo "✅ Migration file created!"
        echo "   Check: alembic/versions/"
        ;;
    
    show)
        echo "=========================================="
        echo "📋 Showing Migration Details"
        echo "=========================================="
        if [ -n "$2" ]; then
            run_alembic "show $2"
        else
            run_alembic "show head"
        fi
        ;;
    
    stamp)
        echo "=========================================="
        echo "🏷️  Stamping Database"
        echo "=========================================="
        if [ -n "$2" ]; then
            run_alembic "stamp $2"
        else
            echo "❌ Please specify revision to stamp"
            exit 1
        fi
        echo ""
        echo "✅ Database stamped!"
        ;;
    
    help|*)
        echo "=========================================="
        echo "📖 Alembic Migration Helper"
        echo "=========================================="
        echo ""
        echo "Usage: $0 <command> [options]"
        echo ""
        echo "Commands:"
        echo "  current                    Show current database revision"
        echo "  history                    Show migration history"
        echo "  upgrade [revision]         Upgrade to revision (default: head)"
        echo "  downgrade <revision>       Downgrade to revision (e.g., -1, base)"
        echo "  revision -m 'message'       Create new manual migration"
        echo "  revision --autogenerate -m 'message'  Auto-generate migration from models"
        echo "  show [revision]            Show migration details (default: head)"
        echo "  stamp <revision>           Stamp database with revision without running"
        echo "  help                       Show this help message"
        echo ""
        echo "Examples:"
        echo "  $0 current"
        echo "  $0 upgrade"
        echo "  $0 revision --autogenerate -m 'Add user query history tables'"
        echo "  $0 revision -m 'Add new column'"
        echo "  $0 downgrade -1"
        echo "  $0 show head"
        echo ""
        echo "Note: alembic.ini is located in /app/alembic/ inside the container"
        ;;
esac


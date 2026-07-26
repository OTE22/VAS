# Run Database Migrations in Docker Container
# ============================================
# This script checks migration status and runs all pending migrations

$CONTAINER_NAME = "face_recognition_api"
$ALEMBIC_DIR = "/app/alembic"
$VERSIONS_DIR = "$ALEMBIC_DIR/versions"

Write-Host "==========================================" -ForegroundColor Cyan
Write-Host "🔍 Checking Docker Container Status" -ForegroundColor Cyan
Write-Host "==========================================" -ForegroundColor Cyan
Write-Host ""

# Check if container is running
$containerRunning = docker ps --format '{{.Names}}' | Select-String -Pattern "^${CONTAINER_NAME}$" -Quiet

if (-not $containerRunning) {
    Write-Host "❌ Container '$CONTAINER_NAME' is not running!" -ForegroundColor Red
    Write-Host "   Please start it with: docker-compose up -d" -ForegroundColor Yellow
    exit 1
}

Write-Host "✅ Container '$CONTAINER_NAME' is running" -ForegroundColor Green
Write-Host ""

# Check if migration files exist in container
Write-Host "==========================================" -ForegroundColor Cyan
Write-Host "📋 Checking Migration Files in Container" -ForegroundColor Cyan
Write-Host "==========================================" -ForegroundColor Cyan
Write-Host ""

$migrationFiles = docker exec ${CONTAINER_NAME} ls -1 ${VERSIONS_DIR}/ 2>$null | Select-String -Pattern '\.py$'

if (-not $migrationFiles) {
    Write-Host "⚠️  No migration files found in container" -ForegroundColor Yellow
    Write-Host "   Copying migration files..." -ForegroundColor Yellow
    
    # Copy migration files
    docker cp alembic/versions/001_add_pgvector_embedding_column.py ${CONTAINER_NAME}:${VERSIONS_DIR}/
    docker cp alembic/versions/002_add_pipeline_coordinates.py ${CONTAINER_NAME}:${VERSIONS_DIR}/
    
    Write-Host "✅ Migration files copied" -ForegroundColor Green
} else {
    Write-Host "✅ Found migration files:" -ForegroundColor Green
    $migrationFiles | ForEach-Object {
        Write-Host "   • $_" -ForegroundColor Gray
    }
}

Write-Host ""
Write-Host "==========================================" -ForegroundColor Cyan
Write-Host "📍 Checking Current Migration Status" -ForegroundColor Cyan
Write-Host "==========================================" -ForegroundColor Cyan
Write-Host ""

# Check current revision
$currentRev = docker exec ${CONTAINER_NAME} bash -c "cd ${ALEMBIC_DIR} && python -m alembic current" 2>&1 | Select-String -Pattern "^(?!INFO:)" | Select-Object -First 1

if (-not $currentRev -or $currentRev -match "None") {
    Write-Host "ℹ️  No migrations applied yet (fresh database)" -ForegroundColor Yellow
} else {
    Write-Host "✅ Current revision: $currentRev" -ForegroundColor Green
}

Write-Host ""
Write-Host "==========================================" -ForegroundColor Cyan
Write-Host "📜 Migration History" -ForegroundColor Cyan
Write-Host "==========================================" -ForegroundColor Cyan
Write-Host ""

docker exec ${CONTAINER_NAME} bash -c "cd ${ALEMBIC_DIR} && python -m alembic history --verbose" 2>&1 | Select-String -Pattern "^(?!INFO:)" | Select-Object -First 20

Write-Host ""
Write-Host "==========================================" -ForegroundColor Cyan
Write-Host "🔄 Running Migrations" -ForegroundColor Cyan
Write-Host "==========================================" -ForegroundColor Cyan
Write-Host ""

# Run migrations
Write-Host "Executing: alembic upgrade head" -ForegroundColor Yellow
Write-Host ""

docker exec ${CONTAINER_NAME} bash -c "cd ${ALEMBIC_DIR} && python -m alembic upgrade head"

$migrationExitCode = $LASTEXITCODE

Write-Host ""
if ($migrationExitCode -eq 0) {
    Write-Host "==========================================" -ForegroundColor Green
    Write-Host "✅ Migrations Completed Successfully!" -ForegroundColor Green
    Write-Host "==========================================" -ForegroundColor Green
    
    # Show final status
    Write-Host ""
    Write-Host "📍 Final Migration Status:" -ForegroundColor Cyan
    docker exec ${CONTAINER_NAME} bash -c "cd ${ALEMBIC_DIR} && python -m alembic current" 2>&1 | Select-String -Pattern "^(?!INFO:)"
    
    Write-Host ""
    Write-Host "✅ Database is now up to date!" -ForegroundColor Green
} else {
    Write-Host "==========================================" -ForegroundColor Red
    Write-Host "❌ Migration Failed!" -ForegroundColor Red
    Write-Host "==========================================" -ForegroundColor Red
    exit 1
}


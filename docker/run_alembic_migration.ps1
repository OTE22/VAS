# Run Alembic Migrations Inside Docker Container (PowerShell)
# ===========================================================
# This script helps you run Alembic migrations inside the Docker container
# Works with alembic.ini located in the alembic/ folder

param(
    [Parameter(Position=0)]
    [string]$Command = "help",
    
    [Parameter(Position=1)]
    [string]$Arg1 = "",
    
    [Parameter(Position=2)]
    [string]$Arg2 = ""
)

$ErrorActionPreference = "Stop"
$CONTAINER_NAME = "face_recognition_api"
$ALEMBIC_DIR = "/app/alembic"
$WORK_DIR = "/app"

Write-Host "==========================================" -ForegroundColor Cyan
Write-Host "🔍 Checking Docker Container Status" -ForegroundColor Cyan
Write-Host "==========================================" -ForegroundColor Cyan

# Check if container is running
$containerRunning = docker ps --format '{{.Names}}' | Select-String -Pattern "^${CONTAINER_NAME}$"
if (-not $containerRunning) {
    Write-Host "❌ Container '$CONTAINER_NAME' is not running!" -ForegroundColor Red
    Write-Host "   Please start it with: docker compose -f docker/docker-compose.cpu.yml up -d" -ForegroundColor Yellow
    exit 1
}

Write-Host "✅ Container '$CONTAINER_NAME' is running" -ForegroundColor Green
Write-Host ""

# Function to run alembic command
function Run-Alembic {
    param([string]$Cmd)
    Write-Host "Executing: alembic $Cmd" -ForegroundColor Yellow
    Write-Host ""
    docker exec -w ${ALEMBIC_DIR} ${CONTAINER_NAME} python -m alembic $Cmd
}

# Parse command
switch ($Command.ToLower()) {
    "current" {
        Write-Host "==========================================" -ForegroundColor Cyan
        Write-Host "📍 Current Migration Status" -ForegroundColor Cyan
        Write-Host "==========================================" -ForegroundColor Cyan
        Run-Alembic "current"
    }
    
    "history" {
        Write-Host "==========================================" -ForegroundColor Cyan
        Write-Host "📜 Migration History" -ForegroundColor Cyan
        Write-Host "==========================================" -ForegroundColor Cyan
        Run-Alembic "history --verbose"
    }
    
    "upgrade" {
        Write-Host "==========================================" -ForegroundColor Cyan
        Write-Host "⬆️  Upgrading Database" -ForegroundColor Cyan
        Write-Host "==========================================" -ForegroundColor Cyan
        if ($Arg1) {
            Run-Alembic "upgrade $Arg1"
        } else {
            Run-Alembic "upgrade head"
        }
        Write-Host ""
        Write-Host "✅ Upgrade completed!" -ForegroundColor Green
    }
    
    "downgrade" {
        Write-Host "==========================================" -ForegroundColor Cyan
        Write-Host "⬇️  Downgrading Database" -ForegroundColor Cyan
        Write-Host "==========================================" -ForegroundColor Cyan
        if ($Arg1) {
            Run-Alembic "downgrade $Arg1"
        } else {
            Write-Host "❌ Please specify revision (e.g., -1 for one step back)" -ForegroundColor Red
            exit 1
        }
        Write-Host ""
        Write-Host "✅ Downgrade completed!" -ForegroundColor Green
    }
    
    "revision" {
        Write-Host "==========================================" -ForegroundColor Cyan
        Write-Host "📝 Creating New Migration" -ForegroundColor Cyan
        Write-Host "==========================================" -ForegroundColor Cyan
        if ($Arg1 -eq "--autogenerate" -or $Arg1 -eq "-m") {
            if ($Arg1 -eq "--autogenerate") {
                $message = if ($Arg2) { $Arg2 } else { "auto_migration" }
                Run-Alembic "revision --autogenerate -m '$message'"
            } else {
                $message = $Arg2
                Run-Alembic "revision -m '$message'"
            }
        } elseif ($Arg1) {
            Run-Alembic "revision -m '$Arg1'"
        } else {
            Write-Host "Usage: .\run_alembic_migration.ps1 revision -m 'migration message'" -ForegroundColor Yellow
            Write-Host "   or: .\run_alembic_migration.ps1 revision --autogenerate -m 'migration message'" -ForegroundColor Yellow
            exit 1
        }
        Write-Host ""
        Write-Host "✅ Migration file created!" -ForegroundColor Green
        Write-Host "   Check: alembic/versions/" -ForegroundColor Yellow
    }
    
    "show" {
        Write-Host "==========================================" -ForegroundColor Cyan
        Write-Host "📋 Showing Migration Details" -ForegroundColor Cyan
        Write-Host "==========================================" -ForegroundColor Cyan
        if ($Arg1) {
            Run-Alembic "show $Arg1"
        } else {
            Run-Alembic "show head"
        }
    }
    
    "stamp" {
        Write-Host "==========================================" -ForegroundColor Cyan
        Write-Host "🏷️  Stamping Database" -ForegroundColor Cyan
        Write-Host "==========================================" -ForegroundColor Cyan
        if ($Arg1) {
            Run-Alembic "stamp $Arg1"
        } else {
            Write-Host "❌ Please specify revision to stamp" -ForegroundColor Red
            exit 1
        }
        Write-Host ""
        Write-Host "✅ Database stamped!" -ForegroundColor Green
    }
    
    default {
        Write-Host "==========================================" -ForegroundColor Cyan
        Write-Host "📖 Alembic Migration Helper" -ForegroundColor Cyan
        Write-Host "==========================================" -ForegroundColor Cyan
        Write-Host ""
        Write-Host "Usage: .\run_alembic_migration.ps1 <command> [options]" -ForegroundColor Yellow
        Write-Host ""
        Write-Host "Commands:" -ForegroundColor Cyan
        Write-Host "  current                    Show current database revision"
        Write-Host "  history                    Show migration history"
        Write-Host "  upgrade [revision]         Upgrade to revision (default: head)"
        Write-Host "  downgrade <revision>       Downgrade to revision (e.g., -1, base)"
        Write-Host "  revision -m 'message'      Create new manual migration"
        Write-Host "  revision --autogenerate -m 'message'  Auto-generate migration from models"
        Write-Host "  show [revision]            Show migration details (default: head)"
        Write-Host "  stamp <revision>           Stamp database with revision without running"
        Write-Host "  help                       Show this help message"
        Write-Host ""
        Write-Host "Examples:" -ForegroundColor Cyan
        Write-Host "  .\run_alembic_migration.ps1 current"
        Write-Host "  .\run_alembic_migration.ps1 upgrade"
        Write-Host "  .\run_alembic_migration.ps1 revision --autogenerate -m 'Add user query history tables'"
        Write-Host "  .\run_alembic_migration.ps1 revision -m 'Add new column'"
        Write-Host "  .\run_alembic_migration.ps1 downgrade -1"
        Write-Host "  .\run_alembic_migration.ps1 show head"
        Write-Host ""
        Write-Host "Note: alembic.ini is located in /app/alembic/ inside the container" -ForegroundColor Yellow
    }
}


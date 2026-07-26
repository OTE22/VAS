@echo off
setlocal EnableExtensions EnableDelayedExpansion

REM ============================================================
REM  Face Recognition Service - Automatic Docker Startup
REM ============================================================

echo ==========================================
echo   Face Recognition Service
echo   Automatic Docker Startup
echo ==========================================
echo.

REM ------------------------------------------------------------
REM Check Docker
REM ------------------------------------------------------------
docker info >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Docker is not running!
    echo Please start Docker Desktop.
    exit /b 1
)

REM ------------------------------------------------------------
REM Detect GPU
REM ------------------------------------------------------------
set HAS_GPU=false
set GPU_NAME=

where nvidia-smi >nul 2>&1
if not errorlevel 1 (
    nvidia-smi >nul 2>&1
    if not errorlevel 1 (
        set HAS_GPU=true
        for /f "delims=" %%i in ('nvidia-smi --query-gpu=name --format=csv,noheader 2^>nul') do (
            set "GPU_NAME=%%i"
            goto gpu_found
        )
    )
)

:gpu_found

REM ------------------------------------------------------------
REM Check NVIDIA Container Toolkit
REM ------------------------------------------------------------
set HAS_NVIDIA_DOCKER=false

if "%HAS_GPU%"=="true" (
    docker info 2>nul | findstr /I "nvidia" >nul
    if not errorlevel 1 (
        set HAS_NVIDIA_DOCKER=true
    )
)

REM ------------------------------------------------------------
REM Mode Selection
REM ------------------------------------------------------------
if "%HAS_GPU%"=="true" (
    if "%HAS_NVIDIA_DOCKER%"=="true" (
        goto gpu_mode
    )
)

goto cpu_mode

REM ------------------------------------------------------------
REM GPU MODE
REM ------------------------------------------------------------
:gpu_mode
echo [OK] NVIDIA GPU detected: %GPU_NAME%
echo [OK] NVIDIA Container Toolkit available
echo [INFO] Starting in GPU mode...
echo.
echo Configuration:
echo   - Workers: 16
echo   - Queue Workers: 50
echo   - Max Queue: 10,000
echo   - GPU Acceleration: ENABLED
echo.

docker compose -f docker-compose.gpu.yml up --build %*
exit /b 0

REM ------------------------------------------------------------
REM CPU MODE
REM ------------------------------------------------------------
:cpu_mode
echo [INFO] No compatible NVIDIA GPU detected
echo [INFO] Starting in CPU mode...
echo.
echo Configuration:
echo   - Workers: 8
echo   - Queue Workers: 15
echo   - Max Queue: 2,000
echo   - GPU Acceleration: DISABLED
echo.

docker compose -f docker-compose.cpu.yml up --build %*
exit /b 0

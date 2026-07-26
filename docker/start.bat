@echo off
REM Automatic Docker startup script for Windows
REM Detects GPU and starts appropriate Docker Compose configuration

echo ==========================================
echo Face Recognition Service - Docker Startup
echo ==========================================
echo.

REM Check for GPU
where nvidia-smi >nul 2>&1
if %errorlevel% == 0 (
    nvidia-smi >nul 2>&1
    if %errorlevel% == 0 (
        echo ✅ NVIDIA GPU detected
        echo 🚀 Starting with GPU support...
        docker-compose -f docker/docker-compose.gpu.yml up --build %*
        exit /b 0
    )
)

echo ℹ️  No GPU detected
echo 💻 Starting with CPU support...
docker-compose -f docker/docker-compose.cpu.yml up --build %*


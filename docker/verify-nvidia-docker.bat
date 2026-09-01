@echo off
REM Verification script for NVIDIA Docker setup (Windows)
REM Run this to check if everything is configured correctly

echo ==========================================
echo   NVIDIA Docker Setup Verification
echo ==========================================
echo.

set ERRORS=0

REM Check 1: NVIDIA drivers
echo 1. Checking NVIDIA GPU drivers...
where nvidia-smi >nul 2>&1
if %errorlevel% == 0 (
    nvidia-smi >nul 2>&1
    if %errorlevel% == 0 (
        echo    ✅ NVIDIA drivers installed
        for /f "tokens=*" %%i in ('nvidia-smi --query-gpu=name --format=csv,noheader 2^>nul') do (
            echo    📊 GPU: %%i
            goto :gpu_found
        )
        :gpu_found
    ) else (
        echo    ❌ nvidia-smi failed (drivers may not be working)
        set /a ERRORS+=1
    )
) else (
    echo    ❌ nvidia-smi not found (drivers not installed)
    set /a ERRORS+=1
)

echo.

REM Check 2: Docker
echo 2. Checking Docker...
docker info >nul 2>&1
if %errorlevel% == 0 (
    echo    ✅ Docker is running
    for /f "tokens=*" %%i in ('docker --version 2^>nul') do (
        echo    📊 %%i
    )
) else (
    echo    ❌ Docker is not running
    set /a ERRORS+=1
)

echo.

REM Check 3: NVIDIA Container Toolkit
echo 3. Checking NVIDIA Container Toolkit...
docker info 2>nul | findstr /C:"nvidia" >nul 2>&1
if %errorlevel% == 0 (
    echo    ✅ NVIDIA Container Toolkit installed
    echo    📊 NVIDIA runtime configured
) else (
    echo    ❌ NVIDIA Container Toolkit not found
    echo    💡 Install NVIDIA Container Toolkit for Windows
    echo    💡 Or use WSL2 with Linux setup
    set /a ERRORS+=1
)

echo.

REM Check 4: Test GPU access in Docker
echo 4. Testing GPU access in Docker...
docker run --rm --gpus all nvidia/cuda:12.4.1-base-ubuntu22.04 nvidia-smi >nul 2>&1
if %errorlevel% == 0 (
    echo    ✅ GPU accessible in Docker containers
    echo    📊 Test container can see GPU
) else (
    echo    ❌ GPU not accessible in Docker
    echo    💡 Check NVIDIA Container Toolkit installation
    set /a ERRORS+=1
)

echo.

REM Check 5: Docker Compose
echo 5. Checking Docker Compose...
docker compose version >nul 2>&1
if %errorlevel% == 0 (
    echo    ✅ Docker Compose available
    for /f "tokens=*" %%i in ('docker compose version 2^>nul') do (
        echo    📊 %%i
    )
) else (
    docker-compose --version >nul 2>&1
    if %errorlevel% == 0 (
        echo    ✅ Docker Compose available
        for /f "tokens=*" %%i in ('docker-compose --version 2^>nul') do (
            echo    📊 %%i
        )
    ) else (
        echo    ❌ Docker Compose not found
        set /a ERRORS+=1
    )
)

echo.
echo ==========================================
if %ERRORS% == 0 (
    echo ✅ All checks passed!
    echo 🚀 Ready for GPU Docker deployment
    echo.
    echo You can now run:
    echo   docker\auto-start.bat
    exit /b 0
) else (
    echo ❌ Found %ERRORS% issue(s)
    echo 📚 See Docs\SETUP_NVIDIA_DOCKER.md for setup instructions
    exit /b 1
)


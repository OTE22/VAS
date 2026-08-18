@echo off
setlocal EnableExtensions EnableDelayedExpansion

REM ============================================================
REM  Start the DEVELOPMENT stack, using the GPU if available.
REM
REM    docker\auto-start.bat              foreground
REM    docker\auto-start.bat -d           detached
REM    docker\auto-start.bat -d --build   rebuild first
REM
REM  DEVELOPMENT only. Production needs secrets, TLS material and database
REM  roles applied in a specific order; a one-click script invites skipping
REM  them. See Docs\61_DEPLOYMENT_RUNBOOK.md.
REM
REM  Replaces docker\start.bat, a second implementation of the same job.
REM ============================================================

REM Run from the repository root regardless of where this was invoked, so the
REM relative -f paths below resolve. The old version omitted this and used
REM "-f docker-compose.gpu.yml" with no directory, which only worked when the
REM current directory happened to be docker\.
pushd "%~dp0.."

echo ==========================================
echo   Face Recognition Service - development
echo ==========================================
echo.

docker info >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Docker is not running. Start Docker Desktop.
    popd & exit /b 1
)

REM The v1 "docker-compose" binary ignores the top-level name: key that keeps
REM the development and production stacks on separate volumes.
docker compose version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] The Docker Compose v2 plugin is required ^(docker compose^).
    popd & exit /b 1
)

REM Shared webhook network (external): VMS -^> nginx, alias face-webhook.
docker network inspect webhook_integration >nul 2>&1
if errorlevel 1 (
    echo Provisioning shared network: webhook_integration
    docker network create webhook_integration >nul 2>&1
    docker network inspect webhook_integration >nul 2>&1
    if errorlevel 1 (
        echo [ERROR] Cannot create network webhook_integration.
        popd & exit /b 1
    )
)
echo [OK] Shared network webhook_integration present

set "HAS_GPU=false"
set "GPU_NAME="
where nvidia-smi >nul 2>&1
if not errorlevel 1 (
    nvidia-smi >nul 2>&1
    if not errorlevel 1 set "HAS_GPU=true"
)
if "!HAS_GPU!"=="true" (
    for /f "delims=" %%i in ('nvidia-smi --query-gpu^=name --format^=csv^,noheader 2^>nul') do (
        if not defined GPU_NAME set "GPU_NAME=%%i"
    )
)

REM A GPU is not enough: the container runtime must be able to hand it over.
set "HAS_NVIDIA_RUNTIME=false"
if "!HAS_GPU!"=="true" (
    docker info 2>nul | findstr /I "nvidia" >nul
    if not errorlevel 1 set "HAS_NVIDIA_RUNTIME=true"
)

REM Delayed expansion (!VAR!) is load-bearing here. The previous version read
REM %HAS_NVIDIA_DOCKER% inside a parenthesised block, which cmd expands when it
REM PARSES the block — i.e. before the detection above ran — so the GPU branch
REM could never be taken.
if "!HAS_GPU!"=="true" if "!HAS_NVIDIA_RUNTIME!"=="true" goto gpu_mode
if "!HAS_GPU!"=="true" goto gpu_without_runtime
goto cpu_mode

:gpu_mode
echo [OK] GPU: !GPU_NAME! ^(NVIDIA container runtime available^)
echo [INFO] Starting CPU stack + GPU override
echo        ALLOW_CPU_FALLBACK is false on this path: if CUDA is unusable the
echo        API refuses to start rather than quietly running on the CPU.
echo.
REM The GPU file is an OVERRIDE layered on the CPU stack, never used alone:
REM by itself it declares no database, no proxy and no dependencies.
docker compose -f docker/docker-compose.cpu.yml -f docker/docker-compose.gpu.yml up %*
popd & exit /b %errorlevel%

:gpu_without_runtime
echo [WARN] GPU present ^(!GPU_NAME!^) but the NVIDIA container runtime is not.
echo        Install the NVIDIA Container Toolkit, then check with:
echo          docker\verify-nvidia-docker.bat
echo          Docs\04_SETUP_NVIDIA_DOCKER.md
echo        Falling back to CPU.
echo.
goto cpu_start

:cpu_mode
echo [INFO] No NVIDIA GPU detected - starting the CPU stack.
echo.

:cpu_start
docker compose -f docker/docker-compose.cpu.yml up %*
popd & exit /b %errorlevel%

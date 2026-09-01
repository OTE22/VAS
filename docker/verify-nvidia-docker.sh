#!/bin/bash
# Verification script for NVIDIA Docker setup
# Run this to check if everything is configured correctly

echo "=========================================="
echo "  NVIDIA Docker Setup Verification"
echo "=========================================="
echo ""

ERRORS=0

# Check 1: NVIDIA drivers
echo "1. Checking NVIDIA GPU drivers..."
if command -v nvidia-smi &> /dev/null; then
    if nvidia-smi &> /dev/null; then
        GPU_NAME=$(nvidia-smi --query-gpu=name --format=csv,noheader | head -n1)
        echo "   ✅ NVIDIA drivers installed"
        echo "   📊 GPU: $GPU_NAME"
    else
        echo "   ❌ nvidia-smi failed (drivers may not be working)"
        ERRORS=$((ERRORS + 1))
    fi
else
    echo "   ❌ nvidia-smi not found (drivers not installed)"
    ERRORS=$((ERRORS + 1))
fi

echo ""

# Check 2: Docker
echo "2. Checking Docker..."
if docker info > /dev/null 2>&1; then
    echo "   ✅ Docker is running"
    DOCKER_VERSION=$(docker --version)
    echo "   📊 $DOCKER_VERSION"
else
    echo "   ❌ Docker is not running"
    ERRORS=$((ERRORS + 1))
fi

echo ""

# Check 3: NVIDIA Container Toolkit
echo "3. Checking NVIDIA Container Toolkit..."
if docker info 2>/dev/null | grep -q "nvidia"; then
    echo "   ✅ NVIDIA Container Toolkit installed"
    echo "   📊 NVIDIA runtime configured"
else
    echo "   ❌ NVIDIA Container Toolkit not found"
    echo "   💡 Install with: sudo apt-get install nvidia-container-toolkit"
    ERRORS=$((ERRORS + 1))
fi

echo ""

# Check 4: Test GPU access in Docker
echo "4. Testing GPU access in Docker..."
if docker run --rm --gpus all nvidia/cuda:12.4.1-base-ubuntu22.04 nvidia-smi > /dev/null 2>&1; then
    echo "   ✅ GPU accessible in Docker containers"
    echo "   📊 Test container can see GPU"
else
    echo "   ❌ GPU not accessible in Docker"
    echo "   💡 Check NVIDIA Container Toolkit installation"
    ERRORS=$((ERRORS + 1))
fi

echo ""

# Check 5: Docker Compose
echo "5. Checking Docker Compose..."
if command -v docker-compose &> /dev/null || docker compose version &> /dev/null; then
    echo "   ✅ Docker Compose available"
    if command -v docker-compose &> /dev/null; then
        COMPOSE_VERSION=$(docker-compose --version)
    else
        COMPOSE_VERSION=$(docker compose version)
    fi
    echo "   📊 $COMPOSE_VERSION"
else
    echo "   ❌ Docker Compose not found"
    ERRORS=$((ERRORS + 1))
fi

echo ""
echo "=========================================="
if [ $ERRORS -eq 0 ]; then
    echo "✅ All checks passed!"
    echo "🚀 Ready for GPU Docker deployment"
    echo ""
    echo "You can now run:"
    echo "  ./docker/auto-start.sh"
    exit 0
else
    echo "❌ Found $ERRORS issue(s)"
    echo "📚 See Docs/SETUP_NVIDIA_DOCKER.md for setup instructions"
    exit 1
fi


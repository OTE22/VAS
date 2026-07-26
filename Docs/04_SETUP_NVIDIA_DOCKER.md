# NVIDIA Docker Setup Guide

## Overview

To use GPU acceleration in Docker, you need:
1. **NVIDIA GPU** with drivers installed
2. **NVIDIA Container Toolkit** (replaces old nvidia-docker2)
3. **NVIDIA CUDA Base Images** (already included in Dockerfile.gpu)

## ✅ What's Already Included

The `Dockerfile.gpu` already uses the official NVIDIA CUDA base image:
```dockerfile
FROM nvidia/cuda:11.8.0-cudnn8-runtime-ubuntu22.04
```

This is the official NVIDIA image from `nvcr.io` (NVIDIA Container Registry).

## 🔧 Installation Steps

### **Step 1: Install NVIDIA GPU Drivers**

**Ubuntu/Debian:**
```bash
# Check if drivers are installed
nvidia-smi

# If not installed, install them:
sudo apt-get update
sudo apt-get install -y nvidia-driver-535  # or latest version
sudo reboot
```

**Windows:**
- Download and install from: https://www.nvidia.com/Download/index.aspx

### **Step 2: Install NVIDIA Container Toolkit**

**Ubuntu/Debian:**
```bash
# Add NVIDIA package repositories
distribution=$(. /etc/os-release;echo $ID$VERSION_ID)
curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey | sudo gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
curl -s -L https://nvidia.github.io/libnvidia-container/$distribution/libnvidia-container.list | \
    sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' | \
    sudo tee /etc/apt/sources.list.d/nvidia-container-toolkit.list

# Install NVIDIA Container Toolkit
sudo apt-get update
sudo apt-get install -y nvidia-container-toolkit

# Configure Docker to use NVIDIA runtime
sudo nvidia-ctk runtime configure --runtime=docker

# Restart Docker
sudo systemctl restart docker
```

**Windows:**
- Install Docker Desktop with WSL2 backend
- Install NVIDIA drivers for Windows
- Docker Desktop should automatically detect GPU (Windows 11 with WSL2)

### **Step 3: Verify Installation**

```bash
# Test GPU access in Docker
docker run --rm --gpus all nvidia/cuda:11.8.0-base-ubuntu22.04 nvidia-smi
```

You should see your GPU information displayed.

### **Step 4: Verify Docker Compose GPU Support**

```bash
# Check if Docker Compose supports GPU
docker compose version

# Test with our GPU compose file
docker-compose -f docker/docker-compose.gpu.yml config
```

## 🎯 Quick Verification Script

Create and run this script to verify everything:

```bash
#!/bin/bash
echo "Checking NVIDIA Docker Setup..."
echo ""

echo "1. Checking NVIDIA drivers..."
if command -v nvidia-smi &> /dev/null; then
    nvidia-smi --query-gpu=name --format=csv,noheader
    echo "✅ NVIDIA drivers installed"
else
    echo "❌ NVIDIA drivers not found"
    exit 1
fi

echo ""
echo "2. Checking Docker..."
if docker info > /dev/null 2>&1; then
    echo "✅ Docker is running"
else
    echo "❌ Docker is not running"
    exit 1
fi

echo ""
echo "3. Checking NVIDIA Container Toolkit..."
if docker info 2>/dev/null | grep -q "nvidia"; then
    echo "✅ NVIDIA Container Toolkit installed"
else
    echo "❌ NVIDIA Container Toolkit not found"
    echo "   Install it with: sudo apt-get install nvidia-container-toolkit"
    exit 1
fi

echo ""
echo "4. Testing GPU access in Docker..."
if docker run --rm --gpus all nvidia/cuda:11.8.0-base-ubuntu22.04 nvidia-smi > /dev/null 2>&1; then
    echo "✅ GPU accessible in Docker"
else
    echo "❌ GPU not accessible in Docker"
    exit 1
fi

echo ""
echo "✅ All checks passed! Ready for GPU Docker deployment."
```

## 📋 Official NVIDIA Images

The Dockerfile.gpu uses these official NVIDIA images:

- **Base Image:** `nvidia/cuda:11.8.0-cudnn8-runtime-ubuntu22.04`
- **Registry:** `nvcr.io` (NVIDIA Container Registry)
- **CUDA Version:** 11.8.0
- **cuDNN:** 8
- **OS:** Ubuntu 22.04

### Available NVIDIA Images

You can browse all available images at:
- **Docker Hub:** https://hub.docker.com/r/nvidia/cuda
- **NVIDIA Container Registry:** https://catalog.ngc.nvidia.com/containers

### Common NVIDIA Image Tags

```dockerfile
# Runtime (smaller, for production)
FROM nvidia/cuda:11.8.0-cudnn8-runtime-ubuntu22.04

# Devel (includes development tools, larger)
FROM nvidia/cuda:11.8.0-cudnn8-devel-ubuntu22.04

# Latest CUDA 12.x
FROM nvidia/cuda:12.2.0-cudnn8-runtime-ubuntu22.04
```

## 🔍 Troubleshooting

### **Issue: "nvidia-container-toolkit not found"**

**Solution:**
```bash
# Install NVIDIA Container Toolkit
sudo apt-get update
sudo apt-get install -y nvidia-container-toolkit
sudo systemctl restart docker
```

### **Issue: "Cannot connect to the Docker daemon"**

**Solution:**
```bash
# Add your user to docker group
sudo usermod -aG docker $USER
newgrp docker
```

### **Issue: "nvidia-smi not found in container"**

**Solution:**
- Ensure you're using `--gpus all` flag
- Check docker-compose.yml has GPU configuration
- Verify NVIDIA Container Toolkit is installed

### **Issue: "CUDA out of memory"**

**Solution:**
- Reduce batch sizes in config
- Limit GPU memory in docker-compose:
  ```yaml
  deploy:
    resources:
      reservations:
        devices:
          - driver: nvidia
            count: 1
            capabilities: [gpu]
            options:
              memory: "4GB"  # Limit GPU memory
  ```

## 📚 References

- **NVIDIA Container Toolkit:** https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/install-guide.html
- **NVIDIA CUDA Images:** https://hub.docker.com/r/nvidia/cuda
- **Docker GPU Support:** https://docs.docker.com/config/containers/resource_constraints/#gpu

## ✅ Summary

1. ✅ **Dockerfile.gpu** already uses official NVIDIA base image
2. ✅ **Install NVIDIA Container Toolkit** (not nvidia-docker2)
3. ✅ **Configure Docker** to use NVIDIA runtime
4. ✅ **Verify** with test container
5. ✅ **Run** with `docker\auto-start.bat` or `./docker/auto-start.sh`

The auto-start scripts will automatically detect if everything is set up correctly!


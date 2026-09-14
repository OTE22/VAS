# 🚀 Quick Start Guide

## The Easiest Way to Start

### **Windows:**
Double-click or run:
```cmd
docker\auto-start.bat
```

### **Linux/Mac:**
```bash
chmod +x docker/auto-start.sh
./docker/auto-start.sh
```

**That's it!** The script will:
- ✅ Automatically detect your hardware (GPU or CPU)
- ✅ Start the correct Docker configuration
- ✅ Show you what's running

---

## 📦 What Docker Files Are Available?

### **Docker Compose Files:**
1. **`docker-compose.gpu.yml`** - GPU **override**, layered on the CPU
   stack (`-f cpu.yml -f gpu.yml`). Not a standalone stack.
2. **`docker-compose.cpu.yml`** - For systems without GPU

### **Dockerfiles:**
1. **`Dockerfile.gpu`** - GPU-enabled image
2. **`Dockerfile.cpu`** - CPU-only image

### **Auto-Start Scripts:**
**`auto-start.sh`** / **`auto-start.bat`** — detects the GPU and the NVIDIA
container runtime, then starts the CPU stack, layering the GPU override when
both are present.

(`start.sh` / `start.bat` were a second, less capable implementation of the
same job and have been removed. Two launchers drifted apart; one is enough.)

---

## 🎯 Common Commands

### **Start Services:**
```bash
# Auto-detect (recommended)
./docker/auto-start.sh

# Or manually
docker compose -f docker/docker-compose.cpu.yml -f docker/docker-compose.gpu.yml up -d --build    # GPU
docker-compose -f docker/docker-compose.cpu.yml up --build   # CPU
```

### **Start in Background:**
```bash
./docker/auto-start.sh -d
```

### **Stop Services:**
```bash
# Press Ctrl+C if running in foreground
# Or:
docker compose -f docker/docker-compose.cpu.yml -f docker/docker-compose.gpu.yml down    # GPU
docker-compose -f docker/docker-compose.cpu.yml down    # CPU
```

### **View Logs:**
```bash
docker compose -f docker/docker-compose.cpu.yml -f docker/docker-compose.gpu.yml logs -f
```

---

## ✅ After Starting

1. **Wait for services to start** (30-60 seconds)
2. **Open browser:** http://localhost:8000
3. **Check health:** http://localhost:8000/health

### **Verify GPU Usage (if GPU version):**

**Check Ollama GPU usage:**
```bash
# Check if Ollama is using GPU
docker logs face_recognition_ollama | grep -i gpu

# Check GPU activity
nvidia-smi
```

**Ollama automatically uses GPU when available!** See `OLLAMA_GPU.md` for details.

---

## 🔍 Verify NVIDIA Docker Setup

Before starting with GPU, verify your setup:

**Linux/Mac:**
```bash
chmod +x docker/verify-nvidia-docker.sh
./docker/verify-nvidia-docker.sh
```

**Windows:**
```cmd
docker\verify-nvidia-docker.bat
```

## 🆘 Need Help?

- **Quick Start:** This file
- **Full Guide:** See `DOCKER_USAGE.md` (in this Docs folder)
- **NVIDIA Setup:** See `SETUP_NVIDIA_DOCKER.md` (in this Docs folder)


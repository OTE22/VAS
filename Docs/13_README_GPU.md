# GPU Support Guide

> **Vector backend note.** Where this document says *FAISS*, the live
> system uses **PostgreSQL + pgvector**. PostgreSQL is authoritative and
> the index is a disposable acceleration layer — see
> [`70_VECTOR_INDEX_CONTRACT.md`](70_VECTOR_INDEX_CONTRACT.md). The
> surrounding explanation of *what* the index does is still accurate.

This guide explains how to use GPU acceleration in the Face Recognition Service.

## Automatic GPU Detection

The system automatically detects GPU availability and configures itself accordingly:

- **ONNX Runtime**: Uses `CUDAExecutionProvider` if GPU is available, falls back to `CPUExecutionProvider`
- **FAISS**: the variant is fixed when the IMAGE is built, not chosen at
  runtime — `requirements-gpu.txt` declares `faiss-gpu`, `requirements-cpu.txt`
  declares `faiss-cpu`. There is no fallback between them. (FAISS is also not
  the default backend; pgvector is — see
  [`70_VECTOR_INDEX_CONTRACT.md`](70_VECTOR_INDEX_CONTRACT.md).)
- **PyTorch**: both images install the **CPU** wheel deliberately. Nothing in
  this application imports torch; it exists only for a 384-dim MiniLM sentence
  embedding used by query-history search. Installing the CUDA build would add
  several GB and contend with SCRFD/ArcFace for the same device.

## Installation

### Automatic Installation (Recommended)

Run the automatic installation script that detects GPU and installs appropriate dependencies:

```bash
python scripts/setup/install_dependencies.py
```

This will:
1. Detect if GPU is available
2. Install `requirements-gpu.txt` if GPU found, otherwise `requirements-cpu.txt`
3. Report which mode is being used

### Manual Installation

#### CPU Only
```bash
pip install -r requirements-cpu.txt
```

#### GPU Enabled
```bash
pip install -r requirements-gpu.txt
```

**Prerequisites for GPU:**
- NVIDIA GPU with CUDA support
- **CUDA 12.x and cuDNN 9** — not 11.8. `requirements-gpu.txt` pins
  `onnxruntime-gpu==1.20.1`, which is built against CUDA 12.x, and
  `docker/Dockerfile.gpu` is based on `nvidia/cuda:12.4.1-cudnn-runtime`.
  These three move together; changing one alone is how the CUDA execution
  provider silently stops registering and every inference quietly runs on the
  CPU with clean logs and a passing healthcheck.
- NVIDIA driver **>= 525**. Verify before deploying:
  `nvidia-smi --query-gpu=driver_version --format=csv`

## Docker Deployment

### Automatic Detection

Use the auto-start script that detects GPU automatically:

**Linux/Mac:**
```bash
chmod +x docker/auto-start.sh
./docker/auto-start.sh
```

**Windows:**
```cmd
docker\auto-start.bat
```

### Manual Selection

#### CPU Deployment
```bash
docker-compose -f docker/docker-compose.cpu.yml up --build
```

#### GPU Deployment
```bash
# Prerequisites: Install nvidia-docker2
docker compose -f docker/docker-compose.cpu.yml -f docker/docker-compose.gpu.yml up -d --build
```

**GPU Docker Requirements:**
- NVIDIA Docker runtime (`nvidia-docker2`)
- NVIDIA Container Toolkit
- NVIDIA GPU drivers

## Code Usage

The code automatically detects and uses GPU when available:

### ONNX Runtime (ArcFace & SCRFD Models)

```python
# Automatically uses GPU if available
from models import ArcFace, SCRFD

recognizer = ArcFace(model_path)  # Uses GPU if available
detector = SCRFD(model_path)     # Uses GPU if available
```

### FAISS (Vector Index)

The display-name-keyed `FaceDatabase` (`from database import FaceDatabase`)
was deleted 2026-08 — it was write-never under pgvector and its fallback could
only answer Unknown. Vector search runs through the `backend/core/vector_index`
contract: `VECTOR_BACKEND=pgvector` searches PostgreSQL in place;
`VECTOR_BACKEND=faiss` uses `FlatFaissIndex`, which selects the GPU FAISS
build automatically via `utils/gpu_detection.get_faiss_backend()`.

## Verification

### Check GPU Detection

```bash
python -c "from utils.gpu_detection import detect_gpu; print(detect_gpu())"
```

### Check ONNX Providers

The application logs which provider is being used:
- Look for: `"ONNX Runtime using provider: CUDAExecutionProvider"` (GPU)
- Or: `"ONNX Runtime using provider: CPUExecutionProvider"` (CPU)

### Check FAISS Backend

The application logs which FAISS backend is being used:
- Look for: `"Using FAISS GPU for face database"` (GPU)
- Or: `"Using FAISS CPU for face database"` (CPU)

## Performance

### Expected Speedup with GPU

- **Face Detection (SCRFD)**: 3-5x faster on GPU
- **Face Recognition (ArcFace)**: 4-6x faster on GPU
- **FAISS Search**: 10-50x faster on GPU (depending on database size)

### CPU Fallback

If GPU is not available or fails to initialize, the system automatically falls back to CPU mode. No code changes are required.

## Troubleshooting

### GPU Not Detected

1. Check NVIDIA drivers: `nvidia-smi`
2. Check CUDA installation: `nvcc --version`
3. Verify `onnxruntime-gpu` is installed: `pip list | grep onnxruntime`

### Docker GPU Issues

1. Install nvidia-docker2: `sudo apt-get install nvidia-docker2`
2. Restart Docker: `sudo systemctl restart docker`
3. Test: `docker run --rm --gpus all nvidia/cuda:11.8.0-base-ubuntu22.04 nvidia-smi`

### FAISS GPU Issues

If FAISS GPU fails to initialize, it automatically falls back to CPU. Check logs for warnings.

## Manual Override

You can force CPU mode by setting environment variable:
```bash
export USE_GPU=false
```

Or force GPU mode:
```bash
export USE_GPU=true
```

Note: Forcing GPU mode when GPU is not available will cause errors.


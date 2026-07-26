"""
GPU Detection Utility
=====================
Automatically detects GPU availability and configures the system accordingly.
"""

import os
import sys
import subprocess
import logging
from typing import Tuple, Optional, List

logger = logging.getLogger(__name__)


def detect_gpu() -> Tuple[bool, Optional[str], Optional[dict]]:
    """
    Detect GPU availability and return information.
    
    Returns:
        Tuple of (has_gpu, gpu_type, gpu_info)
        - has_gpu: bool indicating if GPU is available
        - gpu_type: str ('cuda', 'rocm', 'cpu', None)
        - gpu_info: dict with GPU details
    """
    gpu_info = {}
    gpu_type = None
    
    # Check for NVIDIA CUDA
    cuda_available = _check_cuda()
    if cuda_available:
        gpu_type = 'cuda'
        gpu_info = _get_cuda_info()
        logger.info(f"✅ NVIDIA CUDA GPU detected: {gpu_info.get('name', 'Unknown')}")
        return True, gpu_type, gpu_info
    
    # Check for AMD ROCm
    rocm_available = _check_rocm()
    if rocm_available:
        gpu_type = 'rocm'
        gpu_info = _get_rocm_info()
        logger.info(f"✅ AMD ROCm GPU detected")
        return True, gpu_type, gpu_info
    
    # Check for Intel GPU (oneAPI)
    intel_gpu_available = _check_intel_gpu()
    if intel_gpu_available:
        gpu_type = 'intel'
        gpu_info = _get_intel_gpu_info()
        logger.info(f"✅ Intel GPU detected")
        return True, gpu_type, gpu_info
    
    logger.info("ℹ️  No GPU detected, using CPU")
    return False, 'cpu', gpu_info


def _check_cuda() -> bool:
    """Check if NVIDIA CUDA is available"""
    try:
        # Check nvidia-smi command
        result = subprocess.run(
            ['nvidia-smi'],
            capture_output=True,
            text=True,
            timeout=5
        )
        if result.returncode == 0:
            return True
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    
    # Check for CUDA in environment
    if os.environ.get('CUDA_VISIBLE_DEVICES') is not None:
        return True
    
    # Check for CUDA libraries
    cuda_paths = [
        '/usr/local/cuda',
        '/usr/local/cuda-11',
        '/usr/local/cuda-12',
        os.environ.get('CUDA_HOME', ''),
        os.environ.get('CUDA_PATH', ''),
    ]
    for path in cuda_paths:
        if path and os.path.exists(path):
            return True
    
    return False


def _check_rocm() -> bool:
    """Check if AMD ROCm is available"""
    try:
        result = subprocess.run(
            ['rocm-smi'],
            capture_output=True,
            text=True,
            timeout=5
        )
        if result.returncode == 0:
            return True
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    
    # Check for ROCm in environment
    if os.environ.get('ROCM_PATH'):
        return True
    
    return False


def _check_intel_gpu() -> bool:
    """Check if Intel GPU is available"""
    try:
        result = subprocess.run(
            ['intel_gpu_top', '--version'],
            capture_output=True,
            text=True,
            timeout=5
        )
        if result.returncode == 0:
            return True
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    
    return False


def _get_cuda_info() -> dict:
    """Get NVIDIA CUDA GPU information"""
    info = {
        'type': 'cuda',
        'name': 'Unknown',
        'driver_version': None,
        'cuda_version': None,
        'gpu_count': 0,
        'gpus': []
    }
    
    try:
        result = subprocess.run(
            ['nvidia-smi', '--query-gpu=name,driver_version,memory.total', '--format=csv,noheader'],
            capture_output=True,
            text=True,
            timeout=5
        )
        if result.returncode == 0:
            lines = result.stdout.strip().split('\n')
            info['gpu_count'] = len(lines)
            for line in lines:
                parts = [p.strip() for p in line.split(',')]
                if len(parts) >= 2:
                    gpu = {
                        'name': parts[0],
                        'driver_version': parts[1] if len(parts) > 1 else None,
                        'memory': parts[2] if len(parts) > 2 else None
                    }
                    info['gpus'].append(gpu)
            if info['gpus']:
                info['name'] = info['gpus'][0]['name']
                info['driver_version'] = info['gpus'][0]['driver_version']
    except Exception as e:
        logger.warning(f"Could not get detailed CUDA info: {e}")
    
    # Get CUDA version
    try:
        cuda_path = os.environ.get('CUDA_HOME') or os.environ.get('CUDA_PATH') or '/usr/local/cuda'
        version_file = os.path.join(cuda_path, 'version.txt')
        if os.path.exists(version_file):
            with open(version_file, 'r') as f:
                info['cuda_version'] = f.read().strip()
    except Exception:
        pass
    
    return info


def _get_rocm_info() -> dict:
    """Get AMD ROCm GPU information"""
    info = {
        'type': 'rocm',
        'name': 'AMD GPU',
        'version': None
    }
    
    try:
        result = subprocess.run(
            ['rocm-smi', '--version'],
            capture_output=True,
            text=True,
            timeout=5
        )
        if result.returncode == 0:
            info['version'] = result.stdout.strip()
    except Exception:
        pass
    
    return info


def _get_intel_gpu_info() -> dict:
    """Get Intel GPU information"""
    return {
        'type': 'intel',
        'name': 'Intel GPU'
    }


def get_onnx_providers() -> List[str]:
    """
    Get the appropriate ONNX Runtime execution providers based on GPU availability.
    
    Returns:
        List of provider names in priority order
    """
    has_gpu, gpu_type, _ = detect_gpu()
    
    if has_gpu and gpu_type == 'cuda':
        # Try CUDA first, fallback to CPU
        return ["CUDAExecutionProvider", "CPUExecutionProvider"]
    elif has_gpu and gpu_type == 'rocm':
        # ROCm support (if available)
        return ["ROCMExecutionProvider", "CPUExecutionProvider"]
    elif has_gpu and gpu_type == 'intel':
        # Intel GPU support
        return ["OpenVINOExecutionProvider", "CPUExecutionProvider"]
    else:
        # CPU only
        return ["CPUExecutionProvider"]


def get_faiss_backend() -> str:
    """
    Get the appropriate FAISS backend based on GPU availability.
    
    Returns:
        'gpu' or 'cpu'
    """
    has_gpu, gpu_type, _ = detect_gpu()
    
    if has_gpu and gpu_type == 'cuda':
        return 'gpu'
    else:
        return 'cpu'


def should_install_gpu_dependencies() -> bool:
    """
    Determine if GPU dependencies should be installed.
    
    Returns:
        True if GPU is available and GPU dependencies should be installed
    """
    has_gpu, _, _ = detect_gpu()
    return has_gpu


if __name__ == "__main__":
    # Test GPU detection
    logging.basicConfig(level=logging.INFO)
    has_gpu, gpu_type, gpu_info = detect_gpu()
    
    print(f"\n{'='*60}")
    print("GPU Detection Results")
    print(f"{'='*60}")
    print(f"GPU Available: {has_gpu}")
    print(f"GPU Type: {gpu_type}")
    print(f"GPU Info: {gpu_info}")
    print(f"\nONNX Providers: {get_onnx_providers()}")
    print(f"FAISS Backend: {get_faiss_backend()}")
    print(f"Install GPU Dependencies: {should_install_gpu_dependencies()}")
    print(f"{'='*60}\n")


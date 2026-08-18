"""
Automatic Dependency Installation Script
======================================
Detects GPU availability and installs appropriate dependencies.
"""

import os
import sys
import subprocess
import logging

# Setup logging
logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')
logger = logging.getLogger(__name__)

# Repository root, derived from this file's location rather than the working
# directory. The script used to pass bare names like 'requirements-cpu.txt' to
# pip, so it only worked when invoked from the repository root and reported
# "Requirements file not found" from anywhere else.
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
BASE_REQUIREMENTS = os.path.join(REPO_ROOT, 'requirements-base.txt')


def detect_gpu():
    """Detect if GPU is available"""
    try:
        from utils.gpu_detection import detect_gpu
        has_gpu, gpu_type, gpu_info = detect_gpu()
        return has_gpu, gpu_type, gpu_info
    except ImportError:
        # If GPU detection module not available, try basic check
        try:
            result = subprocess.run(
                ['nvidia-smi'],
                capture_output=True,
                text=True,
                timeout=5
            )
            if result.returncode == 0:
                logger.info("✅ NVIDIA GPU detected via nvidia-smi")
                return True, 'cuda', {}
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass
        
        logger.info("ℹ️  No GPU detected, will install CPU dependencies")
        return False, 'cpu', {}


def install_dependencies(requirements_file):
    """Install dependencies from requirements file"""
    logger.info(f"📦 Installing dependencies from {requirements_file}...")
    
    try:
        subprocess.check_call([
            sys.executable, '-m', 'pip', 'install', '--upgrade', 'pip'
        ])
        
        subprocess.check_call([
            sys.executable, '-m', 'pip', 'install', '--no-cache-dir', '-r', requirements_file
        ])
        
        logger.info(f"✅ Successfully installed dependencies from {requirements_file}")
        return True
    except subprocess.CalledProcessError as e:
        logger.error(f"❌ Failed to install dependencies: {e}")
        return False


def main():
    """Main installation function"""
    print("\n" + "="*60)
    print("Face Recognition Service - Dependency Installer")
    print("="*60 + "\n")
    
    # Detect GPU
    has_gpu, gpu_type, gpu_info = detect_gpu()
    
    print(f"GPU Detection:")
    print(f"  Available: {has_gpu}")
    print(f"  Type: {gpu_type}")
    if gpu_info:
        print(f"  Info: {gpu_info}")
    print()
    
    # Determine which requirements file to use
    if has_gpu and gpu_type == 'cuda':
        requirements_file = os.path.join(REPO_ROOT, 'requirements-gpu.txt')
        logger.info("🚀 GPU detected! Installing GPU-accelerated dependencies...")
    else:
        requirements_file = os.path.join(REPO_ROOT, 'requirements-cpu.txt')
        logger.info("💻 No GPU detected. Installing CPU-only dependencies...")

    # Check if requirements file exists
    if not os.path.exists(requirements_file):
        logger.error(f"❌ Requirements file not found: {requirements_file}")
        logger.info("Available files:")
        for name in ('requirements-base.txt', 'requirements-cpu.txt', 'requirements-gpu.txt'):
            if os.path.exists(os.path.join(REPO_ROOT, name)):
                logger.info(f"  - {name}")
        sys.exit(1)

    # The hardware file is only half of the dependency set: it opens with
    # `-r requirements-base.txt`, which pip resolves relative to the file. Fail
    # here with a readable message instead of inside pip's resolver.
    if not os.path.exists(BASE_REQUIREMENTS):
        logger.error(f"❌ Shared dependencies missing: {BASE_REQUIREMENTS}")
        logger.error(
            f"   {os.path.basename(requirements_file)} includes it with "
            f"`-r requirements-base.txt` and cannot install without it.")
        sys.exit(1)

    # Install dependencies
    success = install_dependencies(requirements_file)
    
    if success:
        print("\n" + "="*60)
        print("✅ Installation completed successfully!")
        print("="*60)
        print(f"\nInstalled: {os.path.basename(requirements_file)} "
              f"(+ requirements-base.txt)")
        if has_gpu:
            print("GPU acceleration: ENABLED")
        else:
            print("GPU acceleration: DISABLED (CPU mode)")
        print("\nYou can now run the application.")
    else:
        print("\n" + "="*60)
        print("❌ Installation failed!")
        print("="*60)
        print("\nPlease check the error messages above and try again.")
        sys.exit(1)


if __name__ == "__main__":
    main()


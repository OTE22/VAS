"""Operational metrics for alerting.

These exist so the alert rules in monitoring/alerts/ describe real signals
rather than aspirational ones. Each is deliberately cheap to compute and is
refreshed on a slow timer, not per request.

The GPU gauges matter most: a CUDA/onnxruntime version mismatch makes inference
fall back to the CPU silently. Results stay correct, logs stay clean, the
healthcheck stays green — the service is just an order of magnitude slower. It
is only visible if something explicitly measures it.
"""

from __future__ import annotations

import logging
import os
import shutil
import time
from typing import Optional

logger = logging.getLogger(__name__)

_initialized = False

cuda_available = None
cpu_fallback_active = None
gpu_memory_used = None
gpu_memory_total = None
model_loaded = None
storage_free = None
storage_total = None
last_backup_timestamp = None
tls_cert_expiry = None


def initialize_operational_metrics() -> None:
    """Register the gauges. Idempotent."""
    global _initialized
    global cuda_available, cpu_fallback_active, gpu_memory_used, gpu_memory_total
    global model_loaded, storage_free, storage_total
    global last_backup_timestamp, tls_cert_expiry

    if _initialized:
        return

    try:
        from prometheus_client import Gauge

        cuda_available = Gauge(
            "face_detector_cuda_available",
            "1 when CUDAExecutionProvider is active for inference, 0 otherwise",
        )
        cpu_fallback_active = Gauge(
            "face_detector_cpu_fallback_active",
            "1 when GPU mode was requested but inference is running on the CPU",
        )
        gpu_memory_used = Gauge(
            "face_detector_gpu_memory_used_bytes", "GPU memory in use"
        )
        gpu_memory_total = Gauge(
            "face_detector_gpu_memory_total_bytes", "Total GPU memory"
        )
        model_loaded = Gauge(
            "face_detector_model_loaded",
            "1 when the detection and recognition models are loaded",
        )
        storage_free = Gauge(
            "face_detector_storage_free_bytes", "Free bytes on the storage volume"
        )
        storage_total = Gauge(
            "face_detector_storage_total_bytes", "Total bytes on the storage volume"
        )
        last_backup_timestamp = Gauge(
            "face_detector_last_backup_timestamp_seconds",
            "Unix time of the newest successful backup",
        )
        tls_cert_expiry = Gauge(
            "face_detector_tls_cert_expiry_timestamp_seconds",
            "Unix time at which the serving TLS certificate expires",
        )
        _initialized = True
    except Exception as e:  # duplicate registration, or prometheus_client absent
        logger.debug("Operational metrics not registered: %s", e)


def _set(gauge, value) -> None:
    if gauge is not None and value is not None:
        try:
            gauge.set(float(value))
        except Exception:
            pass


def probe_gpu() -> None:
    """Record whether CUDA is genuinely usable, not merely requested."""
    from config import settings

    requested = bool(getattr(settings, "USE_GPU", False))
    usable = False

    try:
        import onnxruntime as ort

        usable = "CUDAExecutionProvider" in ort.get_available_providers()
    except Exception:
        usable = False

    _set(cuda_available, 1 if usable else 0)
    # Only a fallback if GPU was actually asked for; a CPU deployment running
    # on the CPU is not an incident.
    _set(cpu_fallback_active, 1 if (requested and not usable) else 0)

    if requested and usable:
        _probe_gpu_memory()


def _probe_gpu_memory() -> None:
    """GPU memory via nvidia-smi. Absent on CPU hosts, which is not an error."""
    import subprocess

    try:
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.used,memory.total",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode != 0 or not result.stdout.strip():
            return
        used_mb, total_mb = result.stdout.strip().splitlines()[0].split(",")
        _set(gpu_memory_used, float(used_mb.strip()) * 1024 * 1024)
        _set(gpu_memory_total, float(total_mb.strip()) * 1024 * 1024)
    except Exception:
        pass


def probe_models() -> None:
    try:
        from backend.core.model_manager import model_manager

        ready = bool(
            getattr(model_manager, "detector", None) is not None
            and getattr(model_manager, "recognizer", None) is not None
        )
        _set(model_loaded, 1 if ready else 0)
    except Exception:
        _set(model_loaded, 0)


def probe_storage() -> None:
    from config import settings

    path = getattr(settings, "STORAGE_DIR", "/app/storage")
    try:
        usage = shutil.disk_usage(path)
        _set(storage_free, usage.free)
        _set(storage_total, usage.total)
    except Exception:
        pass


def probe_backups(backup_dir: Optional[str] = None) -> None:
    """Age of the newest backup directory.

    Alerting on age rather than on a failure event means a backup job that
    silently stopped running still pages.
    """
    directory = backup_dir or os.getenv("BACKUP_DIR", "/backups")
    try:
        if not os.path.isdir(directory):
            return
        newest = 0.0
        for name in os.listdir(directory):
            full = os.path.join(directory, name)
            if os.path.isdir(full) and name.endswith("Z"):
                newest = max(newest, os.path.getmtime(full))
        if newest:
            _set(last_backup_timestamp, newest)
    except Exception:
        pass


def probe_certificate(cert_path: Optional[str] = None) -> None:
    """Expiry of the serving certificate, so renewal can be alerted on."""
    path = cert_path or os.getenv("TLS_CERT_PATH", "/etc/nginx/certs/server.crt")
    try:
        if not os.path.isfile(path):
            return
        from cryptography import x509

        with open(path, "rb") as handle:
            cert = x509.load_pem_x509_certificate(handle.read())
        expiry = getattr(cert, "not_valid_after_utc", None) or cert.not_valid_after
        _set(tls_cert_expiry, expiry.timestamp())
    except Exception:
        pass


def refresh_all() -> None:
    """Refresh every operational gauge. Safe to call on a timer."""
    initialize_operational_metrics()
    probe_gpu()
    probe_models()
    probe_storage()
    probe_backups()
    probe_certificate()

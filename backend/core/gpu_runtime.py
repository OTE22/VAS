"""ONNX Runtime execution-provider selection and GPU verification.

Both model wrappers previously hardcoded

    providers=["CUDAExecutionProvider", "CPUExecutionProvider"]

which ignores USE_GPU entirely and, more importantly, hides failure: if the
CUDA provider cannot load, ONNX Runtime quietly drops it and uses the CPU one.
Inference still returns correct results, the logs stay clean and the healthcheck
passes — the service is just an order of magnitude slower, indefinitely, with
nothing reporting it.

Provider *discovery* is not proof either. `get_available_providers()` lists what
the build supports, not what successfully initialised for a given session. Only
`session.get_providers()` after construction says what will actually run, which
is what verify_session_providers checks.
"""

from __future__ import annotations

import logging
from typing import List, Optional, Sequence

logger = logging.getLogger(__name__)

CUDA = "CUDAExecutionProvider"
CPU = "CPUExecutionProvider"


class GpuUnavailableError(RuntimeError):
    """GPU inference was required but CUDA could not be used."""


def available_providers() -> List[str]:
    try:
        import onnxruntime as ort

        return list(ort.get_available_providers())
    except Exception as e:
        logger.warning("Could not query ONNX Runtime providers: %s", e)
        return []


def cuda_is_available() -> bool:
    return CUDA in available_providers()


def select_providers(cfg=None) -> List[str]:
    """The provider list to request when creating a session.

    CPU deployments never request CUDA, so a missing GPU is not an error there.
    GPU deployments request CUDA first, and include the CPU provider only when
    fallback is explicitly permitted — otherwise session creation fails loudly
    instead of degrading silently.
    """
    if cfg is None:
        from config import settings as cfg

    use_gpu = bool(getattr(cfg, "USE_GPU", False))
    allow_fallback = bool(getattr(cfg, "ALLOW_CPU_FALLBACK", True))

    if not use_gpu:
        return [CPU]
    if allow_fallback:
        return [CUDA, CPU]
    return [CUDA]


def verify_session_providers(session, model_name: str, cfg=None) -> None:
    """Confirm a constructed session is running where it was supposed to.

    Raises GpuUnavailableError when GPU mode is required and the session did not
    actually get CUDA.
    """
    if cfg is None:
        from config import settings as cfg

    use_gpu = bool(getattr(cfg, "USE_GPU", False))
    allow_fallback = bool(getattr(cfg, "ALLOW_CPU_FALLBACK", True))

    try:
        active = list(session.get_providers())
    except Exception as e:
        logger.warning("Could not read providers for %s: %s", model_name, e)
        return

    if not use_gpu:
        logger.info("%s: running on %s", model_name, active[0] if active else "unknown")
        return

    if active and active[0] == CUDA:
        logger.info("%s: running on CUDA", model_name)
        return

    message = (
        f"{model_name}: GPU mode is enabled but the session is running on "
        f"{active[0] if active else 'no provider'} (providers={active}). "
        "This is the silent-fallback case: results stay correct while "
        "throughput collapses. Check that the installed onnxruntime-gpu build "
        "matches the image's CUDA major version."
    )

    if allow_fallback:
        logger.warning("%s", message)
    else:
        raise GpuUnavailableError(message)


def run_inference_smoke_test(session, model_name: str) -> bool:
    """Run one real inference through the session.

    A session that constructs successfully can still fail on first execution —
    typically CUDA OOM or a missing kernel — so this executes the graph rather
    than trusting construction.
    """
    try:
        import numpy as np

        inputs = session.get_inputs()
        if not inputs:
            return False

        feed = {}
        for spec in inputs:
            shape = [
                1 if (isinstance(d, str) or d is None or d < 0) else d
                for d in spec.shape
            ]
            dtype = np.float32
            if "int64" in str(spec.type):
                dtype = np.int64
            elif "int32" in str(spec.type):
                dtype = np.int32
            feed[spec.name] = np.zeros(shape, dtype=dtype)

        session.run(None, feed)
        logger.info("%s: inference smoke test passed", model_name)
        return True
    except Exception as e:
        logger.error("%s: inference smoke test FAILED: %s: %s",
                     model_name, type(e).__name__, e)
        return False


def verify_gpu_readiness(cfg=None, *, sessions: Optional[Sequence] = None) -> None:
    """Startup gate for GPU deployments.

    1. CUDAExecutionProvider is registered
    2. every live session actually got CUDA
    3. a real inference executes

    No-op unless USE_GPU is set. Raises GpuUnavailableError when GPU inference
    was required and any step fails.
    """
    if cfg is None:
        from config import settings as cfg

    if not bool(getattr(cfg, "USE_GPU", False)):
        return

    allow_fallback = bool(getattr(cfg, "ALLOW_CPU_FALLBACK", True))
    providers = available_providers()

    if CUDA not in providers:
        message = (
            "GPU mode is enabled but CUDAExecutionProvider is not registered "
            f"(available: {providers}). The usual cause is an onnxruntime-gpu "
            "build compiled against a different CUDA major version than the "
            "base image provides."
        )
        if allow_fallback:
            logger.warning("%s", message)
            return
        raise GpuUnavailableError(message)

    for name, session in (sessions or []):
        if session is None:
            continue
        verify_session_providers(session, name, cfg)
        if not run_inference_smoke_test(session, name) and not allow_fallback:
            raise GpuUnavailableError(
                f"{name}: CUDA session failed its inference smoke test"
            )

    logger.info("GPU readiness verified: CUDA active and inference executing")

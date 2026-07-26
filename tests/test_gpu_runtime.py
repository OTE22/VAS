"""
GPU execution-provider selection and the silent-CPU-fallback guard.

    docker exec face_recognition_api python -m pytest tests/test_gpu_runtime.py -v

The regression these cover: models/scrfd.py and models/arcface.py hardcoded
providers=["CUDAExecutionProvider", "CPUExecutionProvider"] regardless of
USE_GPU. When the CUDA provider could not load — which is what an
onnxruntime-gpu build compiled for a different CUDA major version does — ONNX
Runtime dropped it and ran on the CPU. Results stayed correct, logs stayed
clean, the healthcheck passed, and throughput collapsed with nothing reporting
it.

These run on a CPU-only host: the session objects are doubles, so no GPU is
needed to test the GPU policy.
"""

from types import SimpleNamespace

import pytest

from backend.core.gpu_runtime import (
    CPU,
    CUDA,
    GpuUnavailableError,
    select_providers,
    verify_gpu_readiness,
    verify_session_providers,
)


def cfg(use_gpu=False, allow_fallback=True):
    return SimpleNamespace(USE_GPU=use_gpu, ALLOW_CPU_FALLBACK=allow_fallback)


class FakeSession:
    """Stands in for an ort.InferenceSession."""

    def __init__(self, providers):
        self._providers = list(providers)

    def get_providers(self):
        return list(self._providers)

    def get_inputs(self):
        return []


# ------------------------------------------------------- provider selection

def test_cpu_deployment_never_requests_cuda():
    """A CPU deployment asking for CUDA logs confusing warnings for no reason."""
    assert select_providers(cfg(use_gpu=False)) == [CPU]


def test_gpu_deployment_requests_cuda_first():
    providers = select_providers(cfg(use_gpu=True, allow_fallback=True))
    assert providers[0] == CUDA
    assert CPU in providers


def test_gpu_without_fallback_refuses_to_offer_cpu():
    """Omitting the CPU provider makes session creation fail loudly instead of
    degrading silently."""
    assert select_providers(cfg(use_gpu=True, allow_fallback=False)) == [CUDA]


# --------------------------------------------------- session verification

def test_cuda_session_passes_verification():
    verify_session_providers(FakeSession([CUDA, CPU]), "SCRFD", cfg(use_gpu=True))


def test_cpu_session_in_cpu_mode_passes():
    verify_session_providers(FakeSession([CPU]), "SCRFD", cfg(use_gpu=False))


def test_silent_fallback_raises_when_fallback_disabled():
    """The core regression: GPU requested, CPU actually running."""
    with pytest.raises(GpuUnavailableError) as exc:
        verify_session_providers(
            FakeSession([CPU]), "SCRFD", cfg(use_gpu=True, allow_fallback=False)
        )
    assert "silent-fallback" in str(exc.value)


def test_silent_fallback_only_warns_when_fallback_allowed():
    verify_session_providers(
        FakeSession([CPU]), "SCRFD", cfg(use_gpu=True, allow_fallback=True)
    )


def test_cuda_must_be_first_not_merely_present():
    """A session listing CUDA after CPU executes on the CPU."""
    with pytest.raises(GpuUnavailableError):
        verify_session_providers(
            FakeSession([CPU, CUDA]), "ArcFace", cfg(use_gpu=True, allow_fallback=False)
        )


def test_verification_tolerates_a_broken_session_object():
    class Broken:
        def get_providers(self):
            raise RuntimeError("session closed")

    verify_session_providers(Broken(), "SCRFD", cfg(use_gpu=True, allow_fallback=False))


# ----------------------------------------------------------- readiness gate

def test_readiness_is_a_noop_for_cpu_deployments():
    """This host has no NVIDIA GPU; a CPU deployment must not fail here."""
    verify_gpu_readiness(cfg(use_gpu=False), sessions=[("SCRFD", FakeSession([CPU]))])


def test_readiness_raises_without_cuda_when_fallback_disabled(monkeypatch):
    monkeypatch.setattr(
        "backend.core.gpu_runtime.available_providers", lambda: [CPU]
    )
    with pytest.raises(GpuUnavailableError) as exc:
        verify_gpu_readiness(cfg(use_gpu=True, allow_fallback=False))
    assert "CUDAExecutionProvider is not registered" in str(exc.value)


def test_readiness_warns_without_cuda_when_fallback_allowed(monkeypatch):
    monkeypatch.setattr(
        "backend.core.gpu_runtime.available_providers", lambda: [CPU]
    )
    verify_gpu_readiness(cfg(use_gpu=True, allow_fallback=True))


def test_readiness_skips_sessions_that_failed_to_load(monkeypatch):
    monkeypatch.setattr(
        "backend.core.gpu_runtime.available_providers", lambda: [CUDA, CPU]
    )
    monkeypatch.setattr(
        "backend.core.gpu_runtime.run_inference_smoke_test", lambda s, n: True
    )
    verify_gpu_readiness(
        cfg(use_gpu=True, allow_fallback=False), sessions=[("SCRFD", None)]
    )


def test_readiness_fails_when_the_smoke_test_fails(monkeypatch):
    """Provider discovery is not proof: a CUDA session can still fail to run."""
    monkeypatch.setattr(
        "backend.core.gpu_runtime.available_providers", lambda: [CUDA, CPU]
    )
    monkeypatch.setattr(
        "backend.core.gpu_runtime.run_inference_smoke_test", lambda s, n: False
    )
    with pytest.raises(GpuUnavailableError) as exc:
        verify_gpu_readiness(
            cfg(use_gpu=True, allow_fallback=False),
            sessions=[("SCRFD", FakeSession([CUDA, CPU]))],
        )
    assert "smoke test" in str(exc.value)


# -------------------------------------------------- source-level regressions

def _read(path):
    with open(path, encoding="utf-8") as handle:
        return handle.read()


@pytest.mark.parametrize("path", ["/app/models/scrfd.py", "/app/models/arcface.py"])
def test_models_no_longer_hardcode_the_provider_list(path):
    source = _read(path)
    assert 'providers=["CUDAExecutionProvider", "CPUExecutionProvider"]' not in source
    assert "select_providers()" in source
    assert "verify_session_providers" in source


def _requirement_lines(path):
    """Actual requirements, excluding comments and blanks."""
    return [
        line.strip()
        for line in _read(path).splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


def test_gpu_requirements_pin_onnxruntime_exactly():
    """An unpinned onnxruntime-gpu resolves to whatever is newest at build
    time, which is how the CUDA major version drifted away from the base
    image."""
    requirements = _requirement_lines("/app/requirements-gpu.txt")
    ort_lines = [line for line in requirements if line.startswith("onnxruntime")]
    assert ort_lines, "onnxruntime-gpu must be declared"
    for line in ort_lines:
        assert "==" in line, f"unpinned ONNX Runtime requirement: {line}"
        assert ">=" not in line, f"floor-pinned ONNX Runtime requirement: {line}"


def test_gpu_requirements_declare_only_one_opencv():
    """opencv-contrib-python and opencv-python-headless both provide cv2, so
    installing both makes the effective version depend on install order."""
    requirements = _requirement_lines("/app/requirements-gpu.txt")
    opencv = [line for line in requirements if line.startswith("opencv")]
    assert len(opencv) == 1, f"conflicting OpenCV distributions: {opencv}"


def test_gpu_dockerfile_cuda_version_matches_the_pinned_runtime():
    dockerfile = _read("/app/docker/Dockerfile.gpu")
    requirements = _read("/app/requirements-gpu.txt")
    assert "nvidia/cuda:12" in dockerfile, "onnxruntime-gpu 1.20.x needs CUDA 12"
    assert "onnxruntime-gpu==1.20" in requirements


def test_gpu_compose_pins_single_worker():
    """Multiple workers each load their own CUDA session onto one GPU, and
    every single-flight guard in the codebase is process-local."""
    compose = _read("/app/docker/docker-compose.gpu.yml")
    assert "WORKERS: 1" in compose
    assert "WORKERS: 16" not in compose


def test_gpu_compose_disables_cpu_fallback():
    compose = _read("/app/docker/docker-compose.gpu.yml")
    assert 'ALLOW_CPU_FALLBACK: "false"' in compose


def test_gpu_compose_does_not_override_the_entrypoint():
    """Overriding entrypoint skipped the config preflight, the permission fixes
    and the gosu privilege drop."""
    compose = _read("/app/docker/docker-compose.gpu.yml")
    assert 'entrypoint: ["/bin/sh", "-c"]' not in compose


def test_gpu_compose_has_no_windows_host_path():
    compose = _read("/app/docker/docker-compose.gpu.yml")
    assert "C:\\\\Users" not in compose
    assert "ollama_models" in compose

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

import re

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
    """Actual requirements, excluding comments and blanks, with `-r` includes
    expanded.

    requirements-gpu.txt is now thin — it `-r requirements-base.txt` and adds
    only what differs by hardware. Reading it literally would miss opencv,
    numpy and everything else shared, so a test asserting on those would fail
    for a dependency that is in fact installed.

    `--extra-index-url` and other pip flags are dropped: they are directives,
    not requirements, and would otherwise be matched by startswith() checks.
    """
    import os

    lines = []
    for raw in _read(path).splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("-r "):
            included = os.path.join(os.path.dirname(path), line[3:].strip())
            lines.extend(_requirement_lines(included))
            continue
        if line.startswith("-"):          # --extra-index-url, --find-links, ...
            continue
        lines.append(line)
    return lines


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


def test_gpu_dockerfile_removes_the_cpu_onnxruntime_chromadb_drags_in():
    """chromadb hard-depends on `onnxruntime` — the CPU build — because its
    default embedding function runs ONNXMiniLM on it, and
    sql_agent/knowledge_base.py creates its collection without passing an
    embedding_function. So the GPU image installs onnxruntime-gpu (requested)
    AND onnxruntime (transitive), and both unpack into the same
    `onnxruntime/` package directory.

    With both present the build that loads depends on installation order. Lose
    that race and CUDAExecutionProvider is simply absent: inference is correct,
    logs are clean, the healthcheck passes, and the GPU sits idle — the same
    silent fallback the pinned CUDA version exists to prevent.

    A requirements-file check cannot catch this; the conflict is transitive, so
    the fix has to live in the Dockerfile and be asserted there.
    """
    dockerfile = _read("/app/docker/Dockerfile.gpu")
    assert "pip uninstall -y onnxruntime" in dockerfile, (
        "Dockerfile.gpu does not remove the CPU onnxruntime that chromadb "
        "installs, so the GPU image ships both builds in the same directory")
    assert "--force-reinstall" in dockerfile, (
        "the CPU onnxruntime is uninstalled without reinstalling the GPU "
        "build; `pip uninstall onnxruntime` deletes files onnxruntime-gpu "
        "also owns, leaving a broken installation")
    assert "chromadb" in dockerfile, (
        "the step is present but unexplained; the next person will remove it")


def test_chromadb_still_forces_the_cpu_onnxruntime():
    """The remediation above only earns its place while chromadb keeps that
    dependency. If a future chromadb makes onnxruntime optional, this fails and
    the Dockerfile step should go.
    """
    from importlib.metadata import PackageNotFoundError, requires
    try:
        declared = requires("chromadb") or []
    except PackageNotFoundError:
        pytest.skip("chromadb is not installed in this image")
    pulls_ort = [r for r in declared if r.split(";")[0].strip().lower().startswith("onnxruntime")]
    if not pulls_ort:
        pytest.fail(
            "chromadb no longer requires onnxruntime — the uninstall/reinstall "
            "dance in docker/Dockerfile.gpu is now dead weight and should be "
            "removed along with this test")
    unconditional = [r for r in pulls_ort if ";" not in r]
    assert unconditional, (
        f"chromadb's onnxruntime dependency is now conditional ({pulls_ort}); "
        f"check whether the GPU remediation is still needed")


def test_gpu_dockerfile_cuda_version_matches_the_pinned_runtime():
    dockerfile = _read("/app/docker/Dockerfile.gpu")
    requirements = _read("/app/requirements-gpu.txt")
    assert "nvidia/cuda:12" in dockerfile, "onnxruntime-gpu 1.20.x needs CUDA 12"
    assert "onnxruntime-gpu==1.20" in requirements


def test_the_dockerfile_does_not_keep_its_own_copy_of_the_onnxruntime_pin():
    """The version must live in exactly one place.

    Dockerfile.gpu reinstalls the CUDA runtime after removing the CPU build
    chromadb drags in. When that step hard-coded its own `onnxruntime-gpu==X`,
    there were two numbers to keep in step and the reinstall silently won --
    so the image could ship a different build from the one pinned in the
    requirements file. It now greps the pin out of requirements-gpu.txt.
    """
    dockerfile = _read("/app/docker/Dockerfile.gpu")
    hardcoded = re.findall(r"onnxruntime-gpu==[0-9][^\s\"']*", dockerfile)
    assert not hardcoded, (
        f"docker/Dockerfile.gpu hard-codes {hardcoded}; read the pin from "
        f"requirements-gpu.txt instead so the two cannot drift")


def test_the_pinned_onnxruntime_gpu_version_was_actually_released():
    """`onnxruntime-gpu==1.20.1` does not exist.

    The 1.20 line on PyPI is 1.20.0 and 1.20.2 — there is no 1.20.1. That pin
    sat in requirements-gpu.txt and would have failed the GPU build outright
    with "No matching distribution found"; nothing caught it because the GPU
    image was never built.

    This test cannot reach PyPI, so it guards the one case known to be wrong
    rather than pretending to validate every version. Confirm a new pin with:

        docker run --rm python:3.11-slim sh -c \\
          'pip install --dry-run --no-deps onnxruntime-gpu==<version>'
    """
    requirements = _read("/app/requirements-gpu.txt")
    pins = re.findall(r"^onnxruntime-gpu==([0-9][^\s#]*)", requirements, re.M)
    assert pins, "onnxruntime-gpu is no longer pinned in requirements-gpu.txt"
    assert "1.20.1" not in pins, (
        "onnxruntime-gpu 1.20.1 was never released — the 1.20 line is 1.20.0 "
        "and 1.20.2. This pin fails the GPU build with 'No matching "
        "distribution found'.")


# docker-compose.gpu.yml is a development OVERRIDE layered on
# docker-compose.cpu.yml, not a standalone stack, so what a GPU deployment
# actually runs is the pair. Inherited settings (WORKERS, the ollama mount)
# live in the base. Concatenating the two files is enough for these
# presence assertions — the suite runs inside the container and has no docker
# CLI to render the real merge with.
GPU_STACK_FILES = ("/app/docker/docker-compose.cpu.yml",
                   "/app/docker/docker-compose.gpu.yml")


def _read_gpu_stack():
    return "\n".join(_read(path) for path in GPU_STACK_FILES)


def test_gpu_compose_pins_single_worker():
    """Multiple workers each load their own CUDA session onto one GPU, and
    every single-flight guard in the codebase is process-local."""
    compose = _read_gpu_stack()
    assert "WORKERS: 1" in compose
    assert "WORKERS: 16" not in compose
    # The override must not raise it back up.
    assert not re.search(r'^\s*WORKERS:\s*"?(?!1"?\s*$)\d+',
                         _read("/app/docker/docker-compose.gpu.yml"), re.M), (
        "the GPU override sets WORKERS to something other than 1")


def test_gpu_compose_disables_cpu_fallback():
    compose = _read("/app/docker/docker-compose.gpu.yml")
    assert 'ALLOW_CPU_FALLBACK: "false"' in compose


def test_gpu_compose_does_not_override_the_entrypoint():
    """Overriding entrypoint skipped the config preflight, the permission fixes
    and the gosu privilege drop."""
    compose = _read("/app/docker/docker-compose.gpu.yml")
    assert 'entrypoint: ["/bin/sh", "-c"]' not in compose


def test_gpu_compose_has_no_windows_host_path():
    """cpu.yml mounted "C:/Users/Raven/.ollama" — one developer's home
    directory — so the GPU pair inherited it too, and the repository was
    unusable on any other machine.

    Matches a MOUNT, not the mere mention of a path: the comment recording
    what was removed necessarily quotes the old value, and a substring check
    would forbid explaining the fix.
    """
    mount = re.compile(r"""^\s*-\s*["']?[A-Za-z]:[/\\][^\n:]*:""", re.M)
    for path in GPU_STACK_FILES:
        offenders = mount.findall(_read(path))
        assert not offenders, (
            f"{path} mounts an absolute host path from a developer machine: "
            f"{offenders}")
    assert "ollama_models" in _read_gpu_stack(), (
        "the ollama models volume is gone; models would be lost on recreate")

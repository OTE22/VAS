"""CPU and GPU dependency sets stay separate, and stay in step.

    docker exec face_recognition_api python -m pytest tests/test_dependency_separation.py -v

None of this was covered before, and the gap was expensive in both directions:

  * The CPU-only image shipped the full NVIDIA CUDA wheel set — roughly 4.5 GB
    of GPU runtime — because `sentence-transformers` pulls `torch`, and the
    default Linux torch wheel is the CUDA build. Nothing imports torch: a
    repo-wide search for the string finds no Python file. The build was
    optimised *around* the download rather than eliminating it.
  * The GPU image had no `sentence-transformers` at all, so
    generate_query_embedding() always took its ImportError branch and semantic
    query-history search was permanently dead there — while Dockerfile.gpu
    provisioned a HuggingFace cache for the library it never installed.

Both were invisible because the two requirement files were standalone copies
and no test compared them.
"""

import os
import re
import sys

import pytest

REPO = "/app"
BASE = f"{REPO}/requirements-base.txt"
CPU = f"{REPO}/requirements-cpu.txt"
GPU = f"{REPO}/requirements-gpu.txt"

# Distributions that exist only to serve an NVIDIA GPU. `triton` is torch's
# CUDA kernel compiler and arrives with the CUDA wheel, never the CPU one.
CUDA_ONLY = re.compile(
    r"^(nvidia[-_]|cuda[-_]|triton\b|tensorrt\b|cupy|pycuda|faiss-gpu\b|onnxruntime-gpu\b)",
    re.I)


def _read(path):
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def _requirements(path):
    """Requirement specifiers with `-r` includes expanded and pip flags dropped."""
    out = []
    for raw in _read(path).splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        if line.startswith("-r "):
            out.extend(_requirements(os.path.join(os.path.dirname(path), line[3:].strip())))
        elif not line.startswith("-"):
            out.append(line)
    return out


def _names(path):
    """Distribution names only, lowercased, extras and versions stripped."""
    return {re.split(r"[\[<>=!~ ]", spec, 1)[0].strip().lower()
            for spec in _requirements(path)}


def _directives(path):
    return [line.strip() for line in _read(path).splitlines()
            if line.strip().startswith("--")]


# ---------------------------------------------------------------------------
# structure
# ---------------------------------------------------------------------------

def test_the_shared_base_exists_and_both_targets_include_it():
    assert os.path.isfile(BASE), "requirements-base.txt is gone"
    for path in (CPU, GPU):
        assert any(line.strip() == "-r requirements-base.txt"
                   for line in _read(path).splitlines()), (
            f"{path} does not include the shared base, so the two hardware "
            f"targets will drift apart again")


def test_the_hardware_files_stay_thin():
    """Anything not hardware-specific belongs in the base file. If these grow,
    the duplication that caused the original drift is coming back."""
    for path in (CPU, GPU):
        own = [spec for spec in _read(path).splitlines()
               if spec.strip() and not spec.strip().startswith(("#", "-"))]
        assert len(own) <= 6, (
            f"{path} declares {len(own)} packages of its own: {own}. Only the "
            f"ONNX Runtime build, the FAISS build, torch and "
            f"sentence-transformers legitimately differ by hardware.")


def test_no_package_is_declared_in_both_the_base_and_a_hardware_file():
    base = _names(BASE)
    for path in (CPU, GPU):
        own = {re.split(r"[\[<>=!~ ]", line.strip(), 1)[0].strip().lower()
               for line in _read(path).splitlines()
               if line.strip() and not line.strip().startswith(("#", "-"))}
        overlap = base & own
        assert not overlap, (
            f"{path} re-declares {overlap}, which the base file already "
            f"provides — the two can now specify different versions")


# ---------------------------------------------------------------------------
# the headline rule
# ---------------------------------------------------------------------------

def test_the_cpu_target_declares_no_gpu_only_package():
    offenders = [spec for spec in _requirements(CPU) if CUDA_ONLY.match(spec)]
    assert not offenders, (
        f"the CPU dependency set declares GPU-only packages: {offenders}")


def test_the_cpu_target_pins_torch_to_the_cpu_wheel_index():
    """Without the CPU index URL, pip resolves the default Linux torch wheel,
    which is the CUDA build: nvidia-cublas, nvidia-cudnn, nvidia-nccl, triton
    and the rest — several GB of GPU runtime in an image with no GPU."""
    specs = _requirements(CPU)
    if not any(re.match(r"^torch\b", s) for s in specs):
        pytest.skip("torch is no longer a declared dependency on CPU")
    assert any("download.pytorch.org/whl/cpu" in d for d in _directives(CPU)), (
        "requirements-cpu.txt declares torch without pointing at the CPU wheel "
        "index, so the CUDA build (~4.5 GB of NVIDIA wheels) is installed into "
        "an image that never calls torch")


def test_the_gpu_target_uses_the_cuda_onnxruntime_and_the_cpu_torch():
    """ONNX Runtime is where the acceleration actually matters — SCRFD and
    ArcFace run on it. torch is only used for a 384-dim MiniLM embedding, so
    the CPU wheel is deliberate there; see the comment in requirements-gpu.txt.
    """
    names = _names(GPU)
    assert "onnxruntime-gpu" in names, "the GPU target lost the CUDA ONNX Runtime"
    assert "onnxruntime" not in names, (
        "both onnxruntime and onnxruntime-gpu are declared; they provide the "
        "same module, so the effective build depends on install order")
    if "torch" in names:
        assert any("download.pytorch.org/whl/cpu" in d for d in _directives(GPU)), (
            "the GPU target installs the CUDA torch build. Nothing in this "
            "application calls torch on the GPU — it would add several GB and "
            "contend with SCRFD/ArcFace for the same device.")


def test_the_two_targets_agree_on_every_shared_package():
    """The files were standalone copies and had already drifted:
    sentence-transformers and tqdm existed only on CPU, and Pillow was pinned
    on CPU but unpinned on GPU, so one commit could ship two Pillow majors."""
    cpu, gpu = dict(), dict()
    for target, out in ((CPU, cpu), (GPU, gpu)):
        for spec in _requirements(target):
            out[re.split(r"[\[<>=!~ ]", spec, 1)[0].strip().lower()] = spec

    hardware_specific = {"onnxruntime", "onnxruntime-gpu", "faiss-cpu", "faiss-gpu"}
    mismatched = {
        name: (cpu[name], gpu[name])
        for name in (set(cpu) & set(gpu)) - hardware_specific
        if cpu[name] != gpu[name]
    }
    assert not mismatched, (
        f"CPU and GPU specify different versions of shared packages: {mismatched}")


def test_query_embedding_support_is_present_on_both_targets():
    """sentence-transformers was missing from the GPU file entirely, so
    semantic query-history search was dead on every GPU deployment while the
    GPU Dockerfile still provisioned a HuggingFace cache for it."""
    for path in (CPU, GPU):
        assert "sentence-transformers" in _names(path), (
            f"{path} has no sentence-transformers, so "
            f"generate_query_embedding() always returns None there")


# ---------------------------------------------------------------------------
# declared vs imported
# ---------------------------------------------------------------------------

def test_packages_imported_by_the_application_are_declared():
    """These were imported under try/except while appearing in no requirements
    file, so the fallback path fired for a missing dependency rather than for a
    deliberate choice."""
    for name in ("psutil", "hdbscan", "scipy"):
        assert name in _names(CPU), f"{name} is imported but not declared for CPU"
        assert name in _names(GPU), f"{name} is imported but not declared for GPU"


def test_every_third_party_module_the_source_imports_is_installed():
    """The real guard against over-pruning.

    A package with no direct import can still be load-bearing through its
    TRANSITIVE dependencies. Removing `pandas` (nothing imports it) took
    `pyarrow` with it, and ML training failed with "No module named
    'pyarrow'" — `backend/ml/dataset_builder.py` imports pyarrow directly.
    Removing `python-jose[cryptography]` took `cryptography`, silencing the
    TLS-expiry metric in `backend/core/operational_metrics.py`.

    Both were invisible to a name-based "is this package imported?" search.
    This checks the only thing that matters: can the interpreter import what
    the source asks for?
    """
    import ast
    import importlib.util

    stdlib = set(sys.stdlib_module_names)
    local = {"backend", "sql_agent", "models", "utils", "scripts", "config",
             "db_models", "db_connection", "alembic", "conftest", "tests"}
    # A script importing a SIBLING script (scripts/map_data/tile_probe.py ->
    # coverage_check) is a local import, not a missing third-party package.
    # Derive those names from the tree instead of maintaining an allowlist.
    for package in ("backend", "sql_agent", "models", "utils", "scripts"):
        root = os.path.join(REPO, package)
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d != "__pycache__"]
            local.update(f[:-3] for f in filenames if f.endswith(".py"))

    imported = {}
    # scripts/ is included: those files ship in the image and are run
    # operationally (the folium vendoring step runs at BUILD time, and a
    # missing import there silently produces an image whose maps fetch Leaflet
    # from a CDN).
    for package in ("backend", "sql_agent", "models", "utils", "scripts"):
        root = os.path.join(REPO, package)
        if not os.path.isdir(root):
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d != "__pycache__"]
            for filename in filenames:
                if not filename.endswith(".py"):
                    continue
                path = os.path.join(dirpath, filename)
                try:
                    with open(path, encoding="utf-8", errors="replace") as handle:
                        tree = ast.parse(handle.read())
                except SyntaxError:
                    continue
                for node in ast.walk(tree):
                    if isinstance(node, ast.Import):
                        for alias in node.names:
                            imported.setdefault(alias.name.split(".")[0], path)
                    elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                        imported.setdefault(node.module.split(".")[0], path)

    missing = {
        module: os.path.relpath(path, REPO)
        for module, path in sorted(imported.items())
        if module not in stdlib and module not in local
        and not module.startswith("_")
        and importlib.util.find_spec(module) is None
    }
    assert not missing, (
        "the source imports modules that are not installed — a dependency was "
        "removed that something still needs, directly or transitively: "
        f"{missing}")


def test_the_dev_extras_do_not_redeclare_a_runtime_package():
    """requirements-dev.txt is installed AFTER the runtime set, so anything it
    re-declares overrides production's resolution — and the suite then
    exercises versions production never runs. It pinned `requests==2.32.5`
    while the runtime leaves requests unpinned, so every test image was
    downgraded from the 2.34.x a clean production build installs."""
    dev = f"{REPO}/requirements-dev.txt"
    if not os.path.isfile(dev):
        pytest.skip("no dev requirements file")
    runtime = _names(CPU) | _names(GPU)
    overlap = _names(dev) & runtime
    assert not overlap, (
        f"requirements-dev.txt re-declares runtime packages {overlap}; a pin "
        f"there silently replaces the version production installs")


def test_the_dockerfiles_copy_the_shared_base():
    """`-r requirements-base.txt` is resolved relative to the file, so a
    Dockerfile that copies only the hardware file fails at build time."""
    for dockerfile in (f"{REPO}/docker/Dockerfile.cpu", f"{REPO}/docker/Dockerfile.gpu"):
        source = _read(dockerfile)
        assert "requirements-base.txt" in source, (
            f"{dockerfile} does not COPY requirements-base.txt; the pip install "
            f"cannot resolve the -r include")

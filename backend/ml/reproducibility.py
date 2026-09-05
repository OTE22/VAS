"""Explicit runtime allowlist: never export credentials or the environment."""
import hashlib
import importlib.metadata
import os
import platform
import subprocess
from pathlib import Path


def capture(seed, parameters, dataset=None, pipeline=None, require_clean=False):
    from backend.ml.registry_service import RegistryError
    root = Path(__file__).resolve().parents[2]
    def git(*args):
        try:
            return subprocess.check_output(["git", "-C", str(root), *args], stderr=subprocess.DEVNULL, timeout=5).decode().strip()
        except Exception:
            return None
    commit, status = git("rev-parse", "HEAD"), git("status", "--porcelain", "--untracked-files=normal")
    if require_clean and (not commit or status is None or status):
        raise RegistryError("REPRODUCIBILITY_CODE_UNVERIFIED", "A clean, identifiable Git checkout is required for this run")
    digest = hashlib.sha256()
    for directory in (root / "backend" / "ml",):
        for file in sorted(directory.glob("*.py")):
            digest.update(file.name.encode()); digest.update(file.read_bytes())
    versions = {}
    for package in ("numpy", "scikit-learn", "pyarrow", "mlflow", "xgboost", "xgboost-cpu", "optuna", "shap"):
        try:
            versions[package] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            pass
    return {"manifest_version": 1, "git_commit": commit, "git_dirty": None if status is None else bool(status),
            "training_source_sha256": digest.hexdigest(), "seed": seed, "parameters": parameters,
            "dataset": dataset, "pipeline": pipeline, "dependencies": versions,
            "environment_dependencies": {d.metadata["Name"]: d.version for d in importlib.metadata.distributions() if d.metadata.get("Name")},
            "runtime": {"python": platform.python_version(), "platform": platform.system(),
                        "machine": platform.machine(), "cpu_count": os.cpu_count(), "execution": "CPU"}}

"""Explicit runtime allowlist: never export credentials or the environment."""
import hashlib
import importlib.metadata
import os
import platform
from pathlib import Path


def capture(seed, parameters, dataset=None, pipeline=None, require_clean=False):
    from backend.ml.registry_service import RegistryError
    root = Path(__file__).resolve().parents[2]
    from backend.ml.build_provenance import inspect_provenance
    code_identity = inspect_provenance(root)
    commit, dirty = code_identity.get("git_commit"), code_identity.get("git_dirty")
    if require_clean and not code_identity["verified"]:
        raise RegistryError("REPRODUCIBILITY_CODE_UNVERIFIED", code_identity["reason"])
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
    return {"manifest_version": 2, "git_commit": commit, "git_dirty": dirty,
            "code_identity": code_identity,
            "training_source_sha256": digest.hexdigest(), "seed": seed, "parameters": parameters,
            "dataset": dataset, "pipeline": pipeline, "dependencies": versions,
            "environment_dependencies": {d.metadata["Name"]: d.version for d in importlib.metadata.distributions() if d.metadata.get("Name")},
            "runtime": {"python": platform.python_version(), "platform": platform.system(),
                        "machine": platform.machine(), "cpu_count": os.cpu_count(), "execution": "CPU"}}

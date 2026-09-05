"""Capability availability is independent of any per-run opt-in."""
import importlib
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlsplit
from config import settings
from backend.ml.registry_service import RegistryError


@lru_cache(maxsize=16)
def dependency(module):
    try:
        loaded = importlib.import_module(module)
        return True, getattr(loaded, "__version__", None)
    except Exception:
        return False, None


def tracking_uri():
    uri = settings.MLFLOW_TRACKING_URI.strip()
    if uri:
        try:
            parsed = urlsplit(uri)
        except ValueError:
            raise RegistryError("MLFLOW_URI_INVALID", "Use a valid credential-free HTTPS tracking URL")
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise RegistryError("MLFLOW_URI_INVALID", "Use a credential-free HTTPS tracking URL, or leave it empty for managed local storage")
        return uri.rstrip("/")
    root = Path(settings.ML_ARTIFACT_DIR).resolve() / "tracking"
    return "sqlite:///" + str(root / "mlflow.db")


def capability_registry():
    result = {key: {"status": "Available", "configured": True, "implemented": True,
                   "dependency_available": True, "operational": True, "action": note}
              for key, note in {
                  "lineage": "Immutable snapshots and checksums are mandatory.",
                  "validation": "Existing schema, type, range, missingness and leakage gates run before fitting.",
                  "reproducibility": "Each run records code, data, seed, parameters, dependencies and runtime."}.items()}
    for name in ("mlflow", "xgboost", "optuna", "shap"):
        enabled = bool(getattr(settings, name.upper() + "_ENABLED"))
        available, version = dependency(name)
        status = "Disabled" if not enabled else "Unavailable" if not available else "Available"
        action = "Enable in Admin Settings." if not enabled else "Rebuild the API and worker with requirements-cpu.txt or requirements-gpu.txt, then restart." if not available else "Ready."
        if name == "mlflow" and enabled and available:
            try:
                tracking_uri()
                root = Path(settings.ML_ARTIFACT_DIR).resolve()
                import os
                existing = root
                while not existing.exists() and existing != existing.parent:
                    existing = existing.parent
                if not existing.is_dir() or not os.access(existing, os.W_OK | os.X_OK):
                    raise RegistryError("ML_STORAGE_NOT_WRITABLE", "Grant the service write access to ML artifact storage")
                if not settings.MLFLOW_EXPERIMENT_NAME.strip():
                    raise RegistryError("MLFLOW_EXPERIMENT_INVALID", "Set a nonempty MLflow experiment name in Admin Settings")
            except RegistryError as exc:
                status, action = "Misconfigured", exc.message
        result[name] = {"status": status, "version": version, "configured": enabled,
                        "implemented": True, "dependency_available": available,
                        "operational": status == "Available", "action": action}
    if result["xgboost"]["operational"]:
        result["xgboost"]["device_policy"] = "Automatic CUDA when a real XGBoost probe succeeds; otherwise CPU. The actual run device and fallback reason are recorded."
    result["drift"] = {"status": "Disabled", "action": "Production drift is deferred until deployed models have real inference data; historical reports remain available."}
    return result


def require_capability(name):
    item = capability_registry()[name]
    if item["status"] != "Available":
        raise RegistryError(name.upper() + "_" + item["status"].upper(), item["action"])

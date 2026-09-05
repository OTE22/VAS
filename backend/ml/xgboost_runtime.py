"""Probe the XGBoost runtime itself; ONNX provider availability is not proof.

Automatic CUDA selection, with one explicit CPU retry for GPU runtime failures.
The chosen device and reason travel with each fitted model and its manifest.
"""
import json
import logging
import warnings
from config import settings

logger = logging.getLogger(__name__)


def actual_device(estimator):
    config = json.loads(estimator.get_booster().save_config())
    return config["learner"]["generic_param"]["device"]


def select_device():
    import numpy as np
    import xgboost as xgb
    if not xgb.build_info().get("USE_CUDA", False):
        return {"requested": "auto", "device": "cpu", "fallback_reason": "Installed XGBoost build has no CUDA support. GPU deployments use requirements-gpu.txt."}
    try:
        # A tiny real fit detects hidden devices, missing drivers and silent SDK fallback.
        probe = xgb.XGBRegressor(n_estimators=1, max_depth=1, tree_method="hist", device="cuda", n_jobs=1)
        with warnings.catch_warnings(record=True):
            probe.fit(np.array([[0.], [1.], [2.], [3.]]), np.array([0., 1., 2., 3.]))
        device = actual_device(probe)
        if device.startswith("cuda"):
            return {"requested": "auto", "device": device, "fallback_reason": None}
    except Exception:
        pass
    return {"requested": "auto", "device": "cpu", "fallback_reason": "CUDA could not initialize a usable XGBoost device. Check GPU visibility, driver compatibility and available memory; this run uses CPU."}


def fit_with_fallback(cls, params, x, y, execution, **fit_kwargs):
    from xgboost.core import XGBoostError
    def fit():
        model = cls(**params, device=execution["device"], n_jobs=settings.ML_TRAIN_MAX_THREADS, tree_method="hist")
        model.fit(x, y, **fit_kwargs)
        return model
    try:
        model = fit()
    except XGBoostError as exc:
        # Data/configuration failures must remain failures; only GPU runtime errors retry.
        if not execution["device"].startswith("cuda") or not any(token in str(exc).lower() for token in ("cuda", "gpu", "nccl", "out of memory")):
            raise
        execution.update(device="cpu", fallback_reason="XGBoost GPU training failed; retried the same data, seed and parameters on CPU.")
        logger.warning("[ML_OPS] XGBoost GPU fit unavailable; retrying on CPU")
        model = fit()
    observed = actual_device(model)
    if execution["device"].startswith("cuda") and not observed.startswith("cuda"):
        execution["fallback_reason"] = "XGBoost selected CPU during fitting because the requested GPU was unavailable."
    execution["device"] = observed
    model._platform_execution = dict(execution)
    return model

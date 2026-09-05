"""Portable shared train/serve scoring; exported unchanged with MLflow models."""
import math
from typing import Any, Dict, List

class RegistryError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def preprocess_feature_vector(payload: Dict[str, Any], features: Dict[str, Any]):
    """The ONE train/serve preprocessing rule: present -> float(value);
    missing -> the artifact's training median for that feature. Returns
    (vector, missing_feature_names). Raises KeyError when the artifact has
    no median for a scored feature (validate_artifact refuses such artifacts
    before they are ever cached)."""
    medians = payload["imputation_medians"]
    vector, missing = [], []
    for name in payload["feature_names"]:
        if name in features and features[name] is not None:
            vector.append(float(features[name]))
        else:
            missing.append(name)
            vector.append(float(medians[name]))
    return vector, missing


def score_with_payload(payload: Dict[str, Any], matrix) -> List[float]:
    """One train/serve implementation; regression remains in original target units.

    Unsupervised artifacts return relative anomaly scores. Classifier
    artifacts return relative ranking scores; their model contract explicitly
    prevents callers from presenting those values as calibrated threat
    probabilities.
    """
    import numpy as np
    algorithm = payload["algorithm"]
    if algorithm == "isolation_forest":
        raw = -payload["model"].score_samples(matrix)  # higher = more anomalous
    elif algorithm == "mad_baseline":
        params = payload["model"]  # {feature: {median, mad}}
        names = payload["feature_names"]
        z_scores = []
        for row in np.asarray(matrix):
            zs = []
            for i, name in enumerate(names):
                mad = params[name]["mad"]
                if mad <= 0:
                    continue
                zs.append(abs(row[i] - params[name]["median"]) / (1.4826 * mad))
            z_scores.append(max(zs) if zs else 0.0)
        raw = np.array(z_scores)
    elif algorithm == "xgboost_regressor":
        return [float(v) for v in payload["model"].predict(matrix)]
    elif algorithm in ("logreg", "random_forest", "gradient_boosting", "xgboost_classifier"):
        # Classifier output is intentionally named a rank score by the model
        # contract.  Without an independently validated calibration study it
        # must not be presented as a probability of threat.
        raw = payload["model"].predict_proba(matrix)[:, 1]
        return [float(v) for v in raw]
    else:
        raise RegistryError("UNKNOWN_ALGORITHM", f"unsupported algorithm {algorithm!r}")
    norm = payload["normalization"]
    span = max(norm["max"] - norm["min"], 1e-9)
    out = []
    for v in raw:
        value = float(v)
        if not math.isfinite(value):
            # NaN/inf must surface as a failure, never as the lowest band:
            # max(0.0, nan) == 0.0 in CPython would silently band it "normal".
            out.append(float("nan"))
            continue
        out.append(float(min(1.0, max(0.0, (value - norm["min"]) / span))))
    return out


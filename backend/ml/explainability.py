"""Bounded SHAP explanations on held-out rows, with immutable export assets."""
import hashlib
import json
import os
from pathlib import Path
from config import settings
from backend.ml.registry_service import RegistryError


def explain(payload, train_matrix, test_matrix, sample_ids, directory):
    from backend.ml.capabilities import require_capability
    require_capability("shap")
    import numpy as np
    import shap
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    names = payload["feature_names"]
    background = np.asarray(train_matrix)[:settings.ML_SHAP_BACKGROUND_ROWS]
    rows = np.asarray(test_matrix)[:settings.ML_SHAP_MAX_ROWS]
    if not len(rows):
        raise RegistryError("SHAP_TEST_DATA_REQUIRED", "SHAP requires a held-out test sample")
    if payload["algorithm"].startswith("xgboost"):
        import xgboost
        contributions = payload["model"].get_booster().predict(xgboost.DMatrix(rows), pred_contribs=True)
        values = shap.Explanation(values=contributions[:, :-1], base_values=contributions[:, -1], data=rows, feature_names=names)
        background = []  # Native TreeSHAP uses the fitted trees' path statistics.
    elif payload["algorithm"] == "logreg":
        explainer = shap.LinearExplainer(payload["model"], background, feature_names=names)
        values = explainer(rows)
    else:
        explainer = shap.TreeExplainer(payload["model"], background, feature_names=names, model_output="raw", feature_perturbation="interventional")
        values = explainer(rows)
    if values.values.ndim == 3:
        values = values[:, :, 1]
    if not np.isfinite(values.values).all():
        raise RegistryError("SHAP_NONFINITE", "SHAP produced non-finite contributions")
    root = Path(directory)
    root.mkdir(parents=True, exist_ok=True)
    data = {"method": "SHAP", "output_space": "raw estimator output; classifier margin/log-odds where applicable, never a threat probability",
        "feature_names": names, "sample_ids": sample_ids[:len(rows)], "rows": len(rows),
        "sampling": "First held-out test rows in immutable dataset order; bounded, not a population estimate",
        "background_rows": len(background), "values": values.values.tolist(),
        "base_values": np.asarray(values.base_values).tolist(), "data": rows.tolist(),
        "global_importance": dict(zip(names, np.abs(values.values).mean(axis=0).tolist()))}
    exports = {}
    def record(name):
        exports[name] = hashlib.sha256((root / name).read_bytes()).hexdigest()
    file = root / "shap.json"
    file.write_text(json.dumps(data, allow_nan=False), encoding="utf-8"); record(file.name)
    try:
        shap.plots.bar(values, show=False)
        plt.tight_layout(); plt.savefig(root / "shap-global.png", dpi=130, bbox_inches="tight"); plt.close(); record("shap-global.png")
        # Export one individual figure; every sampled row has exact interactive contributions.
        shap.plots.waterfall(values[0], show=False)
        plt.tight_layout(); plt.savefig(root / "shap-individual-0.png", dpi=130, bbox_inches="tight"); plt.close(); record("shap-individual-0.png")
    finally:
        plt.close("all")
    return {"status": "completed", "rows": len(rows), "output_space": data["output_space"],
            "global_importance": data["global_importance"], "exports": exports}

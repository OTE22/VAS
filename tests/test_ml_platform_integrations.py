"""Real optional-library integration tests using temporary files only."""
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import numpy as np
import pytest
from config import settings
from backend.ml.run_spec import PipelineConfiguration, RunOptions
from backend.ml.capabilities import capability_registry
from backend.ml.registry_service import RegistryError, save_artifact, score_with_payload
from backend.ml.tabular import fit_xgboost, prepare_rows
from backend.ml.reproducibility import capture

@pytest.fixture
def enabled(tmp_path):
    with patch.object(settings, "ML_ARTIFACT_DIR", str(tmp_path)), patch.object(settings, "MLFLOW_ENABLED", True), patch.object(settings, "MLFLOW_TRACKING_URI", ""), patch.object(settings, "XGBOOST_ENABLED", True), patch.object(settings, "OPTUNA_ENABLED", True), patch.object(settings, "SHAP_ENABLED", True):
        yield tmp_path

def test_capability_states_and_credential_refusal(enabled):
    from backend.core.runtime_settings import typed_parse, SettingValidationError
    assert capability_registry()["mlflow"]["status"] == "Available"
    with patch.object(settings, "OPTUNA_ENABLED", False):
        assert capability_registry()["optuna"]["status"] == "Disabled"
    with patch('backend.ml.capabilities.dependency', return_value=(False, None)):
        assert capability_registry()["xgboost"]["status"] == "Unavailable"
    with patch.object(settings, "MLFLOW_TRACKING_URI", "https://user:password@example.com"):
        assert capability_registry()["mlflow"]["status"] == "Misconfigured"
    with pytest.raises(SettingValidationError):
        typed_parse("MLFLOW_TRACKING_URI", "https://user:password@example.com")

def test_validation_targets_and_limits(enabled):
    from backend.ml.data_validator import validate_rows
    from datetime import datetime
    rows = [{"entity_id": str(i), "as_of": datetime(2025,1,1), "features": {"x_ratio": "invalid", "y": None}} for i in range(50)]
    report = validate_rows(rows, kind="supervised", definitions=[{"name": "x_ratio"}, {"name": "y"}])
    assert not report["passed"] and not report["checks"]["ratio_ranges"]["passed"] and not report["checks"]["max_null_rate"]["passed"]
    with pytest.raises(ValueError):
        PipelineConfiguration(model_type="tabular_regression_model", algorithm="xgboost_regressor", target="y", features=["y"])
    with pytest.raises(RegistryError, match="finite numeric target"):
        prepare_rows(rows, {"target": "y"}, regression=True)
    with pytest.raises(RegistryError):
        RunOptions.model_validate({"optuna": {"enabled": True, "trials": 200}}).check_capabilities("xgboost_regressor")

@pytest.mark.parametrize("regression", [False, True])
def test_real_tuning_shap_portable_registry_and_promotion(enabled, regression):
    from backend.ml.explainability import explain
    from backend.ml.mlflow_tracking import client, ensure_run, export_model
    import mlflow.pyfunc
    import pandas as pd
    rng = np.random.default_rng(42)
    x = rng.normal(size=(90, 3)); y = x[:,0] * 3 - x[:,1] if regression else (x[:,0] > 0).astype(int)
    algorithm = "xgboost_regressor" if regression else "xgboost_classifier"
    options = RunOptions.model_validate({"shap": True, "optuna": {"enabled": True, "trials": 3, "timeout_seconds": 30, "search_space": {"max_depth": {"type": "int", "low": 2, "high": 3}}}})
    estimator, params, tuning = fit_xgboost(algorithm, x[:60], y[:60], 42, {"n_estimators": 10}, validation=(x[60:75], y[60:75]), options=options)
    assert len(tuning["trials"]) == 3 and tuning["best_params"]
    assert len(estimator.evals_result()["validation_0"]["rmse" if regression else "logloss"]) == 10
    payload = {"algorithm": algorithm, "model": estimator, "feature_names": ["a", "b", "c"], "feature_set_version": "fixture", "imputation_medians": dict(zip(["a","b","c"], np.median(x[:60], axis=0))), "normalization": {"min":0.,"max":1.}, "band_cutpoints": {}, "metadata": {}, "dependency_versions": {}, "saved_at": "2026-01-01"}
    path = enabled / "model.pkl"; digest = save_artifact(payload, str(path))
    shap = explain(payload, x[:60], x[75:], list(range(15)), enabled / "explanations")
    data = json.loads((enabled / "explanations" / "shap.json").read_text())
    predicted = estimator.predict(x[75:]) if regression else estimator.get_booster().predict(__import__('xgboost').DMatrix(x[75:]), output_margin=True)
    np.testing.assert_allclose(np.sum(data["values"], axis=1) + data["base_values"], predicted, rtol=1e-5, atol=1e-5)
    assert set(shap["exports"]) == {"shap.json", "shap-global.png", "shap-individual-0.png"}
    manifest = capture(42, params, dataset={"id": "fixture", "version": 1})
    assert manifest["dependencies"].get("xgboost-cpu") or manifest["dependencies"].get("xgboost")
    c = client(); run_id = ensure_run(c, "fixture-" + algorithm, manifest)
    assert ensure_run(c, "fixture-" + algorithm, manifest) == run_id
    model = SimpleNamespace(id="fixture-model", algorithm=algorithm, seed=42, dataset_id="fixture", hyperparameters=params, evaluation_report={"test": {"metric": 0.8}}, model_type="fixture-" + algorithm, artifact_path=str(path), artifact_hash=digest, version=1, stage="validated")
    name, version = export_model(c, run_id, model, payload, manifest)
    model.stage = "approved"; assert export_model(c, run_id, model, payload, manifest) == (name, version)
    assert len(c.search_model_versions("name = '" + name + "'")) == 1
    assert str(c.get_model_version_by_alias(name, "approved").version) == version
    downloaded = c.download_artifacts(run_id, "model", str(enabled))
    portable = mlflow.pyfunc.load_model(downloaded)
    np.testing.assert_allclose(portable.predict(pd.DataFrame(x[75:], columns=["a","b","c"])), score_with_payload(payload, x[75:]), rtol=1e-7)
    model.stage = "archived"; export_model(c, run_id, model, payload, manifest)
    assert "approved" not in c.get_registered_model(name).aliases


def test_xgboost_cpu_build_and_runtime_fallback(enabled):
    from backend.ml.xgboost_runtime import select_device, fit_with_fallback
    from xgboost.core import XGBoostError
    from xgboost import XGBRegressor
    with patch('xgboost.build_info', return_value={"USE_CUDA": False}):
        assert select_device()["device"] == "cpu"
    with patch('xgboost.build_info', return_value={"USE_CUDA": True}), patch('xgboost.XGBRegressor.fit', side_effect=XGBoostError('CUDA device unavailable')):
        report = select_device(); assert report['device'] == 'cpu' and report['fallback_reason']
    with patch('xgboost.build_info', return_value={"USE_CUDA": True}), patch('xgboost.XGBRegressor.fit'), patch('backend.ml.xgboost_runtime.actual_device', return_value='cuda:0'):
        assert select_device()['device'] == 'cuda:0'
    calls = []
    def factory(**kwargs):
        if kwargs['device'].startswith('cuda'):
            calls.append('cuda')
            return SimpleNamespace(fit=lambda *a, **kw: (_ for _ in ()).throw(XGBoostError('CUDA out of memory')))
        calls.append('cpu'); return XGBRegressor(**kwargs)
    runtime = {'requested':'auto', 'device':'cuda:0', 'fallback_reason':None}
    x = np.arange(30).reshape(-1, 1); y = np.arange(30)
    model = fit_with_fallback(factory, {'n_estimators':10, 'random_state':42}, x, y, runtime)
    assert calls == ['cuda', 'cpu'] and runtime['device'] == 'cpu'
    assert model._platform_execution['fallback_reason'] and np.isfinite(model.predict(x)).all()
    def invalid(**kwargs): return SimpleNamespace(fit=lambda *a, **kw: (_ for _ in ()).throw(XGBoostError('Invalid target labels')))
    with pytest.raises(XGBoostError, match='Invalid target'):
        fit_with_fallback(invalid, {}, x, y, {'device':'cuda:0'})


@pytest.mark.parametrize("algorithm", ["logreg", "random_forest", "gradient_boosting"])
def test_existing_supervised_algorithms_explain_without_changing_predictions(enabled, algorithm):
    from backend.ml.trainer import _fit_supervised
    from backend.ml.explainability import explain
    rng = np.random.default_rng(7); x = rng.normal(size=(70, 3)); y = (x[:,0] + x[:,1] > 0).astype(int)
    model, _ = _fit_supervised(algorithm, x[:50], y[:50], 42, {})
    before = model.predict_proba(x[50:])
    payload = {"algorithm": algorithm, "model": model, "feature_names": ['a','b','c']}
    report = explain(payload, x[:50], x[50:], list(range(20)), enabled / algorithm)
    assert report['status'] == 'completed' and report['rows'] == 20
    np.testing.assert_array_equal(before, model.predict_proba(x[50:]))


def test_optuna_timeout_has_actionable_no_completed_trial(enabled):
    from backend.ml import tabular
    options = RunOptions.model_validate({"optuna":{"enabled":True, "trials":1, "timeout_seconds":10, "search_space":{"max_depth":{"type":"int","low":2,"high":2}}}})
    ticks = iter([0, 20])
    with patch.object(tabular, 'time', SimpleNamespace(monotonic=lambda: next(ticks))):
        with pytest.raises(RegistryError) as error:
            fit_xgboost('xgboost_regressor', np.arange(40).reshape(-1,1), np.arange(40), 42, {'n_estimators':10}, validation=(np.arange(10).reshape(-1,1), np.arange(10)), options=options)
    assert error.value.code == 'OPTUNA_NO_COMPLETED_TRIAL'

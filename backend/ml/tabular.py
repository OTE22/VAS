"""Bounded tabular fits and tuning; validation is never fitted or used as training."""
import math
import time
from config import settings
from backend.ml.registry_service import RegistryError

XGB_DEFAULTS = {"n_estimators": 200, "max_depth": 4, "learning_rate": 0.05,
                "subsample": 1.0, "colsample_bytree": 1.0, "reg_lambda": 1.0}
RANGES = {"n_estimators": (10, 2000), "max_depth": (1, 12), "learning_rate": (0.001, 1.0),
          "subsample": (0.1, 1.0), "colsample_bytree": (0.1, 1.0), "reg_lambda": (0.0, 100.0)}


def xgb_parameters(overrides):
    params = {**XGB_DEFAULTS, **(overrides or {})}
    if set(params) - set(RANGES):
        raise RegistryError("INVALID_XGBOOST_PARAMETERS", "Only the documented bounded XGBoost parameters are allowed")
    for name, value in params.items():
        low, high = RANGES[name]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not low <= value <= high:
            raise RegistryError("INVALID_XGBOOST_PARAMETERS", f"{name} must be between {low} and {high}")
        if name in ("n_estimators", "max_depth") and not isinstance(value, int):
            raise RegistryError("INVALID_XGBOOST_PARAMETERS", f"{name} must be an integer")
    return params


def fit_xgboost(algorithm, x, y, seed, parameters, *, validation=None, options=None):
    from backend.ml.capabilities import require_capability
    require_capability("xgboost")
    from xgboost import XGBClassifier, XGBRegressor
    regression = algorithm == "xgboost_regressor"
    cls = XGBRegressor if regression else XGBClassifier
    params = xgb_parameters(parameters)
    from backend.ml.xgboost_runtime import select_device, fit_with_fallback
    execution = select_device()
    tuning = options.optuna if options else None
    study_report = None
    if tuning and tuning.enabled:
        require_capability("optuna")
        import optuna
        from xgboost.callback import TrainingCallback
        if validation is None or not len(validation[1]):
            raise RegistryError("OPTUNA_VALIDATION_REQUIRED", "Tuning requires a nonempty validation split; test rows are never used")
        space = tuning.search_space
        if not space:
            raise RegistryError("OPTUNA_SEARCH_SPACE_REQUIRED", "Specify at least one bounded search dimension")
        for key, dimension in space.items():
            if key not in RANGES:
                raise RegistryError("OPTUNA_SEARCH_SPACE_INVALID", f"Unsupported search parameter: {key}")
            candidates = dimension.choices if dimension.type == "categorical" else [dimension.low, dimension.high]
            for v in candidates:
                if dimension.type == "int":
                    v = int(v)
                xgb_parameters({key: v})
        metric = "rmse" if regression else "logloss"
        deadline = time.monotonic() + tuning.timeout_seconds

        class PruningCallback(TrainingCallback):
            def __init__(self, trial): self.trial = trial
            def after_iteration(self, model, epoch, evals_log):
                score = float(evals_log["validation_0"][metric][-1])
                self.trial.report(score, epoch)
                if time.monotonic() >= deadline:
                    raise optuna.TrialPruned("Per-run timeout reached")
                if tuning.pruning and self.trial.should_prune():
                    raise optuna.TrialPruned()
                return False

        def objective(trial):
            chosen = {}
            for key, dim in space.items():
                chosen[key] = trial.suggest_categorical(key, dim.choices) if dim.type == "categorical" else trial.suggest_int(key, int(dim.low), int(dim.high), log=dim.log) if dim.type == "int" else trial.suggest_float(key, dim.low, dim.high, log=dim.log)
            estimator = fit_with_fallback(cls, {**xgb_parameters({**params, **chosen}), "random_state": seed,
                "eval_metric": metric, "callbacks": [PruningCallback(trial)]}, x, y, execution, eval_set=[validation], verbose=False)
            trial.set_user_attr("execution", dict(execution))
            return float(estimator.evals_result()["validation_0"][metric][-1])

        study = optuna.create_study(direction="minimize", sampler=optuna.samplers.TPESampler(seed=seed),
                                   pruner=optuna.pruners.MedianPruner(n_startup_trials=2) if tuning.pruning else optuna.pruners.NopPruner())
        study.optimize(objective, n_trials=tuning.trials, timeout=tuning.timeout_seconds, n_jobs=1)
        complete = [t for t in study.trials if t.state == optuna.trial.TrialState.COMPLETE]
        if not complete:
            raise RegistryError("OPTUNA_NO_COMPLETED_TRIAL", "No trial completed; increase the timeout or reduce tree/trial limits")
        params.update(study.best_params)
        study_report = {"objective": "validation_" + metric, "direction": "minimize", "best_value": study.best_value,
            "best_params": study.best_params, "trial_limit": tuning.trials, "timeout_seconds": tuning.timeout_seconds,
            "search_space": {k: v.model_dump() for k, v in space.items()}, "pruning": tuning.pruning,
            "trials": [{"number": t.number, "state": t.state.name, "value": t.value, "params": t.params, "execution": t.user_attrs.get("execution")} for t in study.trials]}
    estimator = fit_with_fallback(cls, {**params, "random_state": seed}, x, y, execution,
        eval_set=[(x, y), validation] if validation is not None else [(x, y)], verbose=False)
    if study_report is not None:
        study_report["execution"] = dict(execution)
    return estimator, params, study_report


def prepare_rows(rows, pipeline, regression=False):
    """Only an explicit numeric target enables regression; never infer it from labels."""
    selected = pipeline.get("features") or []
    target = pipeline.get("target")
    known = set().union(*(row["features"] for row in rows)) if rows else set()
    if set(selected) - known:
        raise RegistryError("PIPELINE_FEATURE_UNKNOWN", "Selected features are absent from this dataset version")
    if regression and (not target or target not in known):
        raise RegistryError("REGRESSION_TARGET_REQUIRED", "Choose a numeric target that exists in this dataset")
    predictors = selected or sorted(known - ({target} if regression else set()))
    if target in predictors:
        raise RegistryError("TARGET_LEAKAGE", "The regression target cannot also be a predictor")
    out = []
    for row in rows:
        value = row["features"].get(target) if regression else row.get("label")
        if regression and (isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value)):
            raise RegistryError("REGRESSION_TARGET_INVALID", "Every row must have a finite numeric target; build a corrected dataset version")
        if not regression and value not in ("positive", "negative"):
            raise RegistryError("CLASSIFICATION_LABEL_INVALID", "Classification requires positive/negative reviewed labels")
        features = {key: row["features"][key] for key in predictors if key in row["features"]}
        out.append({**row, "features": features, "target": float(value) if regression else int(value == "positive")})
    return out

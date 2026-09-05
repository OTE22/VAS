"""One validated contract shared by API, worker and pipeline versions."""
from typing import Any, Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator
from backend.ml.registry_service import RegistryError


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SearchDimension(StrictModel):
    type: Literal["int", "float", "categorical"]
    low: float | None = None
    high: float | None = None
    log: bool = False
    choices: list[int | float | str] | None = Field(None, min_length=1, max_length=20)

    @model_validator(mode="after")
    def bounds(self):
        import math
        if self.type == "categorical":
            if not self.choices or self.log or any(isinstance(v, float) and not math.isfinite(v) for v in self.choices):
                raise ValueError("Categorical search requires choices and does not support log sampling")
        elif self.low is None or self.high is None or not all(math.isfinite(v) for v in (self.low, self.high)) or self.low > self.high:
            raise ValueError("Search bounds must be finite and ordered")
        elif self.log and self.low <= 0:
            raise ValueError("Log search requires a positive lower bound")
        elif self.type == "int" and (int(self.low) != self.low or int(self.high) != self.high):
            raise ValueError("Integer search requires integer bounds")
        return self


class OptunaOptions(StrictModel):
    enabled: bool = False
    trials: int = Field(10, ge=1, le=200)
    timeout_seconds: int = Field(300, ge=10, le=7200)
    pruning: bool = True
    search_space: dict[str, SearchDimension] = Field(default_factory=dict, max_length=10)


class PipelineConfiguration(StrictModel):
    model_type: str = "behavior_anomaly_model"
    algorithm: str = "isolation_forest"
    target: str | None = Field(None, max_length=128)
    features: list[str] = Field(default_factory=list, max_length=200)
    validation_strategy: Literal["dataset_split"] = "dataset_split"
    metrics: list[str] = Field(default_factory=list, max_length=10)

    @model_validator(mode="after")
    def validate_contract(self):
        from backend.ml.model_specs import get_model_spec
        spec = get_model_spec(self.model_type)
        if self.algorithm not in spec.algorithms:
            raise ValueError("Algorithm is not supported by this model contract")
        regression = self.model_type == "tabular_regression_model"
        if regression and (not self.target or self.target == "label"):
            raise ValueError("Regression requires an explicit numeric feature target")
        if not regression and self.target not in (None, "label"):
            raise ValueError("This task uses the existing reviewed label; numeric targets require the regression model")
        if self.target in self.features or len(set(self.features)) != len(self.features):
            raise ValueError("Predictors must be unique and cannot include the target")
        allowed = {"mae", "rmse", "r2"} if regression else {"roc_auc", "average_precision"} if spec.dataset_kind == "supervised" else {"score_p50", "score_p90", "score_p99"}
        if set(self.metrics) - allowed:
            raise ValueError("Evaluation metrics are incompatible with the task")
        if not self.metrics:
            self.metrics = sorted(allowed)
        return self


class RunOptions(StrictModel):
    optuna: OptunaOptions = Field(default_factory=OptunaOptions)
    shap: bool = False
    require_clean_git: bool = False

    def check_capabilities(self, algorithm):
        from backend.ml.capabilities import require_capability
        from config import settings
        if algorithm.startswith("xgboost"):
            require_capability("xgboost")
        if self.optuna.enabled:
            require_capability("optuna")
            if not algorithm.startswith("xgboost"):
                raise RegistryError("OPTUNA_ALGORITHM_UNSUPPORTED", "This release supports pruned tuning for XGBoost classifier/regressor only")
            if self.optuna.trials > settings.ML_OPTUNA_MAX_TRIALS or self.optuna.timeout_seconds > settings.ML_OPTUNA_TIMEOUT_SECONDS:
                raise RegistryError("OPTUNA_LIMIT_EXCEEDED", "Reduce trials/timeout to the limits in Admin Settings")
        if self.shap:
            require_capability("shap")
            if algorithm not in ("xgboost_classifier", "xgboost_regressor", "random_forest", "gradient_boosting", "logreg"):
                raise RegistryError("SHAP_ALGORITHM_UNSUPPORTED", "SHAP is available for supported supervised tree/linear models and regression; disable it for anomaly baselines")
        return self

"""
ML pipeline constants and honest capability reporting.

Semantics that must never blur (approval-binding):
- Anomaly output is a BEHAVIORAL signal: ``behavioral_anomaly_score`` +
  ``ml_anomaly_band`` in {normal, elevated, unusual, highly_unusual}. It is
  never a threat probability and never maps onto threat severity
  (low/moderate/high/critical belongs to the rules engine alone).
- Decision modes: RULES is the production-safe default. SHADOW requires an
  administrator-approved anomaly model. HYBRID and ML are gated this
  release — gate checks return the exact unmet reasons.
- Optional integrations report FOUR distinct statuses: configured (flag),
  implemented (code path exists), dependency_available (import works),
  operational (end-to-end usable). A flag being true means none of the rest.
"""

import importlib.util
import logging
from enum import Enum
from typing import Dict, List
from config import settings

logger = logging.getLogger(__name__)

FEATURE_SET_VERSION = "secintel-features-v1"
LABEL_DEFINITION_VERSION = "threat-label-v1"

# Model types: only behavior_anomaly_model is implemented this release; the
# rest are registered interfaces for future signals into the risk engine.
MODEL_TYPE_BEHAVIOR_ANOMALY = "behavior_anomaly_model"
MODEL_TYPES = (
    MODEL_TYPE_BEHAVIOR_ANOMALY,
    "coappearance_anomaly_model",
    "social_graph_anomaly_model",
    "threat_ranking_model",
)
ANOMALY_MODEL_TYPES = tuple(t for t in MODEL_TYPES if "anomaly" in t)

MODES = ("rules", "shadow", "hybrid", "ml")

STAGES = ("training", "validated", "shadow", "approved", "production",
          "rejected", "archived", "rolled_back", "failed")

# Allowed stage transitions (service-level guard; the DB CHECK constraint
# ck_ml_models_anomaly_shadow_cap is the second, independent layer).
# validated -> shadow REQUIRES an explicit admin approval payload.
STAGE_TRANSITIONS: Dict[str, List[str]] = {
    "training": ["validated", "failed", "archived", "rejected"],
    "validated": ["shadow", "rejected", "archived"],
    "shadow": ["archived", "rejected"],          # this release: shadow is terminal-forward
    "approved": ["production", "archived"],       # reserved (non-anomaly, future)
    "production": ["rolled_back", "archived"],    # reserved (non-anomaly, future)
    "rejected": ["archived"],
    "rolled_back": ["archived"],
    "failed": ["archived"],
    "archived": [],
}

ANOMALY_BANDS = ("normal", "elevated", "unusual", "highly_unusual")

DISAGREEMENT_VALUES = ("both_flagged", "rules_only", "anomaly_only", "neither")


class FallbackReason(str, Enum):
    """Why a requested ML evaluation fell back to rules (recorded, never silent)."""
    NO_APPROVED_MODEL = "NO_APPROVED_MODEL"
    MODE_GATED = "MODE_GATED"
    ML_PAUSED = "ML_PAUSED"
    MODEL_LOAD_FAILED = "MODEL_LOAD_FAILED"
    ARTIFACT_HASH_MISMATCH = "ARTIFACT_HASH_MISMATCH"
    ARTIFACT_PATH_INVALID = "ARTIFACT_PATH_INVALID"
    DEPENDENCY_MISMATCH = "DEPENDENCY_MISMATCH"
    FEATURE_COMPUTATION_FAILED = "FEATURE_COMPUTATION_FAILED"
    MISSING_REQUIRED_FEATURES = "MISSING_REQUIRED_FEATURES"
    FEATURE_SCHEMA_MISMATCH = "FEATURE_SCHEMA_MISMATCH"
    PREDICTION_FAILED = "PREDICTION_FAILED"
    PREDICTION_TIMEOUT = "PREDICTION_TIMEOUT"
    THRESHOLD_UNRESOLVED = "THRESHOLD_UNRESOLVED"
    MODEL_UNAVAILABLE = "MODEL_UNAVAILABLE"       # model row vanished between inference and persistence


# Redis coordination keys
REDIS_ACTIVE_VERSION_KEY = "fr:ml:active_version:{model_type}"

# Structured log prefixes (house style)
LOG_MLOPS = "[ML_OPS]"
LOG_MLOPS_AUDIT = "[MLOPS_AUDIT]"



_OPTIONAL_DEPS = {
    "mlflow": ("MLFLOW_ENABLED", "mlflow"),
    "optuna": ("OPTUNA_ENABLED", "optuna"),
    "xgboost": ("XGBOOST_ENABLED", "xgboost"),
    "shap": ("SHAP_ENABLED", "shap"),
}

# Which optional integrations have a real consuming code path this release.
_IMPLEMENTED = {"mlflow": False, "optuna": False, "xgboost": False, "shap": False}

_warned_unavailable = set()


def optional_capability_status(name: str) -> Dict[str, bool]:
    """Four distinct statuses — a flag existing does NOT mean available.

    configured: the env flag is on; implemented: this release ships a code
    path that would use it; dependency_available: the package imports;
    operational: all three (end-to-end usable).
    """
    flag_key, module_name = _OPTIONAL_DEPS[name]
    configured = bool(getattr(settings, flag_key))
    implemented = _IMPLEMENTED.get(name, False)
    dependency_available = importlib.util.find_spec(module_name) is not None
    operational = configured and implemented and dependency_available
    if configured and not dependency_available and name not in _warned_unavailable:
        _warned_unavailable.add(name)
        logger.warning("%s optional integration %s is configured but its package "
                       "is not installed — capability unavailable; the application "
                       "continues in rules mode unaffected", LOG_MLOPS, name)
    return {
        "configured": configured,
        "implemented": implemented,
        "dependency_available": dependency_available,
        "operational": operational,
    }


def all_optional_capabilities() -> Dict[str, Dict[str, bool]]:
    return {name: optional_capability_status(name) for name in _OPTIONAL_DEPS}

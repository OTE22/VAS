"""Authoritative contracts for every trainable security-intelligence model.

The model type is not a cosmetic registry label.  It selects the entity,
feature set, dataset population, algorithms and score semantics.  Keeping
that mapping here prevents a pair model from accidentally training on person
rows, or a ranking model from being presented as a calibrated probability.
"""

from dataclasses import asdict, dataclass
from typing import Dict, Tuple


@dataclass(frozen=True)
class ModelSpec:
    model_type: str
    entity_type: str
    feature_set_version: str
    dataset_definition: str
    dataset_kind: str
    algorithms: Tuple[str, ...]
    default_algorithm: str
    model_purpose: str
    score_type: str
    is_probability: bool = False
    calibration_status: str = "not_applicable"
    serving_mode: str = "shadow"
    note: str = ""

    def capability(self) -> dict:
        value = asdict(self)
        value.update({"status": "available", "trainable": True})
        value["algorithms"] = list(self.algorithms)
        return value


BEHAVIOR_FEATURE_SET = "secintel-features-v2"
COAPPEARANCE_FEATURE_SET = "coappearance-features-v1"
SOCIAL_GRAPH_FEATURE_SET = "social-graph-features-v1"
THREAT_RANKING_FEATURE_SET = BEHAVIOR_FEATURE_SET

MODEL_SPECS: Dict[str, ModelSpec] = {
    "behavior_anomaly_model": ModelSpec(
        model_type="behavior_anomaly_model", entity_type="person",
        feature_set_version=BEHAVIOR_FEATURE_SET,
        dataset_definition="behavior_anomaly_person", dataset_kind="unsupervised",
        algorithms=("isolation_forest", "mad_baseline"),
        default_algorithm="isolation_forest",
        model_purpose="behavioral_anomaly_detection", score_type="anomaly_score",
        note="Person-level behavioral anomaly candidate; deploys only to approved shadow.",
    ),
    "coappearance_anomaly_model": ModelSpec(
        model_type="coappearance_anomaly_model", entity_type="pair",
        feature_set_version=COAPPEARANCE_FEATURE_SET,
        dataset_definition="coappearance_pair", dataset_kind="unsupervised",
        algorithms=("isolation_forest", "mad_baseline"),
        default_algorithm="isolation_forest",
        model_purpose="coappearance_anomaly_detection", score_type="anomaly_score",
        serving_mode="on_demand_shadow",
        note="Pair-level coappearance anomaly candidate; readiness-gated and shadow-only.",
    ),
    "social_graph_anomaly_model": ModelSpec(
        model_type="social_graph_anomaly_model", entity_type="person",
        feature_set_version=SOCIAL_GRAPH_FEATURE_SET,
        dataset_definition="social_graph_person", dataset_kind="unsupervised",
        algorithms=("isolation_forest", "mad_baseline"),
        default_algorithm="isolation_forest",
        model_purpose="social_graph_anomaly_detection", score_type="anomaly_score",
        serving_mode="on_demand_shadow",
        note="Graph-position anomaly candidate; graph readiness gates must pass; shadow-only.",
    ),
    "threat_ranking_model": ModelSpec(
        model_type="threat_ranking_model", entity_type="person",
        feature_set_version=THREAT_RANKING_FEATURE_SET,
        dataset_definition="threat_ranking_person_labeled", dataset_kind="supervised",
        algorithms=("logreg", "random_forest", "gradient_boosting"),
        default_algorithm="logreg",
        model_purpose="analyst_review_ranking", score_type="risk_rank_score",
        calibration_status="not_calibrated", serving_mode="offline_ranking",
        note=("Supervised analyst-queue ranking from independently reviewed labels; "
              "the score is relative priority, not a threat probability or live decision."),
    ),
}


def get_model_spec(model_type: str) -> ModelSpec:
    try:
        return MODEL_SPECS[model_type]
    except KeyError as exc:
        raise ValueError(f"unknown model_type {model_type!r}") from exc

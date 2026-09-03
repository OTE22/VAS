"""Contracts for coappearance, graph anomaly and threat-review ranking."""

from datetime import datetime, timedelta
from types import SimpleNamespace


def test_model_specs_keep_entities_algorithms_and_semantics_separate():
    from backend.ml.model_specs import MODEL_SPECS

    pair = MODEL_SPECS["coappearance_anomaly_model"]
    graph = MODEL_SPECS["social_graph_anomaly_model"]
    rank = MODEL_SPECS["threat_ranking_model"]
    assert pair.entity_type == "pair" and pair.dataset_kind == "unsupervised"
    assert pair.serving_mode == "on_demand_shadow"
    assert graph.feature_set_version == "social-graph-features-v1"
    assert set(rank.algorithms) == {"logreg", "random_forest", "gradient_boosting"}
    assert rank.dataset_kind == "supervised"
    assert rank.score_type == "risk_rank_score" and rank.is_probability is False
    assert rank.serving_mode == "offline_ranking"


def test_each_new_model_has_a_typed_dataset_definition():
    from backend.ml.dataset_definitions import get_definition

    pair = get_definition("coappearance_pair")
    graph = get_definition("social_graph_person")
    rank = get_definition("threat_ranking_person_labeled")
    assert (pair.entity_type, pair.kind, pair.feature_set_version) == (
        "pair", "unsupervised", "coappearance-features-v1")
    assert (graph.entity_type, graph.kind, graph.feature_set_version) == (
        "person", "unsupervised", "social-graph-features-v1")
    assert rank.kind == "supervised" and rank.label_definition_version


def test_canonical_pair_is_stable_and_rejects_self_pairs():
    import pytest
    from backend.ml.relational_feature_service import canonical_pair

    left, right, key = canonical_pair("b", "a")
    assert (left, right, key) == ("a", "b", "a|b")
    with pytest.raises(ValueError):
        canonical_pair("a", "a")


def test_ready_graph_features_are_bounded_and_deterministic(monkeypatch):
    from backend.ml import relational_feature_service as service

    monkeypatch.setattr(service.settings, "ML_GRAPH_MIN_NODES", 4)
    monkeypatch.setattr(service.settings, "ML_GRAPH_MIN_EDGES", 4)
    monkeypatch.setattr(service.settings, "ML_GRAPH_MIN_OBSERVATION_DAYS", 2)
    now = datetime.utcnow()
    pairs = [("a", "b"), ("b", "c"), ("c", "a"), ("c", "d")]
    edges = [SimpleNamespace(identity_id_1=a, identity_id_2=b,
                             co_appearance_count=i + 2,
                             first_co_appearance=now - timedelta(days=5),
                             last_co_appearance=now - timedelta(hours=i))
             for i, (a, b) in enumerate(pairs)]
    metrics, stats = service._graph_metrics(edges)
    assert stats["ready"] is True and stats["nodes"] == 4 and stats["edges"] == 4
    assert set(metrics["c"]) == {
        "graph_degree_centrality_90d", "graph_weighted_degree_log_90d",
        "graph_pagerank_90d", "graph_clustering_coefficient_90d",
        "graph_bridge_ratio_90d", "graph_mean_edge_weight_90d"}
    assert 0.0 <= metrics["c"]["graph_degree_centrality_90d"] <= 1.0
    assert 0.0 <= metrics["c"]["graph_clustering_coefficient_90d"] <= 1.0
    assert abs(sum(row["graph_pagerank_90d"] for row in metrics.values()) - 1.0) < 1e-6


def test_classifier_artifacts_score_as_relative_rank_not_probability_claim():
    import numpy as np
    from backend.ml.registry_service import score_with_payload
    from backend.ml.trainer import _fit_supervised

    matrix = np.array([[0.0, 0.0], [0.1, 0.2], [0.8, 0.9], [1.0, 1.0]])
    labels = np.array([0, 0, 1, 1])
    model, _ = _fit_supervised("logreg", matrix, labels, 42)
    scores = score_with_payload({"algorithm": "logreg", "model": model}, matrix)
    assert all(0.0 <= score <= 1.0 for score in scores)
    assert scores[0] < scores[-1]


def test_api_exposes_observational_scoring_without_decision_authority():
    source = open("/app/backend/routes/ml_ops.py", encoding="utf-8").read()
    assert '"/api/ml/score/relational"' in source
    assert '"/api/ml/rank/threat-review"' in source
    assert source.count('"applied_to_live_result": False') >= 2

"""
Production shadow readiness — the acceptance path, end to end.

    event -> Rules assessment (sealed) -> shadow inference -> prediction
    persisted -> comparison persisted exactly once -> analyst resolves
    BLIND -> manual/unreviewed label -> reviewer confirms -> evidence-grade
    -> shadow evidence report counts it -> live scientific readiness
    recomputes -> the operational result is STILL the Rules result.

Then the same invariants under failure (missing model, corrupt artifact,
NaN prediction, mapping unavailable, shadow exception), the single mode
gate behind BOTH mode-change routes, the reserved-model-type contract, the
canonical evidence-grade definition (seed/synthetic/weak/unreviewed are
reported, never counted), evidence-preserving retention, exact-scope
signal-mapping lookup, bounded metric labels and honest call logging.

Runs against the isolated regression stack only (it trains, approves and
stops shadow models; rules mode is restored).
"""

import json
import math
import os
import pickle
import urllib.error
import urllib.request
import uuid as uuid_mod
from datetime import datetime, timedelta

import pytest

from backend.ml.constants import MODEL_TYPE_BEHAVIOR_ANOMALY
from conftest import run_on_shared_loop as run_async
from test_ml_decision_modes import (
    _ensure_db, _get_identity, _set_mode, _stop_shadow_and_cleanup, _train_and_shadow)
from test_decision_router import (
    _current_scope, _decide, _fresh_subject_rows, _http, _install_mapping, _live,
    _remove_mapping, _sql, MAPPING_VERSION)

BASE = "http://localhost:8000"
LIVE_KEYS = ("overall_risk_score", "threat_level", "severity", "score_type", "is_probability",
             "risk_factors", "algorithm_version")
CONTRACT = {"engineering_readiness": "PASS", "scientific_validity": "INSUFFICIENT_EVIDENCE",
            "signal_mapping": "REQUIRES_VALIDATION", "ml_decision_authority": "DISABLED",
            "rules": "AUTHORITATIVE", "fallback": "RULES"}


@pytest.fixture(scope="module")
def token():
    req = urllib.request.Request(BASE + "/api/auth/login",
                                 data=json.dumps({"username": "admin", "password": "admin123"}).encode(),
                                 method="POST", headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())["access_token"]


@pytest.fixture(scope="module", autouse=True)
def shadow_model(token):
    _set_mode(token, "rules", "pytest readiness setup")
    _remove_mapping()
    _train_and_shadow()
    try:
        yield
    finally:
        try:
            _set_mode(token, "rules", "pytest readiness teardown")
        except Exception:
            pass
        _remove_mapping()
        _sql("DELETE FROM ml_labels WHERE source LIKE 'pytest-readiness%' OR source LIKE 'seed-pytest%' "
             "OR source = 'assessment_resolution' AND created_by = 'admin' AND notes LIKE 'pytest readiness%'")
        _stop_shadow_and_cleanup()


def _scored_identity():
    """A subject from the trained corpus (it has appearances and feature
    snapshots), so the shadow leg produces a BANDED prediction rather than an
    honest MISSING_REQUIRED_FEATURES failure."""
    row = _sql("SELECT i.id::text FROM identities i WHERE i.display_name LIKE :p AND EXISTS ("
               " SELECT 1 FROM ml_feature_snapshots s WHERE s.entity_id = i.id::text) "
               "ORDER BY i.display_name LIMIT 1", {"p": "pytest-mlm4-%corpus%"})
    assert row, "the corpus seeded by _train_and_shadow is the precondition"
    return row[0][0]


def _overview(token):
    status, body = _http("GET", "/api/ml/overview", token=token)
    assert status == 200, body
    return body


def _evidence(token, model_id=None, days=365):
    path = f"/api/ml/shadow/evidence?days={days}" + (f"&model_id={model_id}" if model_id else "")
    status, body = _http("GET", path, token=token)
    assert status == 200, body
    return body


# ---------------------------------------------------------------------------
# 1. The acceptance path
# ---------------------------------------------------------------------------

def test_e2e_event_to_evidence_keeps_the_rules_result(token):
    identity = _scored_identity()
    _fresh_subject_rows(identity)
    _set_mode(token, "shadow", "pytest readiness e2e")
    try:
        rules_outcome, _ = _decide(identity, "rules")
        rules_live = _live(rules_outcome)

        # event -> assessment (rules sealed, shadow observes)
        status, body = _http("POST", "/api/security/assessments",
                             {"subject_type": "identity", "subject_id": identity,
                              "event_id": "pytest-readiness-" + uuid_mod.uuid4().hex[:8]}, token=token)
        assert status in (200, 201), body
        assert not body.get("deduplicated"), body
        assessment_id = body["id"]
        prov = body["decision_provenance"]
        assert prov["requested_mode"] == "shadow" and prov["executed_mode"] == "shadow"
        assert prov["anomaly_signal_source"] == "rules" and prov["final_scoring_engine"] == "risk-engine-v1"
        assert not prov["fallback"]
        assert (body["total_risk_score"], body["severity"]) == (rules_live[0], rules_live[1]), (
            "the operational result must be the Rules result")

        # prediction persisted once, linked both ways; comparison exactly once
        pred = _sql("SELECT id::text, requested_mode, actual_mode_used, fallback_reason, model_id::text, "
                    "threshold_version, feature_set_version, behavioral_anomaly_score, ml_anomaly_band, "
                    "event_time, assessment_id::text FROM ml_predictions WHERE assessment_id = :a",
                    {"a": assessment_id})
        assert len(pred) == 1, pred
        prediction_id = pred[0][0]
        assert pred[0][1] == "shadow" and pred[0][10] == assessment_id
        assert pred[0][9] is not None, "event time must be persisted for the outcome join"
        assert pred[0][6], "the model's feature-set version is stamped on the prediction"
        linked = _sql("SELECT ml_prediction_id::text FROM threat_assessments WHERE id = :a", {"a": assessment_id}, scalar=True)
        assert linked == prediction_id
        comparisons = _sql("SELECT count(*) FROM ml_shadow_comparisons WHERE prediction_id = :p",
                           {"p": prediction_id}, scalar=True)
        assert comparisons == 1, f"exactly one comparison per prediction, got {comparisons}"
        cmp_row = _sql("SELECT rule_threat_score, rule_threat_severity, assessment_id::text "
                       "FROM ml_shadow_comparisons WHERE prediction_id = :p", {"p": prediction_id})[0]
        assert cmp_row[0] == body["total_risk_score"] and cmp_row[2] == assessment_id, (
            "the comparison describes the assessment it names")

        # a repeated request inside the dedup window: still ONE prediction,
        # ONE comparison, the assessment link unchanged
        status, again = _http("POST", "/api/security/assessments",
                              {"subject_type": "identity", "subject_id": identity}, token=token)
        assert status in (200, 201) and again.get("deduplicated") is True, again
        assert _sql("SELECT count(*) FROM ml_shadow_comparisons WHERE assessment_id = :a",
                    {"a": assessment_id}, scalar=True) == 1
        assert _sql("SELECT ml_prediction_id::text FROM threat_assessments WHERE id = :a",
                    {"a": assessment_id}, scalar=True) == prediction_id

        # analyst resolves BLIND with an outcome -> manual, UNREVIEWED label
        status, resolved = _http("POST", f"/api/security/assessments/{assessment_id}/resolve",
                                 {"resolution_status": "confirmed_threat", "outcome": "positive",
                                  "notes": "pytest readiness blind outcome",
                                  "ml_observation_revealed": False}, token=token)
        assert status == 200, resolved
        label = resolved["outcome_label"]
        assert label["label"] == "positive" and label["label_kind"] == "manual"
        assert label["review_status"] == "unreviewed" and label["source"] == "assessment_resolution"
        assert label["selection"]["entry_point"] == "security_intelligence"
        assert label["selection"]["ml_observation_revealed"] is False
        assert _sql("SELECT outcome_label_id::text FROM ml_predictions WHERE id = :p",
                    {"p": prediction_id}, scalar=True) == label["id"], "the outcome links to the prediction"

        # not evidence yet: unreviewed is a distinct population
        model_id = pred[0][4]
        report = _evidence(token, model_id)
        entry = report["models"][model_id]
        assert entry["with_reviewed_outcome"] == 0
        assert entry["populations"]["unreviewed"] >= 1
        assert report["data_quality"]["duplicate_comparisons"] == 0

        # authorized review -> evidence-grade (blind)
        status, reviewed = _http("POST", f"/api/ml/labels/{label['id']}/review",
                                 {"action": "confirm", "notes": "pytest readiness review"}, token=token)
        assert status == 200, reviewed
        report = _evidence(token, model_id)
        entry = report["models"][model_id]
        assert entry["with_reviewed_outcome"] == 1
        ev = entry["evidence"]
        # one admin account resolved AND reviewed: truthfully SELF-reviewed
        # (never silently mixed into blind); recorded blind nonetheless
        assert ev["self_reviewed"] == 1 and ev["blind_reviewed"] == 0 and ev["revealed_reviewed"] == 0
        assert ev["recorded_blind"] == 1 and ev["recorded_revealed"] == 0
        assert ev["coverage"]["reviewed_total"] == 1
        assert report["mapping_decision"] == "REQUIRES_VALIDATION"
        for key in ("score_delta", "score_diff"):
            assert key not in json.dumps(report)

        # live scientific readiness sees it; the contract is unchanged
        overview = _overview(token)
        system = overview["system"]
        readiness = system["scientific_readiness"]
        assert readiness["status"] == "INSUFFICIENT_EVIDENCE"
        assert readiness["metrics"]["reviewed_outcome_coverage"]["reviewed_total"] == 1
        assert readiness["metrics"]["reviewed_outcome_coverage"]["populations"]["self_reviewed"] == 1
        assert any(c["criterion"] == "reviewed_outcomes_total" and c["status"] == "NOT_CONFIGURED"
                   for c in readiness["criteria"]), readiness["criteria"]
        contract = system["ml_contract"]
        for key, expected in CONTRACT.items():
            assert contract[key] == expected, (key, contract[key])
        assert contract["evidence_collection"] == "ACTIVE"

        # the operational result did not move
        after, _ = _decide(identity, "rules")
        assert _live(after) == rules_live
    finally:
        _set_mode(token, "rules", "pytest readiness restore")


# ---------------------------------------------------------------------------
# 2. Failure matrix — rules keep serving, failures are data
# ---------------------------------------------------------------------------

def _run_shadow(identity, monkeypatch=None, patches=()):
    """In-process shadow pass with optional patches; returns the newest
    prediction row for the identity written by THIS pass."""
    async def _go():
        from db_connection import db_manager
        from backend.ml.shadow_service import shadow_service
        from backend.ml.decision_service import decision_service
        from config import settings
        await _ensure_db()
        previous = settings.ML_DECISION_MODE
        settings.ML_DECISION_MODE = "shadow"
        try:
            async with db_manager.get_session() as db:
                outcome = await decision_service.decide(db, identity)
            await shadow_service.run_shadow(identity_id=identity,
                                            rule_score=outcome.assessment.overall_risk_score,
                                            rule_severity=outcome.assessment.severity,
                                            assessment_id=None, event_time=datetime.utcnow())
            return outcome
        finally:
            settings.ML_DECISION_MODE = previous
    return run_async(_go())


def _latest_failure(identity, reason):
    return _sql("SELECT fallback_reason, actual_mode_used, ml_anomaly_band FROM ml_predictions "
                "WHERE subject_id = :s AND fallback_reason = :r AND created_at > now() - interval '3 minutes' "
                "ORDER BY created_at DESC LIMIT 1", {"s": identity, "r": reason})


def test_nan_score_is_an_invalid_prediction_not_a_normal_band(monkeypatch):
    identity = _scored_identity()
    rules_outcome, _ = _decide(identity, "rules")
    from backend.ml import registry_service, inference_service as inf_mod
    # warm the model cache with the REAL scorer (the load-time smoke test
    # must pass); the per-request scorer is then patched to emit NaN
    _run_shadow(identity)
    monkeypatch.setattr(registry_service, "score_with_payload", lambda payload, vectors: [float("nan")])
    outcome = _run_shadow(identity)
    assert _live(outcome) == _live(rules_outcome)
    row = _latest_failure(identity, "INVALID_PREDICTION")
    assert row, "a NaN score must be persisted as INVALID_PREDICTION, never banded"
    assert row[0][2] is None, "no band for an invalid score"
    monkeypatch.undo()


def test_shadow_exception_and_missing_artifact_keep_rules(monkeypatch):
    identity = _get_identity(None)
    rules_outcome, _ = _decide(identity, "rules")
    from backend.ml import inference_service as inf_mod

    async def boom(db, identity_id, **kw):
        raise RuntimeError("pytest shadow explosion")
    monkeypatch.setattr(inf_mod.inference_service, "predict_identity", boom)
    outcome = _run_shadow(identity)
    assert _live(outcome) == _live(rules_outcome)
    assert _latest_failure(identity, "SHADOW_INTERNAL_ERROR"), "a raised shadow pass is recorded as data"
    monkeypatch.undo()

    # corrupt artifact on disk: hash mismatch -> ARTIFACT_HASH_MISMATCH
    path = _sql("SELECT artifact_path FROM ml_models WHERE model_type = :t AND stage = 'shadow'",
                {"t": MODEL_TYPE_BEHAVIOR_ANOMALY}, scalar=True)
    assert path and os.path.exists(path)
    original = open(path, "rb").read()
    try:
        with open(path, "wb") as fh:
            fh.write(original + b"tampered")
        inf_mod.inference_service.invalidate()
        outcome = _run_shadow(identity)
        assert _live(outcome) == _live(rules_outcome)
        assert _latest_failure(identity, "ARTIFACT_HASH_MISMATCH")
        # missing artifact file -> MODEL_LOAD_FAILED (ARTIFACT_MISSING)
        os.rename(path, path + ".moved")
        inf_mod.inference_service.invalidate()
        outcome = _run_shadow(identity)
        assert _live(outcome) == _live(rules_outcome)
        assert _latest_failure(identity, "MODEL_LOAD_FAILED")
    finally:
        if os.path.exists(path + ".moved"):
            os.rename(path + ".moved", path)
        with open(path, "wb") as fh:
            fh.write(original)
        inf_mod.inference_service.invalidate()


def test_ml_mode_bookkeeping_failure_falls_back_to_rules(monkeypatch):
    """The critical-path rule: once rules can be computed, a failing mapping
    lookup (DB hiccup in ML bookkeeping) must not become an error."""
    identity = _get_identity(None)
    rules_outcome, _ = _decide(identity, "rules")
    from backend.ml.decision_service import decision_service

    async def broken(db):
        raise RuntimeError("pytest mapping lookup outage")
    monkeypatch.setattr(decision_service, "_mapping_policy", broken)
    outcome, mode_after = _decide(identity, "ml")
    assert _live(outcome) == _live(rules_outcome)
    assert outcome.provenance.fallback is True
    assert outcome.provenance.fallback_reason == "SIGNAL_MAPPING_UNVALIDATED"
    assert mode_after == "ml"
    monkeypatch.undo()


# ---------------------------------------------------------------------------
# 3. One mode gate behind both routes
# ---------------------------------------------------------------------------

def test_both_mode_change_routes_share_the_gate_and_its_body(token):
    for target in ("ml", "hybrid"):
        s1, b1 = _http("PUT", "/api/ml/config/mode", {"mode": target, "reason": "pytest gate parity"}, token=token)
        s2, b2 = _http("PUT", "/api/settings/ML_DECISION_MODE",
                       {"value": target, "change_reason": "pytest gate parity"}, token=token)
        assert s1 == 409 and s2 == 409, (s1, b1, s2, b2)
        d1, d2 = b1["detail"], b2["detail"]
        assert d1["error_code"] == d2["error_code"] == "MODE_GATED"
        assert [g["gate"] for g in d1["gates"]] == [g["gate"] for g in d2["gates"]]
        assert {g["gate"]: g["ok"] for g in d1["gates"]} == {g["gate"]: g["ok"] for g in d2["gates"]}
        states = {g["gate"]: g for g in d1["gates"]}
        assert states["shadow_model"]["ok"] is True
        assert states["scientific_validity"]["ok"] is False
        assert states["scientific_validity"]["status"] == "INSUFFICIENT_EVIDENCE"
        assert states["signal_mapping"]["ok"] is False
        assert states["signal_mapping"]["status"] == "REQUIRES_VALIDATION"
        assert any(r["code"] == "SIGNAL_MAPPING_UNVALIDATED" for r in d1["reasons"])
        assert any(r["code"] == "SCIENTIFIC_EVIDENCE_INSUFFICIENT" for r in d1["reasons"])
    mode = _sql("SELECT value FROM settings WHERE key = 'ML_DECISION_MODE'", scalar=True)
    assert mode in ("rules", "shadow"), "a refused change never moves the mode"
    rejected = _sql("SELECT count(*) FROM ml_audit_log WHERE action = 'mode_change_rejected' "
                    "AND created_at > now() - interval '2 minutes'", scalar=True)
    assert rejected >= 4, "every refused mode change is audited"
    rid = _sql("SELECT request_id FROM ml_audit_log WHERE action = 'mode_change_rejected' "
               "ORDER BY created_at DESC LIMIT 1", scalar=True)
    assert rid, "audit rows carry the request id for log correlation"


# ---------------------------------------------------------------------------
# 4. Model-family contracts
# ---------------------------------------------------------------------------

def test_all_model_families_are_declared_with_typed_training_contracts(token):
    overview = _overview(token)
    types = {t["model_type"]: t for t in overview["model_types"]}
    for model_type in ("behavior_anomaly_model", "coappearance_anomaly_model",
                       "social_graph_anomaly_model", "threat_ranking_model"):
        assert types[model_type]["trainable"] is True
        assert types[model_type]["status"] == "available"
        assert types[model_type]["algorithms"]
        assert types[model_type]["feature_set_version"]
    assert types["coappearance_anomaly_model"]["entity_type"] == "pair"
    assert types["threat_ranking_model"]["dataset_kind"] == "supervised"
    assert types["threat_ranking_model"]["is_probability"] is False
    html = open("/app/frontend/admin/ml-ops.html", encoding="utf-8").read()
    assert 'value="threat_ranking_model"' in html
    assert 'value="threat_ranking_model" disabled' not in html
    js = open("/app/frontend/js/admin-ml-ops.js", encoding="utf-8").read()
    assert "renderModelTypeContract" in js and "trainable !== true" in js


def test_trainer_refuses_an_algorithm_from_the_wrong_model_family():
    async def _go():
        from backend.core.task_history import task_history_manager
        from backend.ml import trainer
        await _ensure_db()
        job_id = "pytest-reserved-" + uuid_mod.uuid4().hex[:8]
        await task_history_manager.create_job(job_id=job_id, task_type="ml_training",
                                              task_name="pytest reserved", description="pytest")
        await trainer.run_training_job(job_id, model_type="threat_ranking_model",
                                       algorithm="isolation_forest")
        task = await task_history_manager.get_task_by_job_id(job_id)
        return task
    task = run_async(_go())
    assert task["status"] == "failed"
    assert task["error_code"] == "ALGORITHM_NOT_SUPPORTED_FOR_MODEL"
    assert _sql("SELECT count(*) FROM ml_models WHERE model_type = 'threat_ranking_model'", scalar=True) == 0


# ---------------------------------------------------------------------------
# 5. Canonical evidence-grade definition
# ---------------------------------------------------------------------------

def test_evidence_grade_excludes_seed_weak_unreviewed_and_conflicts(token):
    identity = _scored_identity()
    day = datetime.utcnow().replace(microsecond=0)
    pred = _sql("SELECT id::text, model_id::text FROM ml_predictions WHERE subject_id = :s "
                "AND ml_anomaly_band IS NOT NULL ORDER BY created_at DESC LIMIT 1", {"s": identity})
    assert pred, "a banded prediction for the subject is the precondition"
    prediction_id, model_id = pred[0]

    # weak label: links, but can never be confirmed into evidence
    status, weak = _http("POST", "/api/ml/labels",
                         {"subject_id": identity, "label": "positive", "label_kind": "weak",
                          "source": "pytest-readiness-weak", "event_time": day.isoformat() + "Z"}, token=token)
    assert status in (200, 201), weak
    status, body = _http("POST", f"/api/ml/labels/{weak['id']}/review", {"action": "confirm"}, token=token)
    assert status == 422 and body["detail"]["error_code"] == "WEAK_LABEL_NOT_REVIEWABLE", body

    # a conflicting value is refused, never silently "identical"
    status, first = _http("POST", "/api/ml/labels",
                          {"subject_id": identity, "label": "negative", "label_kind": "manual",
                           "source": "pytest-readiness-conflict", "event_time": day.isoformat() + "Z"}, token=token)
    assert status == 201, first
    status, conflict = _http("POST", "/api/ml/labels",
                             {"subject_id": identity, "label": "positive", "label_kind": "manual",
                              "source": "pytest-readiness-conflict", "event_time": day.isoformat() + "Z"}, token=token)
    assert status == 409 and conflict["detail"]["error_code"] == "LABEL_CONFLICT", conflict
    assert conflict["detail"]["existing_label_id"] == first["id"]

    # a seed-sourced, manual, REVIEWED label is still not evidence
    seed_id = str(uuid_mod.uuid4())
    _sql("INSERT INTO ml_labels (id, subject_type, subject_id, label, label_kind, label_definition_version, "
         " confidence, source, event_time, status, review_status, reviewed_by, reviewed_at, idempotency_key, "
         " created_at, created_by) VALUES (CAST(:id AS uuid), 'identity', :s, 'positive', 'manual', "
         " 'threat-label-v1', 1.0, 'seed-pytest-manual', :t, 'active', 'reviewed', 'seeder', now(), "
         " :k, now(), 'seeder')",
         {"id": seed_id, "s": identity, "t": day, "k": "pytest-seed-" + seed_id})
    _sql("UPDATE ml_predictions SET outcome_label_id = CAST(:l AS uuid), outcome_label = 'positive', "
         "outcome_recorded_at = now() WHERE id = CAST(:p AS uuid)", {"l": seed_id, "p": prediction_id})
    try:
        report = _evidence(token, model_id)
        entry = report["models"][model_id]
        assert entry["populations"]["synthetic_or_seed"] >= 1
        assert "seed-pytest-manual" in report["excluded_non_evidence_labels"]
        counted = _sql("SELECT count(*) FROM ml_labels l JOIN ml_predictions p ON p.outcome_label_id = l.id "
                       "WHERE p.id = CAST(:p AS uuid) AND l.source LIKE 'seed-%'", {"p": prediction_id}, scalar=True)
        assert counted == 1, "the seed label IS linked (the join is real) ..."
        from backend.ml import evidence_grade
        assert not evidence_grade.is_evidence_grade(type("L", (), {
            "label_kind": "manual", "review_status": "reviewed", "status": "active",
            "label": "positive", "source": "seed-pytest-manual"})()), "... but it is never evidence"
        overview = _overview(token)
        alerts = {a["code"] for a in overview["system"]["alerts"]}
        assert "NON_EVIDENCE_LABELS_PRESENT" in alerts
        readiness = overview["system"]["scientific_readiness"]
        assert readiness["metrics"]["reviewed_outcome_coverage"]["populations"]["synthetic_or_seed"] >= 1
        # label_stats (supervised gate) uses the same definition
        status, stats = _http("GET", "/api/ml/overview", token=token)
        assert "seed-/synthetic-" in stats["label_readiness"]["evidence_grade_definition"]
    finally:
        _sql("UPDATE ml_predictions SET outcome_label_id = NULL, outcome_label = NULL, outcome_recorded_at = NULL "
             "WHERE outcome_label_id = CAST(:l AS uuid)", {"l": seed_id})
        _sql("DELETE FROM ml_labels WHERE id = CAST(:l AS uuid)", {"l": seed_id})
        _sql("DELETE FROM ml_labels WHERE source LIKE 'pytest-readiness%'")


# ---------------------------------------------------------------------------
# 6. Retention preserves the evidence chain
# ---------------------------------------------------------------------------

def test_retention_never_ages_out_linked_predictions():
    model_id = _sql("SELECT id::text FROM ml_models WHERE model_type = :t AND stage = 'shadow'",
                    {"t": MODEL_TYPE_BEHAVIOR_ANOMALY}, scalar=True)
    old = datetime.utcnow() - timedelta(days=400)
    linked_id, loose_id = str(uuid_mod.uuid4()), str(uuid_mod.uuid4())
    subject = "pytest-retention-" + uuid_mod.uuid4().hex[:8]
    assessment_id = str(uuid_mod.uuid4())
    _sql("INSERT INTO threat_assessments (id, subject_type, subject_id, total_risk_score, severity, confidence, "
         " signals, model_version, status, source_timestamp, idempotency_key, created_at, updated_at) "
         "VALUES (CAST(:a AS uuid), 'identity', :s, 10, 'low', 0.5, '[]'::jsonb, 'risk-engine-v1', 'open', "
         " :t, :k, :t, :t)", {"a": assessment_id, "s": subject, "t": old, "k": "pytest-ret-" + assessment_id})
    for pid, a_id in ((linked_id, assessment_id), (loose_id, None)):
        _sql("INSERT INTO ml_predictions (id, subject_type, subject_id, model_id, model_type, model_version_label, "
             " model_purpose, requested_mode, actual_mode_used, fallback_reason, feature_set_version, score_type, "
             " is_probability, calibration_status, assessment_id, as_of_timestamp, idempotency_key, created_at) "
             "VALUES (CAST(:id AS uuid), 'identity', :s, CAST(:m AS uuid), 'behavior_anomaly_model', 'pytest', "
             " 'behavioral_anomaly_detection', 'shadow', 'rules', 'PREDICTION_FAILED', 'pytest', 'anomaly_score', "
             " false, 'not_applicable', CAST(:a AS uuid), :t, :k, :t)",
             {"id": pid, "s": subject, "m": model_id, "a": a_id, "t": old, "k": "pytest-ret-" + pid})
    try:
        async def _dry():
            from backend.core.data_retention import DataRetentionManager
            await _ensure_db()
            return await DataRetentionManager().cleanup_old_data(dry_run=True)
        result = run_async(_dry())
        extra = result["extra"]
        assert extra["ml_predictions_retained_as_evidence"] >= 1
        would_delete = _sql("SELECT count(*) FROM ml_predictions WHERE id = CAST(:p AS uuid) AND created_at < now() - interval '180 days' "
                            "AND outcome_label_id IS NULL AND assessment_id IS NULL", {"p": loose_id}, scalar=True)
        assert would_delete == 1, "an UNLINKED old prediction is sweepable"
        kept = _sql("SELECT count(*) FROM ml_predictions WHERE id = CAST(:p AS uuid) AND assessment_id IS NOT NULL",
                    {"p": linked_id}, scalar=True)
        assert kept == 1
        # the guard in the sweep statement itself
        import inspect
        from backend.core import data_retention
        source = inspect.getsource(data_retention)
        assert "outcome_label_id IS NULL AND assessment_id IS NULL" in source
        assert "SELECT ml_prediction_id FROM threat_assessments" in source
    finally:
        _sql("DELETE FROM ml_predictions WHERE subject_id = :s", {"s": subject})
        _sql("DELETE FROM threat_assessments WHERE subject_id = :s", {"s": subject})


# ---------------------------------------------------------------------------
# 7. Signal mapping: exact scope, no wildcard, no masking
# ---------------------------------------------------------------------------

def _policy(anomaly_cap=30.0, **scope_override):
    scope = dict(_current_scope())        # outside the loop: _sql runs its own coroutine
    scope.update(scope_override)

    async def _go():
        from db_connection import db_manager
        from backend.ml.signal_mapping_service import signal_mapping_service
        await _ensure_db()
        async with db_manager.get_session() as db:
            return await signal_mapping_service.active_policy(db, anomaly_cap=anomaly_cap, **scope)
    return run_async(_go())


def _insert_mapping(version, *, status="active", calibration="validated", calibration_data=None,
                    weights=None, activated_offset_seconds=0):
    scope = _current_scope()
    w = weights if weights is not None else {"kind": "band_points",
                                              "band_points": {"normal": 0, "elevated": 5, "unusual": 15, "highly_unusual": 30},
                                              "scope": scope}
    _sql("INSERT INTO risk_model_versions (id, profile, version, weights, thresholds, status, score_type, "
         " calibration_status, calibration_data, notes, created_at, activated_at) VALUES (gen_random_uuid(), "
         " 'ml_anomaly_signal_map', :v, CAST(:w AS jsonb), '{}'::jsonb, :s, 'heuristic', :c, CAST(:d AS jsonb), "
         " 'pytest', now(), now() + (:off || ' seconds')::interval)",
         {"v": version, "w": json.dumps(w), "s": status, "c": calibration,
          "d": json.dumps(calibration_data) if calibration_data is not None else None,
          "off": str(activated_offset_seconds)})


def test_mapping_lookup_is_exact_scope_and_not_masked_by_newer_invalid_rows():
    versions = ["pytest-map-" + x for x in ("valid", "pending", "empty", "badkind", "noscope")]
    for v in versions:
        _sql("DELETE FROM risk_model_versions WHERE profile = 'ml_anomaly_signal_map' AND version = :v", {"v": v})
    try:
        assert _policy() is None, "no policy exists: REQUIRES_VALIDATION"
        _insert_mapping("pytest-map-pending", calibration="pending", calibration_data={"x": 1})
        assert _policy() is None, "active but not validated is not a policy"
        _insert_mapping("pytest-map-empty", calibration_data=None, activated_offset_seconds=1)
        assert _policy() is None, "validated without calibration data is not a policy"
        _insert_mapping("pytest-map-badkind", calibration_data={"x": 1}, activated_offset_seconds=2,
                        weights={"kind": "score_times", "scope": _current_scope()})
        assert _policy() is None
        _insert_mapping("pytest-map-noscope", calibration_data={"x": 1}, activated_offset_seconds=3,
                        weights={"kind": "band_points",
                                 "band_points": {"normal": 0, "elevated": 5, "unusual": 15, "highly_unusual": 30}})
        assert _policy() is None, "a policy without scope is never used"
        # the valid, scoped policy is OLDER than four newer invalid active rows
        _insert_mapping("pytest-map-valid", calibration_data={"source": "pytest"}, activated_offset_seconds=-60)
        policy = _policy()
        assert policy is not None and policy.version == "pytest-map-valid", (
            "newer invalid rows must not mask the valid scoped policy")
        # exact scope: any mismatch, and any missing dimension, refuses
        assert _policy(model_id=str(uuid_mod.uuid4())) is None
        assert _policy(feature_set_version="secintel-features-v1") is None
        assert _policy(threshold_version="global:@v999") is None
        assert _policy(threshold_version=None) is None, "no wildcard: unknown threshold scope -> no policy"
    finally:
        for v in versions:
            _sql("DELETE FROM risk_model_versions WHERE profile = 'ml_anomaly_signal_map' AND version = :v", {"v": v})


# ---------------------------------------------------------------------------
# 8. Observability and call logging
# ---------------------------------------------------------------------------

def test_metric_labels_are_bounded_and_core_face_counters_carry_no_names():
    from backend.ml import metrics as ml_metrics
    names = ml_metrics.label_names()
    assert names, "ML metrics must be registered"
    allowed = {"band", "reason", "outcome", "status", "target_mode", "label_kind",
               "model_version", "feature_set_version", "algorithm", "table"}
    for key, labels in names.items():
        assert set(labels) <= allowed, (key, labels)
    forbidden = ("identity_id", "assessment_id", "prediction_id", "request_id", "name", "subject_id")
    for key, labels in names.items():
        assert not (set(labels) & set(forbidden)), (key, labels)
    from backend.core import metrics as core_metrics
    core_metrics.initialize_metrics()
    for counter in (core_metrics.metrics_faces_detected, core_metrics.metrics_faces_skipped,
                    core_metrics.metrics_faces_batch_skipped):
        assert list(getattr(counter, "_labelnames", ()) or ()) == [], "face counters carry no person name"
    src = open("/app/backend/core/metrics.py", encoding="utf-8").read()
    assert "labelnames=['name']" not in src


def test_call_log_sanitizes_notes_and_records_validation_errors_as_422(token):
    from backend.ml import call_log
    out = call_log.sanitize_body({"label": "positive", "notes": "the subject was seen near the gate",
                                  "password": "x", "features": [1, 2, 3]})
    assert out["notes"].startswith("<text:") and "gate" not in out["notes"]
    assert out["password"] == "<redacted>" and out["features"] == "<list:3>"
    # a client validation error is a 422 in the call log, not a 500
    status, _ = _http("PUT", "/api/ml/config/mode", {"mode": "shadow", "reason": "x"}, token=token)
    assert status == 422
    status, calls = _http("GET", "/api/ml/calls?errors_only=true&limit=50", token=token)
    assert status == 200, calls
    items = calls.get("items") or calls.get("calls") or []
    recent = [c for c in items if c.get("route") == "/api/ml/config/mode" and c.get("status") == 422]
    assert recent and recent[0].get("error_code") == "VALIDATION_ERROR", items[:3]
    assert not any(c.get("status") == 500 and c.get("error_code") == "RequestValidationError" for c in items)


def test_overview_exposes_live_readiness_criteria_and_contract(token):
    overview = _overview(token)
    system = overview["system"]
    readiness = system["scientific_readiness"]
    assert readiness["computation"] == "live"
    criteria = {c["criterion"]: c for c in readiness["criteria"]}
    for name in ("history_span_days", "median_appearances_per_entity", "reviewed_outcomes_total",
                 "reviewed_outcomes_per_band"):
        assert criteria[name]["status"] == "NOT_CONFIGURED", criteria[name]
        assert criteria[name]["required"] is None
    assert criteria["signal_mapping"]["status"] == "FAIL"
    assert set(system["evidence_populations"]) >= {"blind_reviewed", "revealed_reviewed", "self_reviewed",
                                                   "weak", "synthetic_or_seed", "unreviewed"}
    contract = system["ml_contract"]
    for key, expected in CONTRACT.items():
        assert contract[key] == expected, (key, contract[key])


# ---------------------------------------------------------------------------
# 9. Risk closure: contamination isolation, metric freshness, lineage,
#    separation-of-duties provenance
# ---------------------------------------------------------------------------

def _latest_banded_prediction(identity):
    row = _sql("SELECT id::text, model_id::text FROM ml_predictions WHERE subject_id = :s "
               "AND ml_anomaly_band IS NOT NULL ORDER BY created_at DESC LIMIT 1", {"s": identity})
    assert row, "a banded prediction is the precondition"
    return row[0]


def test_seed_labels_and_duplicate_comparisons_cannot_reach_readiness_gate_or_review_counts(token):
    """The dev-style contamination (seed-source reviewed labels, a historical
    duplicate comparison) is excluded from scientific readiness, the mode
    gate, review counts and mapping lookup - and reported, not deleted."""
    identity = _scored_identity()
    _run_shadow(identity)
    prediction_id, model_id = _latest_banded_prediction(identity)
    seed_id = str(uuid_mod.uuid4())
    day = datetime.utcnow().replace(microsecond=0)
    _sql("INSERT INTO ml_labels (id, subject_type, subject_id, label, label_kind, label_definition_version, "
         " confidence, source, event_time, status, review_status, reviewed_by, reviewed_at, idempotency_key, "
         " created_at, created_by) VALUES (CAST(:id AS uuid), 'identity', :s, 'positive', 'manual', "
         " 'threat-label-v1', 1.0, 'seed-manual', :t, 'active', 'reviewed', 'seed-reviewer', now(), :k, now(), 'seed')",
         {"id": seed_id, "s": identity, "t": day, "k": "pytest-contam-" + seed_id})
    _sql("UPDATE ml_predictions SET outcome_label_id = CAST(:l AS uuid), outcome_label = 'positive', "
         "outcome_recorded_at = now() WHERE id = CAST(:p AS uuid)", {"l": seed_id, "p": prediction_id})
    # A historical duplicate comparison row can no longer be CREATED, which is
    # a stronger guarantee than the one this test was written for.
    #
    # It used to be inserted here to simulate legacy contamination, and the
    # gate was then asserted to exclude it. Since the duplicate-prevention
    # migration (b4d5e6f7a8c9) added
    #
    #     CREATE UNIQUE INDEX uq_ml_shadow_comparison_prediction ON
    #         ml_shadow_comparisons (prediction_id)
    #
    # the insert raises instead. A duplicate cannot reach the readiness gate
    # because it cannot reach the table, so that is what is asserted — the
    # seed-label half below, which IS still insertable, is unchanged.
    import sqlalchemy.exc as _sa_exc

    with pytest.raises(_sa_exc.IntegrityError):
        _sql("INSERT INTO ml_shadow_comparisons (id, prediction_id, model_id, assessment_id, subject_id, "
             " rule_threat_score, rule_threat_severity, behavioral_anomaly_score, ml_anomaly_band, rule_would_alert, "
             " ml_would_flag_anomaly, operational_disagreement, ml_failed, created_at) "
             "SELECT gen_random_uuid(), prediction_id, model_id, assessment_id, subject_id, rule_threat_score, "
             " rule_threat_severity, behavioral_anomaly_score, ml_anomaly_band, rule_would_alert, ml_would_flag_anomaly, "
             " operational_disagreement, ml_failed, now() FROM ml_shadow_comparisons WHERE prediction_id = CAST(:p AS uuid) LIMIT 1",
             {"p": prediction_id})

    # ...and there is still exactly one.
    assert _sql("SELECT count(*) FROM ml_shadow_comparisons WHERE prediction_id = CAST(:p AS uuid)",
                {"p": prediction_id})[0][0] == 1
    try:
        overview = _overview(token)
        cov = overview["system"]["scientific_readiness"]["metrics"]["reviewed_outcome_coverage"]
        assert cov["populations"]["synthetic_or_seed"] >= 1
        assert cov["reviewed_total"] == _sql(
            "SELECT count(*) FROM ml_predictions p JOIN ml_labels l ON l.id = p.outcome_label_id "
            "WHERE p.model_id = CAST(:m AS uuid) AND l.label_kind = 'manual' AND l.review_status = 'reviewed' "
            "AND l.status = 'active' AND l.label IN ('positive','negative') AND l.source NOT ILIKE 'seed-%' "
            "AND l.source NOT ILIKE 'synthetic-%' AND l.source NOT ILIKE 'synth-%'", {"m": model_id}, scalar=True)
        assert overview["label_readiness"]["counted_reviewed_manual"]["total"] == _sql(
            "SELECT count(*) FROM ml_labels WHERE status='active' AND label_kind='manual' AND review_status='reviewed' "
            "AND source NOT ILIKE 'seed-%' AND source NOT ILIKE 'synthetic-%' AND source NOT ILIKE 'synth-%'", scalar=True)
        status, body = _http("PUT", "/api/ml/config/mode", {"mode": "ml", "reason": "pytest contamination"}, token=token)
        assert status == 409
        gates = {g["gate"]: g for g in body["detail"]["gates"]}
        assert gates["scientific_validity"]["status"] == "INSUFFICIENT_EVIDENCE"
        assert gates["signal_mapping"]["status"] == "REQUIRES_VALIDATION"
        assert _policy() is None, "labels never create a mapping policy"
        report = _evidence(token, model_id)
        assert report["data_quality"]["duplicate_comparisons"] == 0
        entry = report["models"][model_id]
        assert entry["predictions"] == _sql(
            "SELECT count(*) FROM ml_predictions WHERE model_id = CAST(:m AS uuid) AND ml_anomaly_band IS NOT NULL "
            "AND created_at >= now() - interval '365 days'", {"m": model_id}, scalar=True)
        assert "seed-manual" in report["excluded_non_evidence_labels"]
        status, summary = _http("GET", "/api/ml/shadow/summary?days=7", token=token)
        if status == 200 and not summary.get("insufficient_data"):
            assert summary["duplicate_comparisons"] == 0
        alerts = {a["code"] for a in overview["system"]["alerts"]}
        assert "NON_EVIDENCE_LABELS_PRESENT" in alerts
    finally:
        _sql("DELETE FROM ml_shadow_comparisons WHERE id IN (SELECT id FROM ml_shadow_comparisons "
             "WHERE prediction_id = CAST(:p AS uuid) ORDER BY created_at DESC OFFSET 1)", {"p": prediction_id})
        _sql("UPDATE ml_predictions SET outcome_label_id = NULL, outcome_label = NULL, outcome_recorded_at = NULL "
             "WHERE outcome_label_id = CAST(:l AS uuid)", {"l": seed_id})
        _sql("DELETE FROM ml_labels WHERE id = CAST(:l AS uuid)", {"l": seed_id})


def _scrape():
    req = urllib.request.Request(BASE + "/metrics")
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.status, r.read().decode()


def _metric(text, name, labels=None):
    for line in text.splitlines():
        if line.startswith(name) and (labels is None or labels in line):
            return float(line.rsplit(" ", 1)[1])
    return None


def test_ml_gauges_are_current_without_opening_the_overview(token):
    """Event-updated + scrape-refreshed: the server process reports the
    shadow model, mapping state, decision authority, review coverage and
    collector lag on /metrics with NO call to /api/ml/overview."""
    from backend.ml import metrics as ml_metrics
    status, text = _scrape()
    assert status == 200
    assert _metric(text, "ml_state_refreshed_timestamp_seconds") is not None, "scrape refresh ran"
    assert _metric(text, "ml_decision_authority") == 0.0
    assert _metric(text, "ml_signal_mapping_validated") == 0.0
    version = _sql("SELECT version FROM ml_models WHERE model_type = :t AND stage = 'shadow'",
                   {"t": MODEL_TYPE_BEHAVIOR_ANOMALY}, scalar=True)
    assert _metric(text, "ml_active_shadow_model_info", 'model_version="%s"' % version) == 1.0
    assert _metric(text, "ml_evidence_table_bytes", 'table="ml_predictions"') > 0
    assert _metric(text, "ml_review_coverage_ratio") is not None

    async def _event():
        from db_connection import db_manager
        await _ensure_db()
        async with db_manager.get_session() as db:
            observed = await ml_metrics.refresh_state(db, reason="pytest")
        from prometheus_client import generate_latest
        return observed, generate_latest().decode()
    observed, local = run_async(_event())
    assert observed["shadow_model"] == version and observed["mapping_validated"] is False
    assert _metric(local, "ml_active_shadow_model_info", 'model_version="%s"' % version) == 1.0
    assert ml_metrics.last_refresh_age_seconds() is not None and ml_metrics.last_refresh_age_seconds() < 60

    async def _twice():
        from db_connection import db_manager
        await _ensure_db()
        async with db_manager.get_session() as db:
            first = await ml_metrics.refresh_state_if_stale(db, max_age_seconds=300)
            second = await ml_metrics.refresh_state_if_stale(db, max_age_seconds=300)
        return first, second
    first, second = run_async(_twice())
    assert second is False, "scrape refresh is throttled per process"


def test_training_lineage_survives_without_task_history():
    """Every lineage answer comes from ml_models + ml_audit_log, never from
    background_task_history (30-day retention)."""
    model = _sql("SELECT id::text, training_job_id, created_by, created_at, dataset_id::text, "
                 "training_config, artifact_hash, code_version, feature_set_version, hyperparameters, "
                 "evaluation_report IS NOT NULL, shadow_approval, approved_by FROM ml_models "
                 "WHERE model_type = :t AND stage = 'shadow'", {"t": MODEL_TYPE_BEHAVIOR_ANOMALY})
    assert model, "shadow model precondition"
    (mid, job_id, created_by, created_at, dataset_id, cfg, artifact_hash, code_version,
     feature_set, hp, has_eval, approval, approved_by) = model[0]
    assert created_at and dataset_id and artifact_hash and feature_set and has_eval
    assert cfg["dataset_checksum"] and cfg["dataset_parquet_sha256"] and cfg["feature_schema_hash"]
    assert cfg["hyperparameters"] == hp and cfg["seed"] is not None
    assert approval and approval.get("approved_by") and approval.get("approved_at"), (
        "who approved into SHADOW and when: shadow_approval block (approved_by column is the reserved "
        "non-anomaly 'approved' stage)")
    shadow_audit = _sql("SELECT actor_username FROM ml_audit_log WHERE action = 'model_shadow' AND object_id = :m",
                        {"m": mid})
    assert shadow_audit and shadow_audit[0][0] == approval["approved_by"]
    dataset = _sql("SELECT checksum, parquet_sha256, feature_set_version, definition_name FROM ml_datasets "
                   "WHERE id = CAST(:d AS uuid)", {"d": dataset_id})
    assert dataset and dataset[0][0] == cfg["dataset_checksum"]
    started = _sql("SELECT actor_user_id, actor_username, after FROM ml_audit_log WHERE action = 'training_started' "
                   "AND object_id = :j", {"j": job_id})
    finished = _sql("SELECT after FROM ml_audit_log WHERE action = 'training_finished' AND object_id = :j", {"j": job_id})
    assert started and finished, "training_started / training_finished audit rows are the durable job record"
    assert finished[0][0]["status"] == "completed" and finished[0][0]["model_id"] == mid
    assert started[0][2]["model_type"] == MODEL_TYPE_BEHAVIOR_ANOMALY
    _sql("DELETE FROM background_task_history WHERE job_id = :j", {"j": job_id})
    again = _sql("SELECT count(*) FROM ml_audit_log WHERE object_id = :j AND action LIKE 'training_%'", {"j": job_id}, scalar=True)
    assert again >= 2, "the answers survive the transient task row"


def test_separation_of_duties_provenance_and_optional_policy(token):
    """Creator and reviewer identities are recorded as user ids; self-review
    is reported (population self_reviewed) and only refused when the policy
    knob is ON - default OFF leaves current semantics unchanged."""
    identity = _scored_identity()
    day = datetime.utcnow().replace(microsecond=0)
    status, created = _http("POST", "/api/ml/labels",
                            {"subject_id": identity, "label": "negative", "label_kind": "manual",
                             "source": "pytest-readiness-sod", "event_time": day.isoformat() + "Z"}, token=token)
    assert status == 201, created
    assert created["created_by_user_id"] is not None, "creator user id recorded"
    status, reviewed = _http("POST", "/api/ml/labels/%s/review" % created["id"], {"action": "confirm"}, token=token)
    assert status == 200, reviewed
    assert reviewed["reviewed_by_user_id"] == created["created_by_user_id"]
    from backend.ml import evidence_grade
    cols = ("created_by", "reviewed_by", "created_by_user_id", "reviewed_by_user_id",
            "label_kind", "review_status", "status", "label", "source", "selection")
    row = _sql("SELECT created_by, reviewed_by, created_by_user_id, reviewed_by_user_id, label_kind, review_status, "
               "status, label, source, selection FROM ml_labels WHERE id = CAST(:l AS uuid)", {"l": created["id"]})[0]
    fake = type("L", (), dict(zip(cols, row)))()
    assert evidence_grade.population_of(fake) == "self_reviewed"
    assert evidence_grade.is_evidence_grade(fake), "still evidence-grade while the policy is off"
    status, second = _http("POST", "/api/ml/labels",
                           {"subject_id": identity, "label": "positive", "label_kind": "manual",
                            "source": "pytest-readiness-sod2", "event_time": day.isoformat() + "Z"}, token=token)
    assert status == 201, second

    async def _confirm_with_policy():
        from config import settings
        from db_connection import db_manager
        from backend.ml.labeling_service import labeling_service
        await _ensure_db()
        previous = settings.ML_EVIDENCE_REQUIRE_INDEPENDENT_REVIEW
        settings.ML_EVIDENCE_REQUIRE_INDEPENDENT_REVIEW = True
        try:
            async with db_manager.get_session() as db:
                try:
                    await labeling_service.review_label(db, second["id"], action="confirm", actor="admin",
                                                        actor_user_id=second["created_by_user_id"])
                    return "allowed"
                except ValueError as exc:
                    return str(exc)
        finally:
            settings.ML_EVIDENCE_REQUIRE_INDEPENDENT_REVIEW = previous
    outcome = run_async(_confirm_with_policy())
    assert outcome.startswith("SELF_REVIEW_REFUSED"), outcome
    assert _sql("SELECT review_status FROM ml_labels WHERE id = CAST(:l AS uuid)", {"l": second["id"]}, scalar=True) == "unreviewed"
    _sql("DELETE FROM ml_labels WHERE source LIKE 'pytest-readiness-sod%'")

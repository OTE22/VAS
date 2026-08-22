"""
Decision Router — one switch point, truthful provenance, honest fallback.

    RULES  -> statistical anomaly signal -> risk-engine-v1
    SHADOW -> same live path; ML observes and is recorded, never applied
    ML     -> validated mapping + healthy prediction -> ML anomaly signal
              -> risk-engine-v1; ANY failure -> statistical signal for that
              request; the configured mode is never touched
    HYBRID -> gated (MODE_GATED) -> rules

Every assessment persists what actually happened; history never changes
when the administrator changes the mode, the model or the policy.
"""

import json
import os
import pickle
import urllib.error
import urllib.request
import uuid as uuid_mod
from datetime import datetime

import pytest

from backend.ml.constants import FEATURE_SET_VERSION, MODEL_TYPE_BEHAVIOR_ANOMALY
from conftest import run_on_shared_loop as run_async
from test_ml_decision_modes import (
    _ensure_db, _get_identity, _set_mode, _stop_shadow_and_cleanup, _train_and_shadow)

BASE = "http://localhost:8000"
MAPPING_VERSION = "ml-anomaly-map-pytest"
DELTA_KEYS = ("score_delta", "score_diff", "rules_ml_delta", "rules_ml_difference")
LIVE_KEYS = ("overall_risk_score", "threat_level", "severity", "score_type", "is_probability",
             "risk_factors", "algorithm_version", "calibration_status")


def _http(method, path, body=None, token=None):
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"}
    if method != "GET":   # CSRF header on mutations only; on GETs it marks a cookie session
        headers["X-Requested-With"] = "XMLHttpRequest"
    req = urllib.request.Request(BASE + path, data=data, method=method, headers=headers)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


@pytest.fixture(scope="module")
def token():
    req = urllib.request.Request(BASE + "/api/auth/login",
                                 data=json.dumps({"username": "admin", "password": "admin123"}).encode(),
                                 method="POST", headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())["access_token"]


def _sql(statement, params=None, scalar=False):
    async def _run():
        from db_connection import db_manager
        from sqlalchemy import text as sa_text
        await _ensure_db()
        async with db_manager.get_session() as db:
            res = await db.execute(sa_text(statement), params or {})
            if statement.lstrip().upper().startswith("SELECT"):
                return res.scalar() if scalar else res.all()
            await db.commit()
    return run_async(_run())


def _decide(identity_id, mode):
    """In-process router run under a forced mode; restores the mode after."""
    async def _run():
        from config import settings
        from backend.ml.decision_service import decision_service
        await _ensure_db()
        from db_connection import db_manager
        previous = settings.ML_DECISION_MODE
        settings.ML_DECISION_MODE = mode
        try:
            async with db_manager.get_session() as db:
                outcome = await decision_service.decide(db, identity_id)
                return outcome, settings.ML_DECISION_MODE
        finally:
            settings.ML_DECISION_MODE = previous
    return run_async(_run())


def _live(outcome):
    a = outcome.assessment
    return (a.overall_risk_score, a.severity, a.threat_level, a.algorithm_version,
            tuple((f["factor"], f["score"]) for f in a.risk_factors))


def _current_scope():
    """The scope a mapping must name: the shadow model, its feature set and its ACTIVE threshold set."""
    row = _sql("SELECT m.id::text, m.feature_set_version, t.scope_type, t.scope_id, t.version "
               "FROM ml_models m LEFT JOIN ml_model_thresholds t ON t.model_id = m.id AND t.status = 'active' "
               "WHERE m.model_type = :t AND m.stage = 'shadow'", {"t": MODEL_TYPE_BEHAVIOR_ANOMALY})
    assert row, "a shadow model with an active threshold set is the module precondition"
    model_id, fs, scope_type, scope_id, version = row[0]
    return {"model_id": model_id, "feature_set_version": fs,
            "threshold_version": f"{scope_type}:{scope_id or 'global'}@v{version}"}


def _install_mapping(points=None, scope=None):
    """A VALIDATED mapping policy row for the test only (removed afterwards),
    scoped to the current shadow model. The values are a test fixture, not a
    recommendation."""
    points = points or {"normal": 0, "elevated": 5, "unusual": 15, "highly_unusual": 30}
    scope = scope or _current_scope()
    _sql("DELETE FROM risk_model_versions WHERE profile = 'ml_anomaly_signal_map' AND version = :v",
         {"v": MAPPING_VERSION})
    _sql("INSERT INTO risk_model_versions (id, profile, version, weights, thresholds, status, score_type, "
         " calibration_status, calibration_data, notes, created_at, activated_at) VALUES (gen_random_uuid(), "
         " 'ml_anomaly_signal_map', :v, CAST(:w AS jsonb), '{}'::jsonb, 'active', 'heuristic', 'validated', "
         " CAST(:c AS jsonb), 'pytest fixture', now(), now())",
         {"v": MAPPING_VERSION, "w": json.dumps({"kind": "band_points", "band_points": points, "scope": scope}),
          "c": json.dumps({"source": "pytest", "evidence": "synthetic fixture"})})


def _fresh_subject_rows(identity):
    """Drop the pytest subject's persisted assessments so the next call
    persists a row of its own (a deduplicated call returns the EARLIER row
    with ITS provenance — history is the truth, which is what we test)."""
    _sql("DELETE FROM ml_shadow_comparisons WHERE assessment_id IN "
         "(SELECT id FROM threat_assessments WHERE subject_id = :s)", {"s": identity})
    _sql("UPDATE ml_predictions SET assessment_id = NULL WHERE assessment_id IN "
         "(SELECT id FROM threat_assessments WHERE subject_id = :s)", {"s": identity})
    _sql("DELETE FROM threat_assessments WHERE subject_id = :s", {"s": identity})


def _remove_mapping():
    _sql("DELETE FROM risk_model_versions WHERE profile = 'ml_anomaly_signal_map' AND version = :v",
         {"v": MAPPING_VERSION})


@pytest.fixture(scope="module", autouse=True)
def shadow_model(token):
    """An approved shadow model for the whole module; rules mode restored."""
    _set_mode(token, "rules", "pytest router setup")
    _remove_mapping()
    _train_and_shadow()
    try:
        yield
    finally:
        try:
            _set_mode(token, "rules", "pytest router teardown")
        except Exception:
            pass
        _remove_mapping()
        _stop_shadow_and_cleanup()


# ---------------------------------------------------------------------------
# Configuration state vs per-result provenance
# ---------------------------------------------------------------------------

def test_capabilities_report_the_decision_engine_now(token):
    status, body = _http("GET", "/api/security/capabilities", token=token)
    assert status == 200
    eng = body["decision_engine"]
    from config import settings
    assert eng["requested_mode"] == str(settings.ML_DECISION_MODE).lower() == "rules"
    assert eng["effective_mode"] == "rules" and eng["final_scoring_engine"] == "risk-engine-v1"
    assert eng["ml_model"]["model_type"] == MODEL_TYPE_BEHAVIOR_ANOMALY
    assert eng["ml_model"]["stage"] == "shadow"
    assert eng["signal_mapping"] is None, "no validated policy exists -> None, never invented"
    codes = {(g["mode"], g["code"]) for g in eng["gates"]}
    assert ("ml", "SIGNAL_MAPPING_UNVALIDATED") in codes and ("hybrid", "RELEASE_GATE_HYBRID") in codes
    for g in eng["gates"]:
        assert {"mode", "code", "message"} <= set(g)
        for unsafe in ("/app", "/var/", "Traceback", "Exception", "postgres", "password"):
            assert unsafe not in g["message"], g


def test_capabilities_model_is_the_inference_model_not_another_registry_row(token):
    """An unrelated registry row of another type must never be shown."""
    ghost = str(uuid_mod.uuid4())
    _sql("INSERT INTO ml_models (id, model_type, version, stage, algorithm, model_purpose, score_type, "
         " is_probability, calibration_status, artifact_name, artifact_path, artifact_hash, dependency_versions, "
         " feature_set_version, feature_names, created_at) VALUES (CAST(:id AS uuid), 'threat_ranking_model', 99, "
         " 'validated', 'pytest', 'pytest', 'anomaly_score', false, 'not_applicable', 'ghost.pkl', 'ghost.pkl', "
         " 'x', '{}'::jsonb, :fs, '[]'::jsonb, now())", {"id": ghost, "fs": FEATURE_SET_VERSION})
    try:
        status, body = _http("GET", "/api/security/capabilities", token=token)
        assert status == 200
        model = body["decision_engine"]["ml_model"]
        assert model["model_type"] == MODEL_TYPE_BEHAVIOR_ANOMALY and model["model_id"] != ghost
        assert _sql("SELECT id::text FROM ml_models WHERE model_type = :t AND stage = 'shadow'",
                    {"t": MODEL_TYPE_BEHAVIOR_ANOMALY}, scalar=True) == model["model_id"]
    finally:
        _sql("DELETE FROM ml_models WHERE id = CAST(:id AS uuid)", {"id": ghost})


# ---------------------------------------------------------------------------
# Router matrix (in-process, forced modes, configured mode untouched)
# ---------------------------------------------------------------------------

def test_router_matrix_and_live_result_invariance(token):
    identity = _get_identity(token)
    rules, _ = _decide(identity, "rules")
    shadow, _ = _decide(identity, "shadow")
    ml, mode_after = _decide(identity, "ml")
    hybrid, _ = _decide(identity, "hybrid")
    base = _live(rules)
    assert _live(shadow) == base and _live(ml) == base and _live(hybrid) == base, \
        "without a validated mapping every mode serves the identical rules result"
    p = rules.provenance.as_dict()
    assert (p["requested_mode"], p["executed_mode"], p["anomaly_signal_source"], p["ml_role"], p["fallback"]) == \
        ("rules", "rules", "rules", "none", False)
    p = shadow.provenance.as_dict()
    assert (p["requested_mode"], p["executed_mode"], p["anomaly_signal_source"], p["ml_role"]) == \
        ("shadow", "shadow", "rules", "observational") and shadow.shadow_planned
    p = ml.provenance.as_dict()
    assert (p["requested_mode"], p["executed_mode"], p["anomaly_signal_source"], p["fallback"], p["fallback_reason"]) == \
        ("ml", "rules", "rules", True, "SIGNAL_MAPPING_UNVALIDATED")
    p = hybrid.provenance.as_dict()
    assert (p["requested_mode"], p["executed_mode"], p["fallback_reason"]) == ("hybrid", "rules", "MODE_GATED")
    assert p["gates"] and all({"mode", "code", "message"} <= set(g) for g in p["gates"])
    for outcome in (rules, shadow, ml, hybrid):
        assert outcome.provenance.final_scoring_engine == "risk-engine-v1"
        assert not set(outcome.provenance.as_dict()) & set(DELTA_KEYS)


def test_ml_mode_with_validated_mapping_supplies_the_anomaly_input_only(token):
    identity = _get_identity(token)
    rules, _ = _decide(identity, "rules")
    _install_mapping()
    try:
        ml, mode_after = _decide(identity, "ml")
    finally:
        _remove_mapping()
    assert mode_after == "ml", "the router never mutates the configured mode"
    p = ml.provenance.as_dict()
    if p["fallback"]:
        # ML could not score this identity (e.g. features unavailable) -> honest per-request fallback
        assert p["executed_mode"] == "rules" and p["anomaly_signal_source"] == "rules"
        assert p["fallback_reason"] and p["fallback_reason"] != "SIGNAL_MAPPING_UNVALIDATED"
        assert _live(ml) == _live(rules)
        pytest.skip(f"ML inference fell back for this identity ({p['fallback_reason']}); "
                    "substitution path exercised up to inference")
    assert (p["requested_mode"], p["executed_mode"], p["anomaly_signal_source"], p["ml_role"]) == \
        ("ml", "ml", "ml", "anomaly_signal")
    assert p["signal_mapping_version"] == MAPPING_VERSION and p["ml_model_id"] and p["ml_model_version"]
    assert p["final_scoring_engine"] == "risk-engine-v1" and p["fallback"] is False
    # ONLY the behavioral-anomaly signal may differ; every other signal is identical
    def non_anomaly(outcome):
        return tuple((f["factor"], f["score"]) for f in outcome.assessment.risk_factors
                     if f["factor"] != "Behavioral Anomalies")
    assert non_anomaly(ml) == non_anomaly(rules)
    assert ml.assessment.engine["anomaly_signal_source"] == "ml"
    assert ml.assessment.engine["signal_mapping_version"] == MAPPING_VERSION
    assert ml.assessment.engine["score_type"] == "heuristic" and ml.assessment.engine["is_probability"] is False
    anomaly = next(f for f in ml.assessment.risk_factors if f["factor"] == "Behavioral Anomalies")
    assert 0.0 <= anomaly["score"] <= 30.0 and "mapped by " + MAPPING_VERSION in anomaly["description"]
    assert ml.prediction_id, "the ML-mode prediction is persisted as lineage"
    row = _sql("SELECT requested_mode, actual_mode_used, fallback_reason FROM ml_predictions WHERE id = CAST(:p AS uuid)",
               {"p": ml.prediction_id})[0]
    assert tuple(row) == ("ml", "ml", None)


def test_mapping_scoped_to_another_model_or_threshold_is_not_reused(token):
    identity = _get_identity(token)
    scope = _current_scope()
    for changed in ({"model_id": str(uuid_mod.uuid4())}, {"feature_set_version": "secintel-features-v1"},
                    {"threshold_version": "global:@v999"}):
        _install_mapping(scope={**scope, **changed})
        try:
            outcome, _ = _decide(identity, "ml")
        finally:
            _remove_mapping()
        p = outcome.provenance.as_dict()
        assert p["fallback"] is True and p["fallback_reason"] == "SIGNAL_MAPPING_UNVALIDATED", (changed, p)
        assert p["anomaly_signal_source"] == "rules"


def test_ml_mode_fallback_matrix_never_breaks_rules_and_recovers(token, monkeypatch):
    identity = _get_identity(token)
    rules, _ = _decide(identity, "rules")
    from backend.ml import inference_service as inf_mod
    from backend.ml.inference_service import MLPredictionResult
    real_predict = inf_mod.inference_service.predict_identity

    async def failing(db, identity_id, **kw):
        raise RuntimeError("simulated ML crash")

    async def timed_out(db, identity_id, **kw):
        return MLPredictionResult(ok=False, failure_reason="PREDICTION_TIMEOUT")

    async def no_model(db, identity_id, **kw):
        return MLPredictionResult(ok=False, failure_reason="NO_APPROVED_MODEL")

    async def malformed(db, identity_id, **kw):
        return MLPredictionResult(ok=True, model_id=uuid_mod.uuid4(), model_version_label="pytest-bad",
                                  behavioral_anomaly_score=7.5, ml_anomaly_band="weird")

    async def missing_features(db, identity_id, **kw):
        return MLPredictionResult(ok=False, failure_reason="MISSING_REQUIRED_FEATURES")

    async def tampered(db, identity_id, **kw):
        return MLPredictionResult(ok=False, failure_reason="ARTIFACT_HASH_MISMATCH")

    _install_mapping()
    try:
        for stub, expected in ((failing, "PREDICTION_FAILED"), (timed_out, "PREDICTION_TIMEOUT"),
                               (no_model, "NO_APPROVED_MODEL"), (malformed, "INVALID_PREDICTION"),
                               (missing_features, "MISSING_REQUIRED_FEATURES"),
                               (tampered, "ARTIFACT_HASH_MISMATCH")):
            monkeypatch.setattr(inf_mod.inference_service, "predict_identity", stub)
            outcome, mode_after = _decide(identity, "ml")
            p = outcome.provenance.as_dict()
            assert p["fallback"] is True and p["fallback_reason"] == expected, (expected, p)
            assert p["executed_mode"] == "rules" and p["anomaly_signal_source"] == "rules"
            assert _live(outcome) == _live(rules), expected
            assert mode_after == "ml", "a transient failure never changes the administrator's setting"
        # recovery: the real inference again on the very next request
        monkeypatch.setattr(inf_mod.inference_service, "predict_identity", real_predict)
        recovered, _ = _decide(identity, "ml")
        assert recovered.provenance.fallback_reason != "PREDICTION_FAILED"
        assert _live(recovered)[1:4] == _live(rules)[1:4] or recovered.provenance.anomaly_signal_source == "ml"
    finally:
        monkeypatch.setattr(inf_mod.inference_service, "predict_identity", real_predict)
        _remove_mapping()


# ---------------------------------------------------------------------------
# HTTP: provenance on the payload, persisted, independent of today's mode
# ---------------------------------------------------------------------------

def test_threat_payload_carries_provenance_and_observation_under_shadow(token):
    identity = _get_identity(token)
    _fresh_subject_rows(identity)
    _set_mode(token, "shadow", "pytest router shadow")
    try:
        status, body = _http("GET", f"/api/security/threat/{identity}", token=token)
        assert status == 200, body
        prov = body["decision_provenance"]
        assert prov["requested_mode"] == "shadow" and prov["executed_mode"] == "shadow"
        assert prov["anomaly_signal_source"] == "rules" and prov["ml_role"] == "observational"
        assert prov["fallback"] is False and prov["final_scoring_engine"] == "risk-engine-v1"
        assert body["score_type"] == "heuristic" and body["is_probability"] is False
        obs = body["ml_observation"]
        assert obs is not None and obs["executed"] is True and obs["role"] == "observational"
        assert obs["applied_to_live_result"] is False
        assert obs["signal_name"] == "behavioral_anomaly" and obs["score_semantics"] == "anomaly_score_not_probability"
        if obs["ml_failed"]:
            assert obs["failure_reason"] and obs["score"] is None and obs["band"] is None
        else:
            assert obs["band"] in ("normal", "elevated", "unusual", "highly_unusual")
            assert 0.0 <= float(obs["score"]) <= 1.0
            assert obs["outcome_relation"] in ("both_flagged", "rules_only", "ml_only", "neither")
        assert not set(body) & set(DELTA_KEYS) and not set(obs) & set(DELTA_KEYS)
        assessment_id = body["assessment_id"]
        assert assessment_id and not body.get("deduplicated"), body.get("deduplicated")
    finally:
        _set_mode(token, "rules", "pytest router restore")

    # History: the same row read back under RULES still says SHADOW
    status, detail = _http("GET", f"/api/security/assessments/{assessment_id}", token=token)
    assert status == 200, detail
    prov = detail["decision_provenance"]
    assert prov["requested_mode"] == "shadow" and prov["executed_mode"] == "shadow" and prov["recorded"] is True
    status, caps = _http("GET", "/api/security/capabilities", token=token)
    assert caps["decision_engine"]["requested_mode"] == "rules", "the page pill follows the configuration"
    status, hist = _http("GET", f"/api/security/assessments/history/identity/{identity}?page_size=5", token=token)
    assert status == 200 and any(a["id"] == assessment_id and a["decision_provenance"]["executed_mode"] == "shadow"
                                 for a in hist["items"])
    row = _sql("SELECT requested_mode, decision_mode, anomaly_signal_source, fallback_reason FROM threat_assessments "
               "WHERE id = CAST(:a AS uuid)", {"a": assessment_id})[0]
    assert tuple(row) == ("shadow", "shadow", "rules", None)


def test_ml_requested_without_mapping_persists_the_fallback(token):
    identity = _get_identity(token)
    status, body = _http("PUT", "/api/settings/ML_DECISION_MODE",
                         {"value": "ml", "change_reason": "pytest router ml"}, token=token)
    if status != 200:
        pytest.skip(f"ML mode cannot be configured on this stack ({status}: {body.get('detail')})")
    try:
        _fresh_subject_rows(identity)
        status, body = _http("POST", "/api/security/assessments",
                             {"subject_type": "identity", "subject_id": identity,
                              "event_id": "pytest-router-" + uuid_mod.uuid4().hex[:8]}, token=token)
        assert status in (200, 201), body
        assert not body.get("deduplicated"), body
        prov = body["decision_provenance"]
        assert prov["requested_mode"] == "ml" and prov["executed_mode"] == "rules"
        assert prov["fallback"] is True and prov["fallback_reason"] == "SIGNAL_MAPPING_UNVALIDATED"
        assert body["score_type"] == "heuristic"
        row = _sql("SELECT requested_mode, decision_mode, fallback_reason FROM threat_assessments WHERE id = CAST(:a AS uuid)",
                   {"a": body["id"]})[0]
        assert tuple(row) == ("ml", "rules", "SIGNAL_MAPPING_UNVALIDATED")
    finally:
        _set_mode(token, "rules", "pytest router restore")


def test_rules_only_features_name_their_engine(token):
    identity = _get_identity(token)
    checks = [
        (f"/api/security/anomalies/{identity}?days_back=7", "anomaly-context-v3"),
        ("/api/security/patterns?days_back=7", "patterns-v3"),
        (f"/api/identities/{identity}/related", "related-cooccurrence-v1"),
        (f"/api/identities/{identity}/temporal-patterns?days_back=30", "temporal-activity-v1"),
        ("/api/security/network?days_back=7", "risk-engine-v1"),
    ]
    for path, version in checks:
        status, body = _http("GET", path, token=token)
        assert status == 200, (path, body)
        eng = body["engine"]
        assert eng["kind"] == "rules" and eng["ml_participates"] is False and eng["algorithm_version"] == version, (path, eng)
        assert eng["name"]
    status, body = _http("GET", f"/api/security/anomalies/{identity}?days_back=7", token=token)
    assert body["algorithm_version"] == "anomaly-context-v3", "existing pins byte-for-byte"


def test_pre_provenance_rows_are_reported_not_rewritten(token):
    identity = _get_identity(token)
    _fresh_subject_rows(identity)
    status, body = _http("GET", f"/api/security/threat/{identity}", token=token)
    assert status == 200
    assessment_id = body["assessment_id"]
    _sql("UPDATE threat_assessments SET requested_mode = NULL, anomaly_signal_source = NULL, "
         "signal_mapping_version = NULL, fallback_reason = NULL WHERE id = CAST(:a AS uuid)", {"a": assessment_id})
    status, detail = _http("GET", f"/api/security/assessments/{assessment_id}", token=token)
    prov = detail["decision_provenance"]
    assert prov["requested_mode"] == "not_recorded" and prov["recorded"] is False
    assert prov["executed_mode"] == (detail["decision_mode"] or "rules")


def test_frontend_renders_provenance_from_payload_only():
    sec = open("/app/frontend/js/admin-security-intelligence.js", encoding="utf-8").read()
    intel = open("/app/frontend/js/admin-intelligence.js", encoding="utf-8").read()
    for src in (sec, intel):
        assert "function renderEngineModePill" in src and "function buildEngineBadge" in src
        assert ".innerHTML" not in src and "insertAdjacentHTML" not in src
        assert "co-appearance statistics" not in src and "(no ML)" not in src.replace("(any mode)", "")
    assert "function buildDecisionBlock" in sec and "function buildObservationPanel" in sec
    assert "function loadThreatHistory" in sec
    assert "delta" not in sec.lower().split("function buildobservationpanel", 1)[1][:4000]
    html = open("/app/frontend/admin/security-intelligence.html", encoding="utf-8").read()
    assert 'id="engine-mode-pill"' in html and 'id="threat-history"' in html

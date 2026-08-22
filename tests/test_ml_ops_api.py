"""
ML Milestone 5: /api/ml/* operations surface.
=============================================
Run INSIDE the api container against the live app:

    docker exec face_recognition_api python -m pytest tests/test_ml_ops_api.py -v

Pins: every route requires authentication; ML_MANAGE stays admin-only in the
capability registry; cookie-mode mutations demand the CSRF header; gated
modes 409 with the exact unmet gates; pagination is bounded; responses are
path-free; mutations write audit rows; scheduled retraining refuses to
enable; the seven warnings arrive verbatim with no-store caching.
"""

import json
import urllib.error
import urllib.request

import pytest

BASE = "http://localhost:8000"

GET_ROUTES = [
    "/api/ml/overview",
    "/api/ml/features/definitions",
    "/api/ml/labels/stats",
    "/api/ml/labels",
    "/api/ml/datasets",
    "/api/ml/datasets/definitions",
    "/api/ml/models",
    "/api/ml/predictions",
    "/api/ml/shadow/summary",
    "/api/ml/shadow/evidence",
    "/api/ml/drift/reports",
    "/api/ml/retraining-policy/behavior_anomaly_model",
    "/api/ml/audit",
    "/api/ml/calls",
]

MUTATIONS = [
    ("PUT", "/api/ml/config/mode", {"mode": "rules", "reason": "pytest"}),
    ("POST", "/api/ml/pause", {"reason": "pytest"}),
    ("POST", "/api/ml/features/compute", {}),
    ("POST", "/api/ml/labels", {"subject_id": "0" * 36, "label": "negative",
                                "event_time": "2026-01-01T00:00:00Z"}),
    ("POST", "/api/ml/datasets", {"name": "x", "kind": "unsupervised"}),
    ("POST", "/api/ml/datasets/backfill-hashes", {}),
    ("POST", "/api/ml/datasets/00000000-0000-0000-0000-000000000000/archive", {"reason": "pytest"}),
    ("POST", "/api/ml/training-jobs", {"model_type": "behavior_anomaly_model"}),
    ("POST", "/api/ml/shadow/stop", {"reason": "pytest"}),
    ("POST", "/api/ml/drift/run", {}),
    ("PUT", "/api/ml/retraining-policy/behavior_anomaly_model",
     {"enabled": False, "schedule_interval_hours": 168, "min_new_labels": 25,
      "min_total_labels": 100, "cooldown_hours": 168, "min_drift_reports": 2}),
]


def _http(method, path, body=None, token=None, headers=None, timeout=60):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read() or b"{}"), dict(resp.headers)
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw or b"{}"), dict(e.headers)
        except Exception:
            return e.code, {"_raw": raw.decode(errors="replace")}, dict(e.headers)


@pytest.fixture(scope="module")
def token():
    status, body, _ = _http("POST", "/api/auth/login",
                            {"username": "admin", "password": "admin123"})
    assert status == 200, body
    return body["access_token"]


# ---------------------------------------------------------------------------
# Authentication + authorization
# ---------------------------------------------------------------------------

def test_every_get_route_rejects_anonymous_callers():
    for path in GET_ROUTES:
        status, _, _ = _http("GET", path)
        assert status in (401, 403), f"{path} answered {status} without credentials"


def test_every_mutation_rejects_anonymous_callers():
    for method, path, body in MUTATIONS:
        status, _, _ = _http(method, path, body)
        assert status in (401, 403), f"{method} {path} answered {status} without credentials"


def test_ml_manage_is_an_admin_only_capability():
    """Non-admin roles must not carry ML_MANAGE — the registry is the gate."""
    from backend.auth.capabilities import Capability, ROLE_CAPABILITIES
    holders = [role for role, caps in ROLE_CAPABILITIES.items()
               if Capability.ML_MANAGE in caps]
    names = sorted(getattr(r, "value", str(r)) for r in holders)
    assert names == ["admin"], f"ML_MANAGE leaked beyond admin: {names}"


def test_cookie_mode_mutations_require_the_csrf_header():
    """require_mlops_csrf: bearer clients are exempt; cookie clients must
    send X-Requested-With. Exercised directly against the dependency."""
    from fastapi import HTTPException
    from starlette.requests import Request as StarletteRequest
    from backend.routes.ml_ops import require_mlops_csrf

    def make_request(headers):
        raw = [(k.lower().encode(), v.encode()) for k, v in headers.items()]
        return StarletteRequest({"type": "http", "method": "POST", "path": "/api/ml/pause",
                                 "headers": raw, "query_string": b""})

    with pytest.raises(HTTPException) as exc:
        require_mlops_csrf(make_request({}))
    assert exc.value.status_code == 403

    require_mlops_csrf(make_request({"x-requested-with": "XMLHttpRequest"}))
    require_mlops_csrf(make_request({"authorization": "Bearer x"}))


# ---------------------------------------------------------------------------
# Overview: warnings verbatim + no-store
# ---------------------------------------------------------------------------

def test_overview_carries_the_seven_warnings_verbatim(token):
    status, body, headers = _http("GET", "/api/ml/overview", token=token)
    assert status == 200, body
    assert body["warnings"] == [
        "Anomaly does not mean threat.",
        "Heuristic scores are not probabilities.",
        "Uncalibrated model outputs are not probabilities.",
        "Shadow mode does not affect live decisions.",
        "ML and HYBRID modes are currently gated.",
        "Drift does not automatically prove model failure.",
        "Human review remains required.",
    ]
    lowered = {k.lower(): v for k, v in headers.items()}
    assert lowered.get("cache-control") == "no-store"
    # The four modes are all present, each with reasons when unavailable.
    modes = body["mode"]["modes"]
    assert set(modes) == {"rules", "shadow", "hybrid", "ml"}
    for name, info in modes.items():
        if not info["available"]:
            assert info["reasons"], f"mode {name} is unavailable without a reason"
    # Optional capabilities report all four distinct statuses.
    for name, cap in body["optional_capabilities"].items():
        assert set(cap) == {"configured", "implemented",
                            "dependency_available", "operational"}, name


# ---------------------------------------------------------------------------
# Gated modes 409 with exact reasons
# ---------------------------------------------------------------------------

def test_hybrid_and_ml_modes_409_with_unmet_gates(token):
    for mode in ("hybrid", "ml"):
        status, body, _ = _http("PUT", "/api/ml/config/mode",
                                {"mode": mode, "reason": "pytest gate check"},
                                token=token)
        assert status == 409, body
        detail = body["detail"]
        assert detail["error_code"] == "MODE_GATED"
        assert detail["unmet_gates"], f"{mode} gated without stating why"


def test_unknown_mode_is_rejected_by_validation(token):
    status, body, _ = _http("PUT", "/api/ml/config/mode",
                            {"mode": "banana", "reason": "pytest"}, token=token)
    assert status == 422, body


# ---------------------------------------------------------------------------
# Pagination bounds
# ---------------------------------------------------------------------------

def test_pagination_is_bounded(token):
    status, _, _ = _http("GET", "/api/ml/labels?page_size=1000", token=token)
    assert status == 422
    status, _, _ = _http("GET", "/api/ml/models?page=0", token=token)
    assert status == 422
    status, _, _ = _http("GET", "/api/ml/predictions?page_size=100&page=1", token=token)
    assert status == 200


# ---------------------------------------------------------------------------
# Path-free responses
# ---------------------------------------------------------------------------

def test_no_route_exposes_filesystem_paths(token):
    forbidden = ("artifact_path", "storage_path", "/app/", "C:\\", "models/ml/")
    for path in GET_ROUTES:
        status, body, _ = _http("GET", path, token=token)
        assert status == 200, f"{path} -> {status}: {body}"
        payload = json.dumps(body)
        for needle in forbidden:
            assert needle not in payload, f"{path} leaks {needle!r}"


def test_model_detail_is_path_free_when_a_model_exists(token):
    status, body, _ = _http("GET", "/api/ml/models?page_size=1", token=token)
    assert status == 200
    items = body.get("items", [])
    if not items:
        pytest.skip("no models registered")
    status, detail, _ = _http("GET", f"/api/ml/models/{items[0]['id']}", token=token)
    assert status == 200
    payload = json.dumps(detail)
    for needle in ("artifact_path", "storage_path", "/app/", "C:\\", "models/ml/"):
        assert needle not in payload, f"model detail leaks {needle!r}"


# ---------------------------------------------------------------------------
# Audit trail on mutations
# ---------------------------------------------------------------------------

def test_pause_writes_an_audit_row(token):
    status, body, _ = _http("POST", "/api/ml/pause",
                            {"reason": "pytest audit-trail check"}, token=token)
    assert status == 200, body
    assert body["mode"] == "rules"
    status, audit, _ = _http("GET", "/api/ml/audit?page_size=5", token=token)
    assert status == 200
    actions = [item["action"] for item in audit["items"]]
    assert "pause" in actions, f"pause left no audit trace in {actions}"
    latest = audit["items"][0]
    assert latest["actor_username"], "audit rows must attribute an actor"


# ---------------------------------------------------------------------------
# Training-job refusals + retraining policy gate
# ---------------------------------------------------------------------------

def test_training_rejects_unknown_and_reserved_model_types(token):
    status, body, _ = _http("POST", "/api/ml/training-jobs",
                            {"model_type": "nonsense_model"}, token=token)
    assert status == 422
    assert body["detail"]["error_code"] == "UNKNOWN_MODEL_TYPE"

    status, body, _ = _http("POST", "/api/ml/training-jobs",
                            {"model_type": "coappearance_anomaly_model"}, token=token)
    assert status == 422
    assert body["detail"]["error_code"] == "MODEL_TYPE_NOT_IMPLEMENTED"


def test_training_job_lookup_404s_for_unknown_ids(token):
    status, _, _ = _http("GET", "/api/ml/training-jobs/never-existed", token=token)
    assert status == 404


def test_scheduled_retraining_cannot_be_enabled(token):
    status, body, _ = _http("GET", "/api/ml/retraining-policy/behavior_anomaly_model",
                            token=token)
    assert status == 200
    assert body["enabled"] is False, "policies must ship disabled"

    status, body, _ = _http(
        "PUT", "/api/ml/retraining-policy/behavior_anomaly_model",
        {"enabled": True, "schedule_interval_hours": 168, "min_new_labels": 25,
         "min_total_labels": 100, "cooldown_hours": 168, "min_drift_reports": 2},
        token=token)
    assert status == 409, body
    assert body["detail"]["error_code"] == "SCHEDULED_RETRAINING_GATED"

    status, _, _ = _http("GET", "/api/ml/retraining-policy/not_a_model_type", token=token)
    assert status == 404


# ---------------------------------------------------------------------------
# Label validation
# ---------------------------------------------------------------------------

def test_label_creation_validates_its_vocabulary(token):
    status, _, _ = _http("POST", "/api/ml/labels",
                         {"subject_id": "0" * 36, "label": "suspicious",
                          "event_time": "2026-01-01T00:00:00Z"}, token=token)
    assert status == 422, "label vocabulary is positive|negative|unknown"

    status, _, _ = _http("POST", "/api/ml/labels",
                         {"subject_id": "short", "label": "negative",
                          "event_time": "2026-01-01T00:00:00Z"}, token=token)
    assert status == 422, "subject ids shorter than 8 chars are rejected"


# ---------------------------------------------------------------------------
# Registry lifecycle errors surface as structured codes
# ---------------------------------------------------------------------------

def test_lifecycle_actions_on_missing_models_fail_closed(token):
    ghost = "00000000-0000-0000-0000-000000000000"
    status, _, _ = _http("POST", f"/api/ml/models/{ghost}/shadow-approve",
                         {"reason": "pytest ghost"}, token=token)
    assert status in (404, 409, 422)
    status, _, _ = _http("POST", f"/api/ml/models/{ghost}/reject",
                         {"reason": "pytest ghost"}, token=token)
    assert status in (404, 409, 422)


def test_every_call_is_logged_with_its_request_id(token):
    """One structured record per /api/ml/* call: the request id equals the
    X-Request-ID the client received, refusals carry their stable code, the
    record is in the dedicated file AND the app log, and nothing in the
    payload is a filesystem path."""
    import json as _json
    import os
    req = urllib.request.Request(BASE + "/api/ml/overview", headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=60) as r:
        rid = r.headers.get("X-Request-ID")
    assert rid
    status, body, _ = _http("GET", "/api/ml/datasets/not-a-uuid", None, token)
    assert status == 422
    status, calls, _ = _http("GET", "/api/ml/calls?limit=20", None, token)
    assert status == 200 and calls["items"]
    by_rid = {c["request_id"]: c for c in calls["items"]}
    assert rid in by_rid, "the overview call is logged under the id the client received"
    rec = by_rid[rid]
    assert rec["method"] == "GET" and rec["route"] == "/api/ml/overview" and rec["status"] == 200
    assert rec["actor"] == "admin" and isinstance(rec["ms"], int) and rec["ms"] >= 0
    refusal = next(c for c in calls["items"] if c["route"] == "/api/ml/datasets/{dataset_id}")
    assert refusal["status"] == 422 and refusal["error_code"] == "INVALID_DATASET_ID"
    blob = _json.dumps(calls)
    for banned in ("/app/", "models/ml", "storage_path", "artifact_path", "C:\\"):
        assert banned not in blob, banned
    status, errors, _ = _http("GET", "/api/ml/calls?errors_only=true&limit=50", None, token)
    assert status == 200 and all(int(c["status"]) >= 400 for c in errors["items"])
    # The SERVER process writes the tagged record into the application log
    # (utils/logging.py owns the handlers; this test process has its own QA
    # log dir, so the server file is named explicitly — the directory conftest
    # promises to leave untouched).
    path = "/var/log/face-recognition/app.log"
    assert os.path.exists(path), path
    with open(path, "rb") as f:
        f.seek(0, os.SEEK_END); size = f.tell(); f.seek(max(0, size - 2_000_000))
        tail = f.read().decode("utf-8", errors="replace")
    assert f"[MLOPS_CALL]" in tail and rid in tail, "the app log carries the tagged record"


def test_overview_system_state_is_factual_and_path_free(token):
    status, body, _ = _http("GET", "/api/ml/overview", None, token)
    assert status == 200, body
    system = body["system"]
    from backend.ml.constants import FEATURE_SET_VERSION, PREVIOUS_FEATURE_SET_VERSION
    assert system["feature_set"]["current"] == FEATURE_SET_VERSION
    assert system["feature_set"]["previous"] == PREVIOUS_FEATURE_SET_VERSION
    names = {f["name"] for f in system["feature_set"]["active_person_features"]}
    assert {"is_unknown_identity", "days_since_last_seen"} <= names
    assert system["decision"]["live_engine"] == "rules"
    assert system["decision"]["requested_mode"] in ("rules", "shadow", "hybrid", "ml")
    assert "hybrid" in system["decision"]["gated"]
    assert system["datasets"]["extraction_policy_version"] == "explicit-cap-v1"
    assert system["call_log"]["enabled"] is True and "MLOPS_CALL" in system["call_log"]["sink"]
    assert system["migration_head"]
    assert isinstance(system["alerts"], list) and isinstance(system["release_notes"], list)
    assert all({"code", "level", "message"} <= set(a) for a in system["alerts"])
    assert len(system["release_notes"]) >= 6 and all({"id", "title", "detail"} <= set(n) for n in system["release_notes"])
    if system["shadow_model"]:
        sm = system["shadow_model"]
        expect_alert = sm["feature_set_version"] != FEATURE_SET_VERSION
        assert sm["compatible_with_current_features"] is (not expect_alert)
        assert any(a["code"] == "SHADOW_MODEL_FEATURE_SET_MISMATCH" for a in system["alerts"]) is expect_alert
    blob = json.dumps(system)
    for banned in ("/app/", "models/ml", "/var/log", "storage_path", "artifact_path"):
        assert banned not in blob, banned

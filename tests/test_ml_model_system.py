"""
ML Model Management system tests
================================
Run INSIDE the api container against the live app:

    docker exec face_recognition_api python -m pytest tests/test_ml_model_system.py -v

Proves the model-lifecycle overhaul contract:
  * status is strictly typed (real booleans/ints), DB-backed (persistent
    samples — not the in-memory list that reset on restart), exposes
    structured readiness checks, and NEVER leaks filesystem paths
  * training runs as a background job (202 + job_id; 409 single-flight;
    400 DATASET_NOT_READY with structured checks) protected by CSRF
  * group-aware splitting cannot leak an identity pair across the split
  * metrics include operational merge-decision numbers (precision,
    false-merge rate, confusion matrix) — not only R²/MSE
  * candidates never overwrite the active artifact; quality gates block
    unsafe activation; activation is atomic (one active row, previous
    archived); rollback restores an archived version and the runtime
  * END-TO-END: seed dataset -> train -> candidate -> activate -> retrain
    -> activate v2 -> rollback to v1 — with full cleanup
  * frontend: typed normalizers ("false" is false, zero stays zero, no
    negative sample counts), zero innerHTML, no injected window globals,
    no arbitrary refresh delays, no path exposure, CSRF header on mutations
"""

import json
import os
import shutil
import time
import urllib.request
import urllib.error

import pytest

from conftest import run_on_shared_loop as run_async  # asyncpg is loop-bound

BASE = "http://localhost:8000"

JS_PATH = "/app/frontend/js/admin-ml-model.js"
HTML_PATH = "/app/frontend/admin/ml-model.html"
IDENTITIES_ROUTES_PATH = "/app/backend/routes/identities.py"
DASHBOARD_ROUTES_PATH = "/app/backend/routes/dashboard.py"
SERVICE_PATH = "/app/backend/core/model_training_service.py"

CSRF = {"X-Requested-With": "XMLHttpRequest"}
LEGACY_MODEL_PATH = "models/similarity_model.pkl"


def _read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def _http(method, path, body=None, token=None, cookie=None, headers=None, timeout=60):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    if cookie:
        req.add_header("Cookie", cookie)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            try:
                payload = json.loads(raw or b"{}")
            except Exception:
                payload = {"_raw": raw.decode(errors="replace")}
            return resp.status, payload, resp.headers
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            payload = json.loads(raw or b"{}")
        except Exception:
            payload = {"_raw": raw.decode(errors="replace")}
        return e.code, payload, e.headers


@pytest.fixture(scope="module")
def token():
    status, body, _ = _http("POST", "/api/auth/login",
                            {"username": "admin", "password": "admin123"})
    assert status == 200, f"admin login failed: {body}"
    return body["access_token"]


@pytest.fixture(scope="module")
def cookie(token):
    return f"access_token={token}"


# ---------------------------------------------------------------------------
# Status contract
# ---------------------------------------------------------------------------

def test_model_status_strictly_typed_and_path_free(token):
    status, body, headers = _http("GET", "/api/admin/merge-suggestions/model-status", token=token)
    assert status == 200, body
    # Real JSON types — never "false"/"50" strings
    assert isinstance(body["is_trained"], bool)
    assert isinstance(body["ready_to_train"], bool)
    assert isinstance(body["training_samples"], int)
    assert isinstance(body["approved_samples"], int)
    assert isinstance(body["min_samples"], int)
    # Structured readiness — sample count is NOT the only rule
    checks = body["readiness_checks"]
    for key in ("total_samples", "approved_samples", "rejected_samples", "class_balance"):
        assert key in checks, f"readiness check missing: {key}"
    assert "ratio" in checks["class_balance"]
    # Filesystem paths never leave the server
    dumped = json.dumps(body)
    assert "model_path" not in dumped
    assert "artifact_path" not in dumped
    assert "/app/" not in dumped and "models/similarity" not in dumped
    # Typed effective configuration
    config = body["configuration"]
    assert isinstance(config["auto_train"], bool)
    assert config["effective_at"].endswith("Z")
    assert "no-store" in (headers.get("Cache-Control") or "").lower()


def test_model_status_requires_auth():
    status, _, _ = _http("GET", "/api/admin/merge-suggestions/model-status")
    assert status == 401


# ---------------------------------------------------------------------------
# CSRF on every mutation
# ---------------------------------------------------------------------------

def test_ml_mutations_require_csrf(cookie):
    cases = [
        ("POST", "/api/admin/merge-suggestions/training-jobs"),
        ("POST", "/api/admin/merge-suggestions/train-model"),
        ("POST", "/api/admin/merge-suggestions/training-jobs/simtrain-x/cancel"),
        ("POST", "/api/admin/merge-suggestions/models/1/activate"),
        ("POST", "/api/admin/merge-suggestions/models/1/reject"),
        ("POST", "/api/admin/merge-suggestions/models/1/rollback"),
    ]
    for method, path in cases:
        status, body, _ = _http(method, path, body={}, cookie=cookie)
        assert status == 403, f"{method} {path}: expected CSRF 403, got {status} {body}"
        assert "CSRF" in str(body.get("detail", ""))


# ---------------------------------------------------------------------------
# Training service unit contracts (in-container imports)
# ---------------------------------------------------------------------------

def test_group_split_prevents_pair_leakage():
    from backend.core import model_training_service as mts
    dataset = []
    for pair_index in range(10):
        for _ in range(5):  # 5 rows per identity pair
            dataset.append({"id": len(dataset), "group": f"pair-{pair_index}",
                            "features": [0.5] * 6, "label": 1.0})
    train, val, meta = mts.group_aware_split(dataset, seed=42)
    train_groups = {r["group"] for r in train}
    val_groups = {r["group"] for r in val}
    assert not (train_groups & val_groups), "an identity pair leaked across the split"
    assert len(train) + len(val) == len(dataset)
    assert meta["split_method"] == "identity_pair_grouped"
    assert meta["seed"] == 42
    # Deterministic: same inputs -> identical split
    train2, val2, _ = mts.group_aware_split(dataset, seed=42)
    assert [r["id"] for r in train2] == [r["id"] for r in train]


def test_metrics_include_operational_decisions():
    import numpy as np
    from backend.core import model_training_service as mts
    y_true = np.array([1.0, 1.0, 0.0, 0.0, 1.0, 0.0])
    y_pred = np.array([0.9, 0.8, 0.1, 0.7, 0.4, 0.2])  # one FP, one FN
    m = mts.compute_metrics(y_true, y_pred)
    for key in ("r2", "mse", "rmse", "mae", "precision", "recall", "f1",
                "false_merge_rate", "missed_merge_rate", "confusion_matrix"):
        assert key in m, f"metric missing: {key}"
    cm = m["confusion_matrix"]
    assert cm == {"tp": 2, "fp": 1, "tn": 2, "fn": 1}
    assert m["false_merge_rate"] == pytest.approx(1 / 3, abs=1e-6)
    assert m["missed_merge_rate"] == pytest.approx(1 / 3, abs=1e-6)
    # Degenerate input never crashes (no divisions by zero)
    empty_pos = mts.compute_metrics(np.array([0.0, 0.0]), np.array([0.1, 0.2]))
    assert empty_pos["precision"] is None and empty_pos["missed_merge_rate"] is None


def test_quality_gates_block_unsafe_candidates():
    from backend.core import model_training_service as mts
    unsafe = {"precision": 0.5, "false_merge_rate": 0.5, "sample_count": 100}
    verdict = mts.evaluate_quality_gates(unsafe)
    assert verdict["passed"] is False
    safe = {"precision": 0.99, "false_merge_rate": 0.01, "sample_count": 100}
    assert mts.evaluate_quality_gates(safe)["passed"] is True
    tiny = {"precision": 0.99, "false_merge_rate": 0.01, "sample_count": 2}
    assert mts.evaluate_quality_gates(tiny)["passed"] is False, \
        "too few validation samples must fail the gates"


def test_training_single_flight_guard():
    from backend.core import model_training_service as mts
    # clean slate
    mts._TRAINING_JOB["job_id"] = None
    mts._TRAINING_JOB["started_at"] = None
    assert mts.try_acquire_training("simtrain-a") is None
    assert mts.try_acquire_training("simtrain-b") == "simtrain-a"
    assert mts.request_cancel("simtrain-a") is True
    mts.release_training("simtrain-a")
    assert mts.running_training_job() is None


def test_atomic_artifact_handling_in_source():
    src = _read(SERVICE_PATH)
    assert ".tmp" in src and "os.replace" in src, "artifacts must be written tmp-then-replace"
    assert "os.fsync" in src
    assert "sha256" in src
    assert "_smoke_test" in src, "artifacts must pass an inference smoke test before registration"
    assert "reload-verify" in src.lower() or "Reload-verify" in src


# ---------------------------------------------------------------------------
# END-TO-END: seed -> train -> candidate -> activate -> retrain -> rollback
# ---------------------------------------------------------------------------

async def _seed_training_rows(count_per_class=30):
    """Deterministic, learnable dataset over REAL identity pairs."""
    from db_connection import db_manager
    from sqlalchemy import text
    if not getattr(db_manager, "_initialized", False):
        await db_manager.init_db()
    inserted = []
    async with db_manager.get_session() as db:
        ids = [r[0] for r in await db.execute(
            text("SELECT id FROM identities WHERE status='ACTIVE' LIMIT 30"))]
        if len(ids) < 6:
            return None  # not enough identities to build pairs
        pairs = []
        for i in range(len(ids)):
            for j in range(i + 1, len(ids)):
                pairs.append((ids[i], ids[j]))
        pairs = pairs[: count_per_class * 2]
        for index, (a, b) in enumerate(pairs):
            approved = index % 2 == 0
            sim = 0.86 + (index % 7) * 0.015 if approved else 0.32 + (index % 7) * 0.02
            row = await db.execute(text("""
                INSERT INTO similarity_training_data
                    (identity_id_1, identity_id_2, embedding_similarity, pipeline_overlap,
                     quality_score_1, quality_score_2, appearances_diff, is_cross_pipeline,
                     label, created_at)
                VALUES (:a, :b, :sim, :overlap, 0.8, 0.8, 0.1, false, :label, now())
                RETURNING id
            """), {"a": str(a), "b": str(b), "sim": sim,
                   "overlap": 0.7 if approved else 0.2,
                   "label": 1.0 if approved else 0.0})
            inserted.append(row.scalar())
        await db.commit()
    return inserted


async def _delete_rows(ids):
    from db_connection import db_manager
    from sqlalchemy import text
    if not ids:
        return
    async with db_manager.get_session() as db:
        await db.execute(text("DELETE FROM similarity_training_data WHERE id = ANY(:ids)"),
                         {"ids": ids})
        await db.commit()


async def _cleanup_registry(job_ids):
    from db_connection import db_manager
    from sqlalchemy import text
    async with db_manager.get_session() as db:
        rows = await db.execute(text(
            "SELECT artifact_path FROM similarity_model_registry WHERE training_job_id = ANY(:jobs)"),
            {"jobs": job_ids})
        paths = [r[0] for r in rows]
        await db.execute(text(
            "DELETE FROM similarity_model_registry WHERE training_job_id = ANY(:jobs)"),
            {"jobs": job_ids})
        await db.commit()
    for p in paths:
        try:
            if p and os.path.exists(p):
                os.remove(p)
        except OSError:
            pass


def _wait_for_job(cookie, job_id, timeout_s=90):
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        status, task, _ = _http("GET",
                                f"/api/admin/merge-suggestions/training-jobs/{job_id}",
                                cookie=cookie)
        if status == 200 and task.get("status") in ("completed", "failed"):
            return task
        time.sleep(1)
    raise AssertionError(f"job {job_id} did not finish in {timeout_s}s")


def _run_training(cookie):
    status, body, _ = _http("POST", "/api/admin/merge-suggestions/training-jobs",
                            cookie=cookie, headers=CSRF, timeout=30)
    assert status == 202, f"expected 202 scheduled, got {status} {body}"
    assert body["accepted"] is True and body["job_id"].startswith("simtrain-")
    task = _wait_for_job(cookie, body["job_id"])
    assert task["status"] == "completed", f"training failed: {task.get('error_code')} {task.get('error_message')}"
    return body["job_id"], task["result"]


def test_full_model_lifecycle(cookie, token):
    """Seed -> train -> candidate (active untouched) -> activate -> retrain
    -> activate v2 (v1 archived) -> rollback to v1. Cleaned up afterwards."""
    seeded = run_async(_seed_training_rows())
    if seeded is None:
        pytest.skip("not enough identities in test database to build pairs")
    job_ids = []
    legacy_backup = None
    if os.path.exists(LEGACY_MODEL_PATH):
        legacy_backup = LEGACY_MODEL_PATH + ".pytest-bak"
        shutil.copyfile(LEGACY_MODEL_PATH, legacy_backup)
    try:
        # readiness must now pass
        status, st, _ = _http("GET", "/api/admin/merge-suggestions/model-status", token=token)
        assert st["ready_to_train"] is True, st["readiness_checks"]

        # --- train v1 ---
        job1, result1 = _run_training(cookie)
        job_ids.append(job1)
        assert result1["awaiting_approval"] is True
        assert result1["artifact_name"].startswith("similarity-model-v")
        assert result1["validation_metrics"]["sample_count"] >= 8
        assert result1["quality_gates"]["passed"] is True, result1["quality_gates"]
        assert result1["split"]["split_method"] == "identity_pair_grouped"
        model_id_1 = result1["model_id"]

        # candidate exists; nothing active yet was replaced
        status, st, _ = _http("GET", "/api/admin/merge-suggestions/model-status", token=token)
        assert st["candidate_model"]["id"] == model_id_1
        assert st["candidate_model"]["status"] == "candidate"

        # --- activate v1 ---
        status, act, _ = _http("POST",
                               f"/api/admin/merge-suggestions/models/{model_id_1}/activate?reason=pytest",
                               cookie=cookie, headers=CSRF)
        assert status == 200, act
        assert act["runtime_degraded"] is False, "runtime must reload the activated model"

        status, st, _ = _http("GET", "/api/admin/merge-suggestions/model-status", token=token)
        assert st["active_model"]["id"] == model_id_1
        assert st["runtime_loaded"] is True

        # re-activating a non-candidate is a controlled conflict
        status, body, _ = _http("POST",
                                f"/api/admin/merge-suggestions/models/{model_id_1}/activate",
                                cookie=cookie, headers=CSRF)
        assert status == 409 and body["detail"]["error_code"] == "INVALID_STATUS"

        # --- train + activate v2; v1 archives ---
        job2, result2 = _run_training(cookie)
        job_ids.append(job2)
        model_id_2 = result2["model_id"]
        assert result2["comparison"]["active_available"] is True, \
            "candidate must be compared against the active model"
        status, act2, _ = _http("POST",
                                f"/api/admin/merge-suggestions/models/{model_id_2}/activate?reason=pytest-v2",
                                cookie=cookie, headers=CSRF)
        assert status == 200, act2
        assert act2["previous_model_id"] == model_id_1

        status, models, _ = _http("GET", "/api/admin/merge-suggestions/models?limit=50", token=token)
        by_id = {m["id"]: m for m in models["items"]}
        assert by_id[model_id_2]["status"] == "active"
        assert by_id[model_id_1]["status"] == "archived", "previous active must be archived, not deleted"
        assert sum(1 for m in models["items"] if m["status"] == "active") == 1, \
            "exactly one active model per type"

        # --- rollback to v1 ---
        status, rb, _ = _http("POST",
                              f"/api/admin/merge-suggestions/models/{model_id_1}/rollback?reason=pytest-rollback",
                              cookie=cookie, headers=CSRF)
        assert status == 200, rb
        status, st, _ = _http("GET", "/api/admin/merge-suggestions/model-status", token=token)
        assert st["active_model"]["id"] == model_id_1, "rollback must restore the archived version"

    finally:
        run_async(_delete_rows(seeded))
        run_async(_cleanup_registry(job_ids))
        if legacy_backup:
            shutil.move(legacy_backup, LEGACY_MODEL_PATH)
        elif os.path.exists(LEGACY_MODEL_PATH):
            os.remove(LEGACY_MODEL_PATH)


def test_dataset_not_ready_is_structured(cookie, token):
    """With the synthetic rows cleaned up, a sparse dataset yields a
    structured DATASET_NOT_READY error — or, if this environment genuinely
    has enough real feedback, a scheduled job we immediately let finish."""
    status, st, _ = _http("GET", "/api/admin/merge-suggestions/model-status", token=token)
    if st["ready_to_train"]:
        pytest.skip("environment has real training data; not-ready path not testable here")
    status, body, _ = _http("POST", "/api/admin/merge-suggestions/training-jobs",
                            cookie=cookie, headers=CSRF)
    assert status == 400, body
    assert body["detail"]["error_code"] == "DATASET_NOT_READY"
    assert "readiness_checks" in body["detail"]


# ---------------------------------------------------------------------------
# Backend hygiene
# ---------------------------------------------------------------------------

def test_ml_section_no_raw_errors_and_audited():
    src = _read(IDENTITIES_ROUTES_PATH)
    ml_section = src.split("ML Similarity Model lifecycle")[1]
    assert "str(e)" not in ml_section.replace("_ml_safe_500", ""), \
        "ML endpoints must never leak raw exception text"
    for action in ("training_requested", "training_cancelled", "model_activated",
                   "candidate_rejected", "model_rolled_back"):
        assert f'_ml_audit("{action}"' in src, f"missing audit for {action}"
    assert "[MODEL_AUDIT]" in src


def test_page_injection_removed():
    src = _read(DASHBOARD_ROUTES_PATH)
    assert "backend-model-status" not in src, "page-injected JSON script tags must be gone"
    assert "window.modelStatusData" not in src
    assert "backend-config-settings" not in src


def test_active_model_never_overwritten_by_training():
    src = _read(SERVICE_PATH)
    assert "CANDIDATE_DIR" in src
    assert 'os.path.join(CANDIDATE_DIR' in src, "candidates get their own artifact directory"
    fn = src.split("async def run_training_job")[1].split("\nasync def ")[0]
    assert "SIMILARITY_MODEL_PATH" not in fn, "training must never write to the active model path"


# ---------------------------------------------------------------------------
# Frontend source contracts
# ---------------------------------------------------------------------------

def test_js_typed_normalizers():
    src = _read(JS_PATH)
    assert "function toBoolean" in src
    assert "=== 'false') return false" in src, "string 'false' must never be truthy"
    assert "function toFiniteNumber" in src and "function formatMetric" in src
    assert "'N/A'" in src, "missing metrics render N/A, never fake zeros"
    assert "|| false" not in src and "|| 0" not in src.replace("attempt || 0", ""), \
        "truthiness fallbacks destroy valid zero/false values"


def test_js_no_negative_sample_requirement():
    src = _read(JS_PATH)
    assert "Math.max(0, s.minSamples - s.trainingSamples)" in src
    assert "readinessReason" in src, "sample count must not be assumed the only readiness rule"


def test_js_zero_innerhtml_and_no_injected_globals():
    src = _read(JS_PATH)
    assert ".innerHTML" not in src
    assert "window.modelStatusData" not in src and "window.configSettingsData" not in src
    assert "replaceChildren" in src


def test_js_no_path_exposure():
    src = _read(JS_PATH)
    html = _read(HTML_PATH)
    assert "SIMILARITY_MODEL_PATH" not in src and "SIMILARITY_MODEL_PATH" not in html
    assert "model_path" not in src
    assert 'id="model-path"' not in html
    assert "artifact_name" in src, "clients see logical artifact names"


def test_js_no_arbitrary_refresh_delays():
    src = _read(JS_PATH)
    assert ", 1000)" not in src, "the 1s refresh guess must be gone"
    assert "pollTrainingJob" in src and "JOB_POLL_MAX" in src, \
        "refresh must be driven by real job state with bounded polling"


def test_js_request_hygiene():
    src = _read(JS_PATH)
    assert "AbortController" in src and "isCurrent()" in src
    assert "X-Requested-With" in src
    assert "cache: 'no-store'" in src
    assert "const DEBUG = false" in src
    assert "getElement" in src, "missing DOM elements must not crash the page"
    assert "alert(" not in src and "window.confirm" not in src


def test_html_contract():
    src = _read(HTML_PATH)
    for banned in ("onclick=", "onerror=", "onmouseover="):
        assert banned not in src
    assert "admin-ml-model.js?v=ml-2" in src
    assert 'id="readiness-body"' in src and 'id="lifecycle-body"' in src
    assert 'id="cancel-train-btn"' in src

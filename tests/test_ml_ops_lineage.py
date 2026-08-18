"""
ML-Ops API, one endpoint at a time, against the seeded demo lineage
(scripts/seed_ml_ops_demo.py --apply — the module seeds itself when the seed
is absent and removes what it seeded afterwards; a pre-existing seed is used
as-is and left in place).

For every /api/ml/* GET: status 200, the response keys, counts against SQL,
filters and pagination. Then the lineage the seed produced: predictions carry
threshold_id / threshold_version / event_time / outcome_*, drift reports name
their model, datasets carry lineage_summary, and the full chain
prediction → model → threshold set → snapshot → dataset → label reconstructs
with one SQL join.

    docker exec face_recognition_api python -m pytest tests/test_ml_ops_lineage.py -q
"""
import json
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request

import pytest
from sqlalchemy import text

from conftest import run_on_shared_loop as run_async

BASE = "http://localhost:8000"
SEED = "/app/scripts/seed_ml_ops_demo.py"


def _sql(statement, params=None, fetch="all"):
    from db_connection import db_manager

    async def _run():
        if not getattr(db_manager, "_initialized", False):
            await db_manager.init_db()
        async with db_manager.get_session() as db:
            res = await db.execute(text(statement), params or {})
            return res.scalar() if fetch == "scalar" else res.all()
    return run_async(_run())


def _seed_total():
    return _sql("SELECT (SELECT count(*) FROM pipelines WHERE pipeline_id LIKE 'seed-pipeline-%') + "
                "(SELECT count(*) FROM ml_models WHERE training_job_id LIKE 'seed-mlops-%')", fetch="scalar")


@pytest.fixture(scope="module", autouse=True)
def seeded():
    """Use the existing seed; otherwise seed now and remove at the end."""
    if _seed_total():
        yield "pre-existing"
        return
    proc = subprocess.run([sys.executable, SEED, "--apply", "--yes-i-understand"], cwd="/app",
                          capture_output=True, text=True, timeout=1500)
    assert proc.returncode == 0, proc.stdout[-3000:] + proc.stderr[-2000:]
    try:
        yield "seeded-by-test"
    finally:
        subprocess.run([sys.executable, SEED, "--remove", "--yes-i-understand"], cwd="/app",
                       capture_output=True, text=True, timeout=600)


@pytest.fixture(scope="module")
def token():
    req = urllib.request.Request(BASE + "/api/auth/login",
                                 data=json.dumps({"username": "admin", "password": "admin123"}).encode(),
                                 method="POST", headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())["access_token"]


def _get(token, path, **params):
    if params:
        path += ("&" if "?" in path else "?") + urllib.parse.urlencode(params)
    req = urllib.request.Request(BASE + path, headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")


def _seed_models():
    return [r[0] for r in _sql("SELECT id::text FROM ml_models WHERE training_job_id LIKE 'seed-mlops-%' ORDER BY version")]


# ---------------------------------------------------------------- endpoint by endpoint

def test_overview(token):
    status, body = _get(token, "/api/ml/overview")
    assert status == 200, body
    for key in ("mode", "models", "label_readiness", "data_readiness", "latest_drift_reports",
                "warnings", "optional_capabilities"):
        assert key in body, key
    assert body["models"]["shadow"] and body["models"]["shadow"]["stage"] == "shadow"
    assert body["models"]["shadow"]["id"] == _seed_models()[1]


def test_feature_definitions_match_the_table(token):
    status, body = _get(token, "/api/ml/features/definitions")
    assert status == 200 and body["total"] == len(body["items"])
    assert body["total"] == _sql("SELECT count(*) FROM ml_feature_definitions", fetch="scalar") == 24
    for item in body["items"]:
        for key in ("name", "version", "entity_type", "source", "computation", "leakage_class", "is_active"):
            assert key in item, (key, item)


def test_labels_list_stats_filters_and_pagination(token):
    status, body = _get(token, "/api/ml/labels", page_size=100, page=1)
    assert status == 200 and {"items", "total", "page", "page_size"} <= set(body)
    assert body["total"] == _sql("SELECT count(*) FROM ml_labels", fetch="scalar")
    status, seed = _get(token, "/api/ml/labels", label_kind="manual", page_size=100)
    assert status == 200 and all(i["label_kind"] == "manual" for i in seed["items"])
    assert seed["total"] == _sql("SELECT count(*) FROM ml_labels WHERE label_kind = 'manual'", fetch="scalar")
    status, reviewed = _get(token, "/api/ml/labels", review_status="reviewed", page_size=100)
    assert reviewed["total"] == _sql("SELECT count(*) FROM ml_labels WHERE review_status = 'reviewed'", fetch="scalar")
    status, p2 = _get(token, "/api/ml/labels", page_size=5, page=2)
    assert p2["page"] == 2 and len(p2["items"]) <= 5
    status, stats = _get(token, "/api/ml/labels/stats")
    assert status == 200 and {"counted_reviewed_manual", "not_counted", "required_total", "supervised_gate_open"} <= set(stats)
    counted = stats["counted_reviewed_manual"]
    assert counted["total"] == _sql("SELECT count(*) FROM ml_labels WHERE status = 'active' AND label_kind = 'manual' "
                                    "AND review_status = 'reviewed'", fetch="scalar")
    assert counted["total"] == counted["positive"] + counted["negative"] + counted["unknown"]
    superseded = _sql("SELECT count(*) FROM ml_labels WHERE status = 'superseded'", fetch="scalar")
    assert superseded >= 1, "the seed supersedes labels"
    chained = _sql("SELECT count(*) FROM ml_labels n JOIN ml_labels o ON o.id = n.supersedes_id "
                   "WHERE o.status = 'superseded' AND n.status = 'active'", fetch="scalar")
    assert chained == superseded


def test_datasets_carry_lineage_summary(token):
    status, body = _get(token, "/api/ml/datasets", page_size=100)
    assert status == 200 and body["total"] == _sql("SELECT count(*) FROM ml_datasets", fetch="scalar")
    built = [d for d in body["items"] if d["status"] == "built"]
    assert built, "the seed's trainings build datasets"
    for d in built:
        assert "lineage_summary" in d and d["lineage_summary"], d["name"]
        assert "storage_path" not in d, "storage_path is deliberately not a client field"
    failed = [d for d in body["items"] if d["status"] == "failed"]
    for d in failed:      # a refused build is a truthful record: quality report present, no lineage claimed
        assert d["quality_report"] and not d["lineage_summary"]


def test_models_list_detail_thresholds_and_stage_filter(token):
    ids = _seed_models()
    assert len(ids) == 2
    status, body = _get(token, "/api/ml/models", page_size=100)
    assert status == 200 and body["total"] == _sql("SELECT count(*) FROM ml_models", fetch="scalar")
    status, shadow = _get(token, "/api/ml/models", stage="shadow")
    assert status == 200 and [m["id"] for m in shadow["items"]] == [ids[1]], shadow
    status, archived = _get(token, "/api/ml/models", stage="archived", page_size=100)
    assert ids[0] in [m["id"] for m in archived["items"]]
    for mid, expect_status in ((ids[0], "retired"), (ids[1], "active")):
        status, detail = _get(token, f"/api/ml/models/{mid}")
        assert status == 200, detail
        sets = detail["thresholds"]
        assert len(sets) == 1 and sets[0]["status"] == expect_status, sets
        for key in ("id", "scope_type", "scope_id", "version", "status", "cutpoints", "quantiles", "sample_count",
                    "source", "created_at", "activated_at", "activated_by", "retired_at", "retired_by"):
            assert key in sets[0], key
        assert set(sets[0]["cutpoints"]) == {"elevated", "unusual", "highly_unusual"}
        assert "threshold" not in sets[0] and "objective" not in sets[0]
        # thresholds equal to the DB row
        row = _sql("SELECT status, version, source FROM ml_model_thresholds WHERE model_id = CAST(:m AS uuid)",
                   {"m": mid})[0]
        assert (sets[0]["status"], sets[0]["version"], sets[0]["source"]) == tuple(row)
    assert shadow["items"][0]["shadow_approval"]["approved_by"] == "seed-admin"


def test_training_job_detail(token):
    job = _sql("SELECT training_job_id FROM ml_models WHERE training_job_id LIKE 'seed-mlops-%' LIMIT 1", fetch="scalar")
    status, body = _get(token, f"/api/ml/training-jobs/{job}")
    assert status == 200, body
    assert body.get("status") == "completed" and body.get("result", {}).get("model_id")


def test_predictions_lineage_fields_filters_and_pagination(token):
    status, body = _get(token, "/api/ml/predictions", page_size=100)
    assert status == 200 and body["total"] == _sql("SELECT count(*) FROM ml_predictions", fetch="scalar")
    for key in ("threshold_id", "threshold_version", "event_time", "outcome_label_id", "outcome_label",
                "outcome_recorded_at", "model_id", "snapshot_id", "assessment_id"):
        assert key in body["items"][0], key
    ok = [p for p in body["items"] if p["actual_mode_used"] == "shadow" and not p["fallback_reason"]]
    assert ok, "seed produced successful shadow predictions"
    for p in ok:
        assert p["threshold_id"] and p["threshold_version"] and p["model_id"], p["id"]
        assert p["event_time"], "event_time is the assessment / sighting time, never NULL for the seed"
    with_outcome = [p for p in ok if p["outcome_label_id"]]
    assert with_outcome, "labels created after predictions link outcomes"
    for p in with_outcome:
        assert p["outcome_label"] in ("positive", "negative", "unknown") and p["outcome_recorded_at"]
    subject = ok[0]["subject_id"]
    status, one = _get(token, "/api/ml/predictions", subject_id=subject, page_size=100)
    assert status == 200 and all(p["subject_id"] == subject for p in one["items"])
    assert one["total"] == _sql("SELECT count(*) FROM ml_predictions WHERE subject_id = :s", {"s": subject}, fetch="scalar")
    status, fb = _get(token, "/api/ml/predictions", fallback_only="true", page_size=100)
    assert status == 200 and all(p["fallback_reason"] for p in fb["items"])
    assert fb["total"] == _sql("SELECT count(*) FROM ml_predictions WHERE fallback_reason IS NOT NULL", fetch="scalar")
    status, p3 = _get(token, "/api/ml/predictions", page=3, page_size=10)
    assert p3["page"] == 3 and len(p3["items"]) == 10


def test_shadow_summary(token):
    status, body = _get(token, "/api/ml/shadow/summary", days=30)
    assert status == 200 and {"comparisons", "window_days", "note"} <= set(body), body
    assert body["window_days"] == 30


def test_drift_reports_name_their_model_and_filter(token):
    ids = _seed_models()
    status, body = _get(token, "/api/ml/drift/reports", page_size=100)
    assert status == 200 and body["total"] == _sql("SELECT count(*) FROM ml_drift_reports", fetch="scalar")
    assert body["total"] >= 2 and all(r["model_id"] for r in body["items"])
    assert all(r["scope_type"] == "global" and r["scope_id"] == "" for r in body["items"])
    status, one = _get(token, "/api/ml/drift/reports", model_id=ids[1])
    assert status == 200 and {r["model_id"] for r in one["items"]} == {ids[1]}
    assert {r["report_kind"] for r in one["items"]} == {"data_drift", "prediction_drift"}
    status, bad = _get(token, "/api/ml/drift/reports", model_id="not-a-uuid")
    assert status == 422


def test_retraining_policy_and_audit(token):
    status, body = _get(token, "/api/ml/retraining-policy/behavior_anomaly_model")
    assert status == 200 and body["model_type"] == "behavior_anomaly_model" and "enabled" in body
    status, audit = _get(token, "/api/ml/audit", page_size=100)
    assert status == 200 and {"items", "total"} <= set(audit)
    actions = {a["action"] for a in audit["items"]}
    assert {"threshold_activate", "threshold_retire", "label_create", "label_supersede"} <= actions, actions


# ---------------------------------------------------------------- lineage chain

def test_full_lineage_chain_reconstructs_with_one_join():
    row = _sql("""
        SELECT count(*) AS n,
               count(*) FILTER (WHERE m.id IS NULL)  AS no_model,
               count(*) FILTER (WHERE t.id IS NULL)  AS no_threshold,
               count(*) FILTER (WHERE s.id IS NULL)  AS no_snapshot,
               count(*) FILTER (WHERE d.id IS NULL)  AS no_dataset,
               count(*) FILTER (WHERE l.id IS NOT NULL) AS with_label
        FROM ml_predictions p
        JOIN ml_models m            ON m.id = p.model_id
        JOIN ml_model_thresholds t  ON t.id = p.threshold_id AND t.model_id = m.id
        LEFT JOIN ml_feature_snapshots s ON s.id = p.snapshot_id
        LEFT JOIN ml_datasets d      ON d.id = m.dataset_id
        LEFT JOIN ml_labels l        ON l.id = p.outcome_label_id
        WHERE p.actual_mode_used = 'shadow' AND p.fallback_reason IS NULL
          AND p.subject_id IN (SELECT id::text FROM identities WHERE display_name LIKE 'seed_person_%')
    """)[0]
    n, no_model, no_threshold, no_snapshot, no_dataset, with_label = row
    assert n >= 200, n
    assert (no_model, no_threshold, no_snapshot, no_dataset) == (0, 0, 0, 0), row
    assert with_label >= 20, with_label
    # every prediction's threshold_version equals its set's label
    mismatch = _sql("SELECT count(*) FROM ml_predictions p JOIN ml_model_thresholds t ON t.id = p.threshold_id "
                    "WHERE p.threshold_version <> t.scope_type || ':' || CASE WHEN t.scope_id = '' THEN 'global' "
                    "ELSE t.scope_id END || '@v' || t.version", fetch="scalar")
    assert mismatch == 0
    # v1 predictions keep pointing at v1's (now retired) set: history is not rewritten
    assert _sql("SELECT count(*) FROM ml_predictions p JOIN ml_model_thresholds t ON t.id = p.threshold_id "
                "WHERE t.status = 'retired'", fetch="scalar") >= 100


def test_data_quality_invariants():
    checks = {
        "shadow success without lineage": "SELECT count(*) FROM ml_predictions WHERE actual_mode_used = 'shadow' "
                                          "AND fallback_reason IS NULL AND (threshold_id IS NULL OR threshold_version IS NULL "
                                          "OR model_id IS NULL)",
        "drift without model": "SELECT count(*) FROM ml_drift_reports WHERE model_id IS NULL",
        "empty snapshots": "SELECT count(*) FROM ml_feature_snapshots WHERE features = '{}'::jsonb",
        "two active sets": "SELECT count(*) FROM (SELECT 1 FROM ml_model_thresholds WHERE status = 'active' "
                           "GROUP BY model_id, scope_type, scope_id HAVING count(*) > 1) x",
        "outcome to ineligible label": "SELECT count(*) FROM ml_predictions p JOIN ml_labels l ON l.id = p.outcome_label_id "
                                       "WHERE l.status <> 'active' OR l.review_status = 'disputed'",
        "shadow model without active set": "SELECT count(*) FROM ml_models m WHERE m.stage = 'shadow' AND NOT EXISTS "
                                           "(SELECT 1 FROM ml_model_thresholds t WHERE t.model_id = m.id AND t.status = 'active')",
        "non-shadow model with active set": "SELECT count(*) FROM ml_model_thresholds t JOIN ml_models m ON m.id = t.model_id "
                                            "WHERE t.status = 'active' AND m.stage <> 'shadow'",
    }
    bad = {k: _sql(v, fetch="scalar") for k, v in checks.items()}
    assert all(v == 0 for v in bad.values()), bad

"""
Reusable ML dataset + training pipeline — contract tests.

What is pinned here (every test is self-seeding through the REAL collector
and self-cleaning):

  * typed dataset definitions; no invented scientific minimum (REQUIRES_VALIDATION)
  * extraction is explicit and auditable: time range honoured, candidate /
    selected / excluded counts recorded, cap exceeded -> refusal or a
    DECLARED sampling policy, never a silent drop
  * same definition + same range -> same logical fingerprint AND same
    Parquet bytes hash; different range -> different lineage
  * sidecar manifest matches the registry row, carries the feature-set
    limitations, and exposes no path / secret
  * validator refuses NaN / Inf / non-numeric values
  * one immutable dataset trains several experiments; each model records the
    exact dataset id + both hashes, its complete training_config and the
    code revision
  * a tampered Parquet (bytes) or an unverifiable legacy dataset is refused
    with a stable code; the dataset row, the current shadow model and the
    active threshold sets are untouched by a failed training
  * training and inference preprocess a raw feature vector identically; an
    artifact without a median for a scored feature is refused
  * candidate-vs-incumbent comparison is task-aware (NOT_APPLICABLE for
    unlike models) and never a verdict
  * serializers stay path-free and report file presence; legacy rows are
    labelled with the legacy extraction policy at read time, never rewritten
  * CLI and HTTP entry points answer with the same stable codes
"""

import json
import math
import os
import pickle
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid as uuid_mod
from datetime import datetime, timedelta

import pytest

from backend.ml.constants import FEATURE_SET_VERSION
from conftest import run_on_shared_loop as run_async
from test_ml_training_registry import _seed_snapshot_corpus

PREFIX = "pytest-mlp-"
BASE = "http://localhost:8000"


async def _ensure_db():
    from db_connection import db_manager
    if not getattr(db_manager, "_initialized", False):
        await db_manager.init_db()


def _sql(statement, params=None, fetch="all"):
    async def _run():
        from db_connection import db_manager
        from sqlalchemy import text as sa_text
        await _ensure_db()
        async with db_manager.get_session() as db:
            res = await db.execute(sa_text(statement), params or {})
            if statement.lstrip().upper().startswith("SELECT"):
                return res.scalar() if fetch == "scalar" else res.all()
            await db.commit()
            return None
    return run_async(_run())


def _cleanup():
    async def _run():
        from db_connection import db_manager
        from sqlalchemy import text as sa_text
        await _ensure_db()
        async with db_manager.get_session() as db:
            for path in (await db.execute(sa_text(
                    "SELECT artifact_path FROM ml_models WHERE training_job_id LIKE :p"),
                    {"p": PREFIX + "%"})).scalars().all():
                if path and os.path.exists(path):
                    os.remove(path)
            for table in ("ml_shadow_comparisons", "ml_predictions"):
                await db.execute(sa_text(
                    f"DELETE FROM {table} WHERE model_id IN "
                    "(SELECT id FROM ml_models WHERE training_job_id LIKE :p)"), {"p": PREFIX + "%"})
            await db.execute(sa_text("DELETE FROM ml_models WHERE training_job_id LIKE :p"),
                             {"p": PREFIX + "%"})
            rows = (await db.execute(sa_text(
                "SELECT storage_path, manifest_path FROM ml_datasets WHERE name LIKE :p"),
                {"p": PREFIX + "%"})).all()
            for storage, manifest in rows:
                for path in (storage, manifest):
                    if path and os.path.exists(path):
                        os.remove(path)
            await db.execute(sa_text("DELETE FROM ml_datasets WHERE name LIKE :p"), {"p": PREFIX + "%"})
            await db.execute(sa_text(
                "DELETE FROM ml_feature_snapshots WHERE entity_id IN "
                "(SELECT id::text FROM identities WHERE display_name LIKE :p)"), {"p": PREFIX + "%"})
            await db.execute(sa_text(
                "DELETE FROM identity_appearances WHERE identity_id IN "
                "(SELECT id FROM identities WHERE display_name LIKE :p)"), {"p": PREFIX + "%"})
            await db.execute(sa_text("DELETE FROM identities WHERE display_name LIKE :p"),
                             {"p": PREFIX + "%"})
            await db.execute(sa_text(
                "DELETE FROM background_task_history WHERE job_id LIKE :p"), {"p": PREFIX + "%"})
            await db.commit()
    run_async(_run())


@pytest.fixture(scope="module", autouse=True)
def corpus():
    _cleanup()

    async def _seed():
        from db_connection import db_manager
        await _ensure_db()
        async with db_manager.get_session() as db:
            await _seed_snapshot_corpus(db, PREFIX, PREFIX + "cam", count=24)
    run_async(_seed())
    yield
    _cleanup()


def _build(name, **kwargs):
    async def _run():
        from db_connection import db_manager
        from backend.ml.dataset_builder import build_dataset
        await _ensure_db()
        async with db_manager.get_session() as db:
            return await build_dataset(db, name=name, kind=kwargs.pop("kind", "unsupervised"),
                                       build_job_id=PREFIX + "build", **kwargs)
    return run_async(_run())


def _dataset_row(dataset_id):
    async def _run():
        from db_connection import db_manager
        from db_models import MLDataset
        from sqlalchemy import select
        await _ensure_db()
        async with db_manager.get_session() as db:
            return (await db.execute(
                select(MLDataset).where(MLDataset.id == uuid_mod.UUID(dataset_id)))).scalar_one()
    return run_async(_run())


def _train(dataset_id=None, algorithm="isolation_forest", seed=None, hyperparameters=None):
    async def _run():
        from db_connection import db_manager
        from backend.core.task_history import task_history_manager
        from backend.ml import trainer
        await _ensure_db()
        job_id = PREFIX + uuid_mod.uuid4().hex[:8]
        assert trainer.try_acquire_training(job_id) is None
        await task_history_manager.create_job(
            job_id=job_id, task_type="ml_training", task_name="pytest pipeline", description="pytest")
        await trainer.run_training_job(job_id, algorithm=algorithm, dataset_id=dataset_id,
                                       seed=seed, hyperparameters=hyperparameters)
        return await task_history_manager.get_task_by_job_id(job_id)
    return run_async(_run())


@pytest.fixture(scope="module")
def built():
    out = _build(PREFIX + "base")
    assert out["status"] == "built", out
    return out


# ---------------------------------------------------------------------------
# Definitions
# ---------------------------------------------------------------------------

def test_definitions_are_typed_and_invent_no_minimum():
    from backend.ml.dataset_definitions import (
        DatasetDefinition, get_definition, list_definitions, default_definition_for_kind)
    names = {d.key for d in list_definitions()}
    assert {"behavior_anomaly_person@v1", "behavior_anomaly_person@v2", "behavior_anomaly_person_labeled@v2"} <= names
    for d in list_definitions():
        assert d.min_rows is None, "no scientific minimum is invented"
        assert d.to_manifest()["scientific_minimum"] == "REQUIRES_VALIDATION"
        assert d.sampling_policy == "refuse", "silence is never the default"
    assert default_definition_for_kind("supervised").kind == "supervised"
    assert get_definition("behavior_anomaly_person").version == "v3"   # latest: trailing-90-day window
    assert get_definition("behavior_anomaly_person", "v2").trailing_days is None
    assert get_definition("behavior_anomaly_person", "v3").trailing_days == 90
    assert get_definition("behavior_anomaly_person", "v1").feature_set_version == "secintel-features-v1"
    with pytest.raises(KeyError):
        get_definition("no_such_dataset")
    with pytest.raises(ValueError):
        DatasetDefinition(name="x", version="v1", purpose="p", entity_type="person",
                          kind="unsupervised", sampling_policy="random")


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------

def test_extraction_is_recorded_and_time_range_honoured(built):
    ex = built["extraction"]
    assert ex["policy_version"] == "explicit-cap-v1"
    assert ex["candidate_rows"] == ex["selected_rows"] == built["row_count"]
    assert ex["excluded_rows"] == 0 and ex["sampling_policy"] == "refuse"
    assert ex["time_range"]["source"] == "whole_history"
    assert built["parquet_sha256"] and built["checksum"] != "empty"

    row = _dataset_row(built["dataset_id"])
    mid = row.time_range_start + (row.time_range_end - row.time_range_start) / 2
    partial = _build(PREFIX + "range", time_range_start=row.time_range_start, time_range_end=mid)
    assert partial["status"] == "built", partial
    assert 0 < partial["row_count"] < built["row_count"]
    assert partial["extraction"]["time_range"]["source"] == "explicit"
    assert partial["extraction"]["candidate_rows"] == partial["row_count"]
    assert partial["checksum"] != built["checksum"], "a different range is a different lineage"
    assert partial["parquet_sha256"] != built["parquet_sha256"]
    prow = _dataset_row(partial["dataset_id"])
    assert prow.time_range_end < mid and prow.source_cutoff == mid


def test_cap_exceeded_refuses_or_samples_by_declared_policy(built):
    from backend.ml.dataset_definitions import DatasetDefinition
    cap = 12   # above the validator floor (MIN_ROWS_UNSUPERVISED=10), below the corpus
    tiny = DatasetDefinition(name="behavior_anomaly_person", version="v1-cap-test",
                             purpose="cap drill", entity_type="person", kind="unsupervised",
                             row_cap=cap)
    refused = _build(PREFIX + "cap-refuse", definition=tiny)
    assert refused["status"] == "failed" and refused["refusal"] == "EXTRACTION_EXCEEDS_CAP"
    ex = refused["extraction"]
    assert ex["candidate_rows"] == built["row_count"] and ex["excluded_rows"] == ex["candidate_rows"]
    frow = _dataset_row(refused["dataset_id"])
    assert frow.status == "failed" and frow.extraction["refused"] == "EXTRACTION_EXCEEDS_CAP"
    assert frow.quality_report["failed_checks"] == ["extraction_cap"]
    assert not frow.lineage_summary and frow.storage_path is None

    newest = _build(PREFIX + "cap-newest", definition=tiny, sampling_policy="newest_first")
    oldest = _build(PREFIX + "cap-oldest", definition=tiny, sampling_policy="oldest_first")
    for out, policy in ((newest, "newest_first"), (oldest, "oldest_first")):
        assert out["status"] == "built", out
        assert out["row_count"] == cap and out["extraction"]["selected_rows"] == cap
        assert out["extraction"]["excluded_rows"] == built["row_count"] - cap
        assert out["extraction"]["sampling_policy"] == policy
    base_row, n_row, o_row = (_dataset_row(x["dataset_id"]) for x in (built, newest, oldest))
    assert o_row.time_range_start == base_row.time_range_start, "oldest_first keeps the oldest rows"
    assert n_row.time_range_end == base_row.time_range_end, "newest_first keeps the newest rows"
    assert n_row.time_range_start > o_row.time_range_end


def test_same_definition_same_range_reproduces_both_hashes(built):
    again = _build(PREFIX + "base")
    assert again["status"] == "built" and again["version"] == built["version"] + 1
    assert again["checksum"] == built["checksum"], "logical fingerprint is reproducible"
    assert again["parquet_sha256"] == built["parquet_sha256"], "Parquet bytes are reproducible"


# ---------------------------------------------------------------------------
# Manifest + validator
# ---------------------------------------------------------------------------

def test_manifest_matches_registry_row_and_is_secret_free(built):
    from backend.ml.dataset_builder import read_manifest
    row = _dataset_row(built["dataset_id"])
    manifest = read_manifest(row)
    assert manifest and manifest["dataset_id"] == str(row.id)
    assert manifest["checksum"] == row.checksum and manifest["parquet_sha256"] == row.parquet_sha256
    assert manifest["row_count"] == row.row_count and manifest["split"] == row.split_config
    assert manifest["definition"]["name"] == row.definition_name == "behavior_anomaly_person"
    assert manifest["definition"]["scientific_minimum"] == "REQUIRES_VALIDATION"
    assert manifest["extraction"] == row.extraction
    from backend.ml.dataset_definitions import feature_set_limitations
    limits = {item["feature"] for item in manifest["feature_set_limitations"]}
    assert limits == {item["feature"] for item in feature_set_limitations(FEATURE_SET_VERSION)}
    assert limits, "the current feature set states its limitations"
    assert "policy_version" in manifest["comparability"]
    blob = json.dumps(manifest).lower()
    for banned in ("storage_path", "manifest_path", "/app/", "models/ml", "password",
                   "postgres://", "secret", "token"):
        assert banned not in blob, banned
    assert row.quality_report["feature_set_limitations"], "quality report states the limitations too"


def test_validator_refuses_nan_inf_and_non_numeric():
    from backend.ml.data_validator import validate_rows
    now = datetime.utcnow()
    defs = [{"name": "a", "leakage_class": "safe"}, {"name": "b", "leakage_class": "safe"}]

    def rows(value):
        return [{"entity_id": f"e{i}", "as_of": now - timedelta(hours=i + 1),
                 "features": {"a": float(i), "b": value if i == 0 else 1.0}}
                for i in range(12)]
    nan = validate_rows(rows(float("nan")), kind="unsupervised", definitions=defs)
    assert nan["checks"]["no_nan_inf"]["passed"] is False and nan["passed"] is False
    inf = validate_rows(rows(float("inf")), kind="unsupervised", definitions=defs)
    assert inf["checks"]["no_nan_inf"]["passed"] is False
    text = validate_rows(rows("1.0"), kind="unsupervised", definitions=defs)
    assert text["checks"]["feature_dtype_numeric"]["passed"] is False
    clean = validate_rows(rows(2.0), kind="unsupervised", definitions=defs)
    assert clean["checks"]["no_nan_inf"]["passed"] and clean["checks"]["timestamp_parseable"]["passed"]


# ---------------------------------------------------------------------------
# Training from an existing dataset
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def two_experiments(built):
    a = _train(built["dataset_id"], "isolation_forest", seed=7, hyperparameters={"n_estimators": 50})
    b = _train(built["dataset_id"], "mad_baseline")
    assert a["status"] == "completed", a
    assert b["status"] == "completed", b
    return a["result"], b["result"]


def test_one_dataset_many_experiments_each_with_full_lineage(built, two_experiments):
    a, b = two_experiments
    for res in (a, b):
        assert res["dataset_id"] == built["dataset_id"] and res["dataset_reused"] is True
        cfg = res["training_config"]
        assert cfg["dataset_checksum"] == built["checksum"]
        assert cfg["dataset_parquet_sha256"] == built["parquet_sha256"]
        assert cfg["feature_set_version"] == FEATURE_SET_VERSION
        assert res["code_version"], "the code revision is recorded in the container"
        assert res["evaluation"]["incumbent_comparison"]["promotion_decision"] == "REQUIRES_VALIDATION"
        assert res["evaluation"]["feature_set_limitations"]
    assert a["training_config"]["seed"] == 7 and b["training_config"]["seed"] == 42
    assert a["training_config"]["hyperparameters"] == {
        "n_estimators": 50, "contamination": "auto", "max_samples": "auto"}
    assert b["training_config"]["hyperparameters"] == {}
    assert a["model_id"] != b["model_id"]
    rows = _sql("SELECT training_config->>'seed', hyperparameters, code_version FROM ml_models "
                "WHERE id IN (:a, :b) ORDER BY version", {"a": a["model_id"], "b": b["model_id"]})
    assert [r[0] for r in rows] == ["7", "42"] and all(r[2] for r in rows)


def test_unknown_hyperparameter_is_refused_not_ignored(built):
    task = _train(built["dataset_id"], "isolation_forest", hyperparameters={"depth": 3})
    assert task["status"] == "failed" and task["error_code"] == "UNKNOWN_HYPERPARAMETER"


def test_tampered_parquet_is_refused_and_nothing_else_changes(built):
    row = _dataset_row(built["dataset_id"])
    before = {
        "models": _sql("SELECT count(*) FROM ml_models WHERE dataset_id = :d",
                       {"d": built["dataset_id"]}, fetch="scalar"),
        "shadow": _sql("SELECT id::text FROM ml_models WHERE stage = 'shadow' ORDER BY id"),
        "active_thresholds": _sql("SELECT id::text FROM ml_model_thresholds WHERE status = 'active' ORDER BY id"),
        "dataset": (row.status, row.checksum, row.parquet_sha256, row.row_count),
    }
    with open(row.storage_path, "rb") as f:
        original = f.read()
    try:
        with open(row.storage_path, "r+b") as f:
            f.seek(100)
            byte = f.read(1)
            f.seek(100)
            f.write(bytes([byte[0] ^ 0xFF]))
        task = _train(built["dataset_id"])
        assert task["status"] == "failed" and task["error_code"] == "DATASET_FILE_HASH_MISMATCH", task
    finally:
        with open(row.storage_path, "wb") as f:
            f.write(original)
    after_row = _dataset_row(built["dataset_id"])
    assert (after_row.status, after_row.checksum, after_row.parquet_sha256, after_row.row_count) == before["dataset"]
    assert _sql("SELECT count(*) FROM ml_models WHERE dataset_id = :d",
                {"d": built["dataset_id"]}, fetch="scalar") == before["models"]
    assert _sql("SELECT id::text FROM ml_models WHERE stage = 'shadow' ORDER BY id") == before["shadow"]
    assert _sql("SELECT id::text FROM ml_model_thresholds WHERE status = 'active' ORDER BY id") == before["active_thresholds"]
    # and the restored file trains again
    task = _train(built["dataset_id"], "mad_baseline")
    assert task["status"] == "completed", task


def test_legacy_dataset_without_file_hash_is_unverifiable(built):
    _sql("UPDATE ml_datasets SET parquet_sha256 = NULL WHERE id = :d", {"d": built["dataset_id"]})
    try:
        task = _train(built["dataset_id"])
        assert task["status"] == "failed" and task["error_code"] == "DATASET_INTEGRITY_UNVERIFIABLE"
    finally:
        _sql("UPDATE ml_datasets SET parquet_sha256 = :h WHERE id = :d",
             {"h": built["parquet_sha256"], "d": built["dataset_id"]})
    missing = _train(str(uuid_mod.uuid4()))
    assert missing["error_code"] == "DATASET_NOT_FOUND"


# ---------------------------------------------------------------------------
# Train/serve preprocessing contract
# ---------------------------------------------------------------------------

def test_training_and_inference_preprocess_identically(two_experiments):
    from backend.ml.registry_service import (
        RegistryError, preprocess_feature_vector, validate_artifact)
    from config import settings
    model_id = two_experiments[0]["model_id"]
    path, h, names, deps = _sql(
        "SELECT artifact_path, artifact_hash, feature_names, dependency_versions FROM ml_models WHERE id = :m",
        {"m": model_id})[0]
    payload = validate_artifact(path, expected_hash=h, expected_feature_names=list(names),
                                expected_dependencies=deps)
    medians = payload["imputation_medians"]
    assert set(payload["feature_names"]) <= set(medians), "every scored feature has a train median"
    raw = {name: float(i) for i, name in enumerate(payload["feature_names"])}
    dropped = payload["feature_names"][:2]
    for name in dropped:
        raw.pop(name)
    # the trainer's matrix rule (registry contract) and the inference rule are the SAME function
    vector, missing = preprocess_feature_vector(payload, raw)
    expected = [raw[n] if n in raw else medians[n] for n in payload["feature_names"]]
    assert vector == expected and missing == dropped
    assert all(math.isfinite(v) for v in vector)

    # an artifact missing a median is refused before it could ever be served
    broken = dict(payload)
    broken["imputation_medians"] = {k: v for k, v in medians.items() if k != payload["feature_names"][0]}
    bad_path = os.path.join(str(settings.ML_ARTIFACT_DIR), "candidates", "pytest-mlp-no-median.pkl")
    with open(bad_path, "wb") as f:
        pickle.dump(broken, f)
    try:
        import hashlib
        bad_hash = hashlib.sha256(open(bad_path, "rb").read()).hexdigest()
        with pytest.raises(RegistryError) as exc:
            validate_artifact(bad_path, expected_hash=bad_hash,
                              expected_feature_names=list(names), expected_dependencies=deps)
        assert exc.value.code == "ARTIFACT_IMPUTATION_INCOMPLETE"
    finally:
        os.remove(bad_path)


# ---------------------------------------------------------------------------
# Task-aware comparison
# ---------------------------------------------------------------------------

def test_incumbent_comparison_is_task_aware():
    from backend.ml.evaluation import compare_with_incumbent

    class Row:
        id = uuid_mod.uuid4(); version = 9; algorithm = "isolation_forest"
        artifact_size_bytes = 1; model_type = "behavior_anomaly_model"
        model_purpose = "behavioral_anomaly_detection"; score_type = "threat_probability"
        feature_set_version = FEATURE_SET_VERSION
    meta = {"model_type": "behavior_anomaly_model", "model_purpose": "behavioral_anomaly_detection",
            "score_type": "anomaly_score", "feature_set_version": FEATURE_SET_VERSION}
    out = compare_with_incumbent(candidate_payload={"feature_names": []}, candidate_meta=meta,
                                 incumbent_row=Row(), incumbent_payload={"feature_names": []},
                                 rows_by_split={"val": []})
    assert out["status"] == "NOT_APPLICABLE" and "score_type" in out["incompatible_fields"]
    assert out["promotion_decision"] == "REQUIRES_VALIDATION"
    none = compare_with_incumbent(candidate_payload={"feature_names": []}, candidate_meta=meta,
                                  incumbent_row=None, incumbent_payload=None, rows_by_split={})
    assert none["status"] == "NOT_APPLICABLE"


# ---------------------------------------------------------------------------
# Serialization + legacy labelling
# ---------------------------------------------------------------------------

def test_serializers_path_free_presence_and_legacy_policy(built):
    from backend.ml.dataset_builder import extraction_for, serialize_dataset
    row = _dataset_row(built["dataset_id"])
    out = serialize_dataset(row)
    assert "storage_path" not in out and "manifest_path" not in out
    assert out["file_present"] is True and out["has_manifest"] is True
    assert out["extraction"]["policy_version"] == "explicit-cap-v1"
    assert out["parquet_sha256"] == built["parquet_sha256"]

    class Legacy:
        extraction = None; row_count = 123
    legacy = extraction_for(Legacy())
    assert legacy["policy_version"] == "legacy-oldest-first-cap-v0"
    assert legacy["excluded_rows"] is None and legacy["selected_rows"] == 123
    assert _sql("SELECT extraction FROM ml_datasets WHERE id = :d", {"d": built["dataset_id"]},
                fetch="scalar")["policy_version"] == "explicit-cap-v1", "stored, not rewritten"


# ---------------------------------------------------------------------------
# CLI + HTTP
# ---------------------------------------------------------------------------

def _cli(*args):
    proc = subprocess.run([sys.executable, "-m", "backend.ml.pipeline", *args],
                          capture_output=True, text=True, cwd="/app", timeout=600)
    return proc.returncode, (json.loads(proc.stdout) if proc.stdout.strip() else {})


def test_cli_entry_points_and_exit_codes(built, two_experiments):
    code, out = _cli("list-definitions")
    assert code == 0 and out["status"] == "ok" and len(out["definitions"]) >= 2
    code, out = _cli("train", "--dataset-id", str(uuid_mod.uuid4()))
    assert code == 2 and out["status"] == "failed" and out["code"] == "DATASET_NOT_FOUND"
    code, out = _cli("describe-dataset", "--dataset-id", built["dataset_id"])
    assert code == 0 and out["manifest"]["checksum"] == built["checksum"]
    assert "storage_path" not in json.dumps(out)
    code, out = _cli("lineage", "--model-id", two_experiments[0]["model_id"])
    assert code == 0 and out["dataset"]["id"] == built["dataset_id"]
    assert out["training_run"]["status"] == "completed"
    assert out["model"]["training_config"]["seed"] == 7
    assert out["feature_set"]["limitations"]
    code, out = _cli("evaluate", "--model-id", two_experiments[0]["model_id"])
    assert code == 0 and out["incumbent_comparison"]["promotion_decision"] == "REQUIRES_VALIDATION"


def _http(method, path, token=None, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method,
                                 headers={"Content-Type": "application/json",
                                          "X-Requested-With": "XMLHttpRequest"})
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def _wait_ml_job(job_id, token, timeout=120):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status, body = _http("GET", f"/api/ml/jobs/{job_id}", token)
        assert status == 200, body
        if body.get("status") in ("completed", "failed", "cancelled"):
            return body
        time.sleep(0.25)
    raise AssertionError(f"ML job {job_id} did not finish within {timeout}s")


@pytest.fixture(scope="module")
def token():
    req = urllib.request.Request(BASE + "/api/auth/login",
                                 data=json.dumps({"username": "admin", "password": "admin123"}).encode(),
                                 method="POST", headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())["access_token"]


def test_http_definitions_detail_and_validation(token, built, two_experiments):
    status, body = _http("GET", "/api/ml/datasets/definitions", token)
    assert status == 200 and body["total"] >= 2
    assert all(d["scientific_minimum"] == "REQUIRES_VALIDATION" for d in body["items"])
    assert all(d["feature_set_limitations"] for d in body["items"])

    status, body = _http("GET", f"/api/ml/datasets/{built['dataset_id']}", token)
    assert status == 200, body
    assert body["immutable"] is True and len(body["models"]) >= 2
    assert body["extraction"]["policy_version"] == "explicit-cap-v1"
    assert body["parquet_sha256"] == built["parquet_sha256"]
    assert body["manifest"]["definition"]["name"] == "behavior_anomaly_person"
    blob = json.dumps(body)
    assert "storage_path" not in blob and "manifest_path" not in blob and "/app/" not in blob

    status, body = _http("GET", f"/api/ml/datasets/{uuid_mod.uuid4()}", token)
    assert status == 404 and body["detail"]["error_code"] == "DATASET_NOT_FOUND"
    status, body = _http("POST", "/api/ml/datasets", token,
                         {"name": PREFIX + "api", "kind": "supervised",
                          "definition": "behavior_anomaly_person"})
    assert status == 422 and body["detail"]["error_code"] == "DATASET_DEFINITION_KIND_MISMATCH"
    status, body = _http("POST", "/api/ml/datasets", token,
                         {"name": PREFIX + "api", "kind": "unsupervised",
                          "time_range_start": "2030-01-01T00:00:00Z",
                          "time_range_end": "2029-01-01T00:00:00Z"})
    assert status == 422 and body["detail"]["error_code"] == "INVALID_TIME_RANGE"

    status, body = _http("GET", f"/api/ml/models/{two_experiments[0]['model_id']}", token)
    assert status == 200 and body["training_config"]["dataset_id"] == built["dataset_id"]
    assert body["artifact_present"] is True and body["code_version"]


# ---------------------------------------------------------------------------
# Legacy backfill + explicit archive
# ---------------------------------------------------------------------------

def test_legacy_backfill_records_hash_only_when_rows_reproduce_checksum(token):
    """Simulate legacy rows: strip parquet_sha256/manifest from two fresh
    builds, corrupt one, backfill. The intact one gains its file hash and a
    legacy-policy manifest; the corrupted one stays unverifiable; nothing
    else on either row changes."""
    from backend.ml.dataset_builder import read_manifest
    good = _build(PREFIX + "legacy-good")
    bad = _build(PREFIX + "legacy-bad")
    for out in (good, bad):
        assert out["status"] == "built"
        row = _dataset_row(out["dataset_id"])
        if row.manifest_path and os.path.exists(row.manifest_path):
            os.remove(row.manifest_path)
        _sql("UPDATE ml_datasets SET parquet_sha256 = NULL, manifest_path = NULL, extraction = NULL, "
             "definition_name = NULL, definition_version = NULL WHERE id = :d", {"d": out["dataset_id"]})
    bad_row = _dataset_row(bad["dataset_id"])
    with open(bad_row.storage_path, "r+b") as f:
        f.seek(100); byte = f.read(1); f.seek(100); f.write(bytes([byte[0] ^ 0xFF]))

    status, scheduled = _http("POST", "/api/ml/datasets/backfill-hashes", token, {})
    assert status == 202, scheduled
    task = _wait_ml_job(scheduled["job_id"], token)
    assert task["status"] == "completed", task
    report = task["result"]
    verified = {v["dataset_id"]: v for v in report["verified"]}
    unverifiable = {u["dataset_id"]: u for u in report["unverifiable"]}
    assert good["dataset_id"] in verified and bad["dataset_id"] in unverifiable
    assert unverifiable[bad["dataset_id"]]["reason"] in ("DATASET_CHECKSUM_MISMATCH", "DATASET_FILE_UNREADABLE")

    good_row = _dataset_row(good["dataset_id"])
    assert good_row.parquet_sha256 == good["parquet_sha256"], "same bytes -> same hash as the original build"
    assert good_row.checksum == good["checksum"] and good_row.extraction is None, "lineage not rewritten"
    manifest = read_manifest(good_row)
    assert manifest["extraction"]["policy_version"] == "legacy-oldest-first-cap-v0"
    assert manifest["definition"] is None and manifest["backfilled_at"]
    bad_after = _dataset_row(bad["dataset_id"])
    assert bad_after.parquet_sha256 is None and bad_after.status == "built"
    # the verified legacy dataset is now reusable for training
    task = _train(good["dataset_id"], "mad_baseline")
    assert task["status"] == "completed", task


def test_archive_is_explicit_and_refuses_referenced_datasets(token, built):
    status, body = _http("POST", f"/api/ml/datasets/{built['dataset_id']}/archive", token,
                         {"reason": "pytest must be refused"})
    assert status == 409 and body["detail"]["error_code"] == "DATASET_REFERENCED_BY_MODEL"
    assert _dataset_row(built["dataset_id"]).status == "built"

    spare = _build(PREFIX + "archive-me")
    assert spare["status"] == "built"
    row = _dataset_row(spare["dataset_id"])
    assert os.path.exists(row.storage_path) and os.path.exists(row.manifest_path)
    status, body = _http("POST", f"/api/ml/datasets/{spare['dataset_id']}/archive", token,
                         {"reason": "pytest archive drill"})
    assert status == 200 and body["status"] == "archived" and body["bytes_released"] > 0
    after = _dataset_row(spare["dataset_id"])
    assert after.status == "archived" and not os.path.exists(row.storage_path)
    assert os.path.exists(after.manifest_path), "manifest (lineage) is kept"
    assert after.checksum == spare["checksum"] and after.parquet_sha256 == spare["parquet_sha256"]
    status, detail = _http("GET", f"/api/ml/datasets/{spare['dataset_id']}", token)
    assert status == 200 and detail["file_present"] is False and detail["status"] == "archived"
    status, body = _http("POST", f"/api/ml/datasets/{spare['dataset_id']}/archive", token,
                         {"reason": "twice"})
    assert status == 409 and body["detail"]["error_code"] == "DATASET_ALREADY_ARCHIVED"
    actions = _sql("SELECT action FROM ml_audit_log WHERE object_type = 'ml_dataset' ORDER BY created_at DESC LIMIT 5")
    assert {"dataset_archived", "dataset_backfill_hashes"} <= {a[0] for a in actions}
    # archived datasets cannot be trained from
    task = _train(spare["dataset_id"])
    assert task["status"] == "failed" and task["error_code"] == "DATASET_NOT_BUILT"


# ---------------------------------------------------------------------------
# Shadow evidence (mechanism for a human mapping decision)
# ---------------------------------------------------------------------------

def test_shadow_evidence_is_descriptive_and_never_decides(token):
    status, body = _http("GET", "/api/ml/shadow/evidence?days=90", token)
    assert status == 200, body
    assert body["mapping_decision"] == "REQUIRES_VALIDATION"
    for key in ("window_days", "predictions", "models", "note", "truncated"):
        assert key in body, key
    blob = json.dumps(body)
    for banned in ("score_delta", "score_diff", "rules_ml_delta", "rules_ml_difference",
                   "threshold_recommendation", "recommended_cutpoint", "/app/", "artifact_path"):
        assert banned not in blob, banned
    for entry in body["models"].values():
        assert entry["predictions"] >= entry["with_reviewed_outcome"]
        for band in entry["bands"].values():
            assert band["n"] >= band["with_reviewed_outcome"] >= band["positive"] + band["negative"] - 0
    status, body = _http("GET", "/api/ml/shadow/evidence?model_id=not-a-uuid", token)
    assert status == 422 and body["detail"]["error_code"] == "INVALID_MODEL_ID"
    code, out = _cli("shadow-evidence", "--days", "30")
    assert code == 0 and out["mapping_decision"] == "REQUIRES_VALIDATION"

"""
Readiness gates + evidence statistics — engineering ≠ scientific ≠ authority.

  * dataset quality report carries population maturity, appearances-per-entity
    distribution, feature availability BY SPLIT and a cold-start conclusion
  * a trained model records BOTH gates: engineering PASS while scientific is
    INSUFFICIENT_EVIDENCE (no configured minimums -> never invented)
  * evidence statistics: unreviewed predictions are never negatives, Wilson
    intervals are right, trend/Spearman/ranking are suppressed when the sample
    cannot support them, computed when it can
  * the ML contract on the overview reads exactly as operators expect
  * a fixture-configured minimum flips adequacy honestly (and back)
"""

import json
import math
import urllib.error
import urllib.request
import uuid as uuid_mod
from datetime import datetime, timedelta

import pytest

from backend.ml.constants import FEATURE_SET_VERSION
from conftest import run_on_shared_loop as run_async
from test_ml_training_registry import _seed_snapshot_corpus

PREFIX = "pytest-mlrd-"
BASE = "http://localhost:8000"


async def _ensure_db():
    from db_connection import db_manager
    if not getattr(db_manager, "_initialized", False):
        await db_manager.init_db()


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


def _cleanup():
    import os
    async def _run():
        from db_connection import db_manager
        from sqlalchemy import text as sa_text
        await _ensure_db()
        async with db_manager.get_session() as db:
            for path in (await db.execute(sa_text(
                    "SELECT artifact_path FROM ml_models WHERE training_job_id LIKE :p"), {"p": PREFIX + "%"})).scalars():
                if path and os.path.exists(path):
                    os.remove(path)
            for table in ("ml_shadow_comparisons", "ml_predictions"):
                await db.execute(sa_text(f"DELETE FROM {table} WHERE model_id IN (SELECT id FROM ml_models WHERE training_job_id LIKE :p)"), {"p": PREFIX + "%"})
            await db.execute(sa_text("DELETE FROM ml_models WHERE training_job_id LIKE :p"), {"p": PREFIX + "%"})
            for storage, manifest in (await db.execute(sa_text(
                    "SELECT storage_path, manifest_path FROM ml_datasets WHERE name LIKE :p"), {"p": PREFIX + "%"})).all():
                for path in (storage, manifest):
                    if path and os.path.exists(path):
                        os.remove(path)
            await db.execute(sa_text("DELETE FROM ml_datasets WHERE name LIKE :p"), {"p": PREFIX + "%"})
            await db.execute(sa_text("DELETE FROM ml_feature_snapshots WHERE entity_id IN (SELECT id::text FROM identities WHERE display_name LIKE :p)"), {"p": PREFIX + "%"})
            await db.execute(sa_text("DELETE FROM identity_appearances WHERE identity_id IN (SELECT id FROM identities WHERE display_name LIKE :p)"), {"p": PREFIX + "%"})
            await db.execute(sa_text("DELETE FROM identities WHERE display_name LIKE :p"), {"p": PREFIX + "%"})
            await db.execute(sa_text("DELETE FROM background_task_history WHERE job_id LIKE :p"), {"p": PREFIX + "%"})
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


@pytest.fixture(scope="module")
def built():
    async def _run():
        from db_connection import db_manager
        from backend.ml.dataset_builder import build_dataset
        await _ensure_db()
        async with db_manager.get_session() as db:
            return await build_dataset(db, name=PREFIX + "ds", kind="unsupervised", build_job_id=PREFIX + "build")
    out = run_async(_run())
    assert out["status"] == "built", out
    return out


@pytest.fixture(scope="module")
def trained(built):
    async def _run():
        from db_connection import db_manager
        from backend.core.task_history import task_history_manager
        from backend.ml import trainer
        await _ensure_db()
        job_id = PREFIX + uuid_mod.uuid4().hex[:8]
        assert trainer.try_acquire_training(job_id) is None
        await task_history_manager.create_job(job_id=job_id, task_type="ml_training",
                                              task_name="pytest readiness", description="pytest")
        await trainer.run_training_job(job_id, dataset_id=built["dataset_id"])
        return await task_history_manager.get_task_by_job_id(job_id)
    task = run_async(_run())
    assert task["status"] == "completed", task
    return task["result"]


# ---------------------------------------------------------------------------
# Dataset quality: population, availability by split, cold start
# ---------------------------------------------------------------------------

def test_dataset_quality_reports_population_and_availability_by_split(built):
    q = built["quality_report"]
    pop = q["population"]
    assert pop["unique_entities"] > 0 and pop["history_span_days"] is not None
    dist = pop["appearances_per_entity"]
    for key in ("min", "p10", "p25", "median", "p75", "p90", "p95", "max"):
        assert key in dist
    assert dist["min"] <= dist["median"] <= dist["max"]
    for k in (1, 3, 5, 10, 20):
        assert 0.0 <= pop[f"pct_entities_ge_{k}_appearances"] <= 1.0
    assert pop["pct_entities_ge_1_appearances"] >= pop["pct_entities_ge_5_appearances"] >= pop["pct_entities_ge_20_appearances"]
    # appearances are counted from the SOURCE table for the dataset's entities
    n_src = _sql("SELECT count(*) FROM identities WHERE display_name LIKE :p", {"p": PREFIX + "%"}, scalar=True)
    assert pop["unique_entities"] <= n_src

    avail = q["feature_availability_by_split"]["features"]
    assert avail, "per-feature availability present"
    for name, e in avail.items():
        for k in ("overall_available_pct", "train_available_pct", "val_available_pct", "test_available_pct",
                  "train_missing_pct", "val_missing_pct", "test_missing_pct", "availability_delta_train_test",
                  "train_entities_available"):
            assert k in e, (name, k)
        if e["train_available_pct"] is not None:
            assert abs(e["train_available_pct"] + e["train_missing_pct"] - 1.0) < 1e-6
    assert "baseline_hour_deviation_last" in avail and "history_requirement" in avail["baseline_hour_deviation_last"]

    maturity = q["maturity"]
    assert maturity["pipeline_technically_usable"] is True
    assert maturity["behavioral_population_maturity"] == "INSUFFICIENT_EVIDENCE"
    assert maturity["facts"]["history_span_days"] == pop["history_span_days"]
    assert "configured policy" in maturity["interpretation"]
    # split integrity is still what it was
    split = built["split"]
    assert split["method"] == "temporal_group" and split["dropped_for_group_integrity"] >= 0
    # rows dropped for group integrity stay in the Parquet with split=NULL
    # (lineage) and are never used by training: row_count = retained + dropped
    assert sum(split["counts"].values()) + split["dropped_for_group_integrity"] == built["row_count"]
    assert built["extraction"]["selected_rows"] == built["row_count"]


def test_manifest_carries_population_and_maturity(built):
    from backend.ml.dataset_builder import read_manifest
    row = _sql("SELECT manifest_path FROM ml_datasets WHERE id = CAST(:d AS uuid)", {"d": built["dataset_id"]}, scalar=True)
    class R: manifest_path = row
    manifest = read_manifest(R())
    assert manifest["population"]["unique_entities"] == built["quality_report"]["population"]["unique_entities"]
    assert manifest["maturity"]["behavioral_population_maturity"] == "INSUFFICIENT_EVIDENCE"
    assert "features" in manifest["feature_availability_by_split"]


# ---------------------------------------------------------------------------
# Gates on the trained model
# ---------------------------------------------------------------------------

def test_engineering_passes_while_scientific_is_insufficient(trained):
    ev = trained["evaluation"]
    eg, sg = ev["engineering_gate"], ev["scientific_gate"]
    assert eg["status"] == "PASS", eg
    for check in ("dataset_checksum_recorded", "parquet_file_hash_recorded", "temporal_group_split_valid",
                  "entity_group_isolation", "artifact_checksum_recorded", "inference_contract_medians_complete",
                  "code_version_recorded", "reload_determinism", "score_distribution_nondegenerate"):
        assert eg["checks"][check]["passed"] is True, check
    assert sg["status"] == "INSUFFICIENT_EVIDENCE"
    codes = {r["code"] for r in sg["reasons"]}
    assert "NOT_CONFIGURED" in codes and "SIGNAL_MAPPING_UNVALIDATED" in codes
    m = sg["metrics"]
    for key in ("history_span_days", "unique_entities", "appearances_per_entity", "feature_availability",
                "feature_availability_delta_train_test", "train_test_score_shift", "reviewed_outcome_coverage",
                "signal_mapping_validation_status"):
        assert key in m, key
    assert m["signal_mapping_validation_status"] == "REQUIRES_VALIDATION"
    assert trained["engineering_gate"] == "PASS" and trained["scientific_gate"] == "INSUFFICIENT_EVIDENCE"
    cfg = trained["training_config"]
    assert cfg["preprocessor_version"] == "impute-train-median-v1"
    assert len(cfg["feature_schema_hash"]) == 64 and cfg["rows"]["train"] > 0 and cfg["train_entities"] > 0
    assert trained["code_version"], "no null code_version on a new candidate"
    shift = ev["temporal_shift"]
    for key in ("train_p90", "test_p90", "score_psi_train_to_test", "score_ks_train_to_test",
                "availability_gains_train_to_test", "test_share_highly_unusual"):
        assert key in shift, key
    assert "feature_availability_by_split" in ev


def test_engineering_gate_fails_without_code_version():
    from backend.ml.readiness import engineering_gate
    out = engineering_gate(quality_gates={"reload_determinism": {"passed": True}}, dataset_quality_passed=True,
                           split_meta={"method": "temporal_group", "counts": {"train": 10}, "group_counts": {"train": 3},
                                       "dropped_for_group_integrity": 0},
                           artifact_hash="abc", code_version=None, feature_names=["a"], medians={"a": 1.0},
                           seed_stability=0.9, dataset_checksum="x", parquet_sha256="y")
    assert out["status"] == "FAIL" and "code_version_recorded" in out["failed"]
    out = engineering_gate(quality_gates={"reload_determinism": {"passed": True}}, dataset_quality_passed=True,
                           split_meta={"method": "temporal_group", "counts": {"train": 10}, "group_counts": {"train": 3},
                                       "dropped_for_group_integrity": 0},
                           artifact_hash="abc", code_version="deadbeef", feature_names=["a", "b"], medians={"a": 1.0},
                           seed_stability=0.9, dataset_checksum="x", parquet_sha256="y")
    assert out["status"] == "FAIL" and "inference_contract_medians_complete" in out["failed"]


def test_scientific_gate_uses_only_configured_minimums(monkeypatch):
    from backend.ml.readiness import scientific_gate
    from config import settings
    population = {"history_span_days": 34.0, "unique_entities": 100,
                  "appearances_per_entity": {"median": 3.0}, "pct_entities_ge_5_appearances": 0.2}
    availability = {"features": {"x": {"overall_available_pct": 0.9, "availability_delta_train_test": 0.0}}}
    out = scientific_gate(population=population, availability=availability, score_shift=None,
                          evidence_coverage={"reviewed_total": 10, "reviewed_per_band": {"normal": 8, "highly_unusual": 2}},
                          mapping_validated=False)
    assert out["status"] == "INSUFFICIENT_EVIDENCE"
    assert {r["code"] for r in out["reasons"]} == {"NOT_CONFIGURED", "SIGNAL_MAPPING_UNVALIDATED"}
    monkeypatch.setattr(settings, "ML_SCIENTIFIC_MIN_HISTORY_DAYS", 90)
    monkeypatch.setattr(settings, "ML_EVIDENCE_MIN_REVIEWED_PER_BAND", 30)
    out = scientific_gate(population=population, availability=availability, score_shift=None,
                          evidence_coverage={"reviewed_total": 10, "reviewed_per_band": {"normal": 8, "highly_unusual": 2}},
                          mapping_validated=True)
    codes = {r["code"] for r in out["reasons"]}
    assert codes == {"HISTORY_SPAN_BELOW_MINIMUM", "REVIEWED_OUTCOMES_PER_BAND_BELOW_MINIMUM"}
    monkeypatch.setattr(settings, "ML_SCIENTIFIC_MIN_HISTORY_DAYS", 30)
    monkeypatch.setattr(settings, "ML_EVIDENCE_MIN_REVIEWED_PER_BAND", 2)
    out = scientific_gate(population=population, availability=availability, score_shift=None,
                          evidence_coverage={"reviewed_total": 10, "reviewed_per_band": {"normal": 8, "highly_unusual": 2}},
                          mapping_validated=True)
    assert out["status"] == "SUFFICIENT_EVIDENCE" and out["reasons"] == []


# ---------------------------------------------------------------------------
# Evidence statistics
# ---------------------------------------------------------------------------

def test_wilson_interval_and_band_table():
    from backend.ml.evidence_stats import band_table, wilson_interval
    ci = wilson_interval(3, 10)
    assert abs(ci["low"] - 0.1078) < 0.002 and abs(ci["high"] - 0.6032) < 0.002   # textbook value
    assert wilson_interval(0, 0) is None
    reviewed = [{"band": "normal", "outcome": "negative", "score": 0.1}] * 4 + \
               [{"band": "normal", "outcome": "positive", "score": 0.2}] + \
               [{"band": "highly_unusual", "outcome": "positive", "score": 0.9}] * 2
    table = band_table(reviewed)
    assert table["normal"]["reviewed_count"] == 5 and table["normal"]["positive_rate"] == 0.2
    assert table["highly_unusual"]["positive_count"] == 2 and table["elevated"]["reviewed_count"] == 0
    assert table["elevated"]["positive_rate"] is None and table["elevated"]["wilson_95"] is None


def test_unreviewed_predictions_are_never_negatives_and_coverage_is_per_band():
    from backend.ml.evidence_stats import band_table, coverage
    table = band_table([{"band": "unusual", "outcome": "positive", "score": 0.8}])
    cov = coverage({"normal": 100, "elevated": 20, "unusual": 5, "highly_unusual": 2}, table)
    assert cov["predictions_total"] == 127 and cov["reviewed_total"] == 1
    assert cov["reviewed_per_band"]["normal"] == 0 and table["normal"]["negative_count"] == 0
    assert cov["review_coverage_per_band"]["unusual"] == 0.2 and cov["review_coverage_per_band"]["normal"] == 0.0
    assert abs(cov["review_coverage_overall"] - 1 / 127) < 1e-4   # ratio is rounded to 4 dp


def test_trend_spearman_ranking_are_suppressed_or_computed_honestly():
    from backend.ml.evidence_stats import (
        band_table, cochran_armitage_trend, ranking_metrics, spearman_score_outcome)
    tiny = [{"band": "normal", "outcome": "negative", "score": 0.1},
            {"band": "highly_unusual", "outcome": "positive", "score": 0.9}]
    assert cochran_armitage_trend(band_table(tiny))["status"] == "computed" or \
        cochran_armitage_trend(band_table(tiny))["status"] == "INSUFFICIENT_SAMPLE"
    assert spearman_score_outcome(tiny)["status"] == "INSUFFICIENT_SAMPLE"
    assert ranking_metrics(tiny)["status"] == "INSUFFICIENT_SAMPLE"
    one_class = [{"band": "normal", "outcome": "negative", "score": s} for s in (0.1, 0.2, 0.3, 0.4)]
    assert cochran_armitage_trend(band_table(one_class))["status"] == "INSUFFICIENT_SAMPLE"
    assert ranking_metrics(one_class)["status"] == "INSUFFICIENT_SAMPLE"
    # a clear monotone relationship
    rows = []
    for band, pos, neg, base in (("normal", 1, 19, 0.1), ("elevated", 3, 12, 0.4),
                                 ("unusual", 6, 6, 0.7), ("highly_unusual", 9, 2, 0.9)):
        rows += [{"band": band, "outcome": "positive", "score": base + 0.01 * i} for i in range(pos)]
        rows += [{"band": band, "outcome": "negative", "score": base - 0.01 * i} for i in range(neg)]
    trend = cochran_armitage_trend(band_table(rows))
    assert trend["status"] == "computed" and trend["direction"] == "increasing" and trend["p_value"] < 0.01
    sp = spearman_score_outcome(rows)
    assert sp["status"] == "computed" and sp["rho"] > 0.4 and sp["n"] == len(rows)
    rk = ranking_metrics(rows)
    assert rk["status"] == "computed" and rk["roc_auc"] > 0.7 and rk["pr_auc"] >= rk["prevalence_in_reviewed"]
    assert rk["precision_at_top_10_pct"] >= rk["prevalence_in_reviewed"] and rk["lift_at_top_10_pct"] >= 1.0
    assert "stratified" in rk["note"]


def test_adequacy_reads_configured_minimums_only(monkeypatch):
    from backend.ml.evidence_stats import adequacy
    from config import settings
    cov = {"reviewed_total": 10, "reviewed_per_band": {"normal": 8, "elevated": 0, "unusual": 0, "highly_unusual": 2}}
    assert adequacy(cov)["status"] == "NOT_CONFIGURED"
    monkeypatch.setattr(settings, "ML_EVIDENCE_MIN_REVIEWED_PER_BAND", 5)
    out = adequacy(cov)
    assert out["status"] == "INSUFFICIENT_EVIDENCE"
    assert {s["band"] for s in out["shortfalls"]} == {"elevated", "unusual", "highly_unusual"}


# ---------------------------------------------------------------------------
# HTTP: the contract, evidence block and label selection metadata
# ---------------------------------------------------------------------------

def _http(method, path, body=None, token=None):
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"}
    if method != "GET":
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


def test_overview_contract_and_evidence_block(token):
    status, body = _http("GET", "/api/ml/overview", token=token)
    assert status == 200, body
    c = body["system"]["ml_contract"]
    assert c["rules"] == "AUTHORITATIVE" and c["fallback"] == "RULES"
    assert c["signal_mapping"] == "REQUIRES_VALIDATION" and c["ml_decision_authority"] == "DISABLED"
    assert c["scientific_validity"] in ("INSUFFICIENT_EVIDENCE", "NOT_RECORDED")
    assert c["dataset"] == "VALID_FOR_EXPERIMENTATION" and c["feature_set"] == "ACTIVE"
    status, ev = _http("GET", "/api/ml/shadow/evidence?days=90", token=token)
    assert status == 200 and ev["mapping_decision"] == "REQUIRES_VALIDATION"
    for entry in ev["models"].values():
        e = entry["evidence"]
        for key in ("bands", "coverage", "adequacy", "monotonicity_trend", "spearman_score_vs_outcome",
                    "ranking", "band_separation", "review_selection_methods", "sampling_caveat"):
            assert key in e, key
        for band, b in e["bands"].items():
            assert b["reviewed_count"] == b["positive_count"] + b["negative_count"]
            assert b["reviewed_count"] <= e["coverage"]["predictions_per_band"][band]
        assert e["adequacy"]["status"] in ("NOT_CONFIGURED", "INSUFFICIENT_EVIDENCE", "ADEQUATE")
        for stat in ("monotonicity_trend", "spearman_score_vs_outcome", "ranking"):
            assert e[stat]["status"] in ("computed", "INSUFFICIENT_SAMPLE")
    blob = json.dumps(ev)
    for banned in ("score_delta", "score_diff", "rules_ml_delta", "recommended_cutpoint"):
        assert banned not in blob


def test_label_selection_metadata_is_stored_and_validated(token):
    status, body = _http("POST", "/api/ml/labels", token=token, body={
        "subject_id": "0" * 36, "label": "negative", "label_kind": "weak",
        "event_time": "2026-01-01T00:00:00Z", "selection": {"method": "teleport"}})
    assert status == 422 and body["detail"]["error_code"] == "INVALID_SELECTION"
    status, body = _http("POST", "/api/ml/labels", token=token, body={
        "subject_id": "0" * 36, "label": "negative", "label_kind": "weak",
        "event_time": "2026-01-01T00:00:00Z", "selection": {"method": "stratified_by_band", "sampling_probability": 2}})
    assert status == 422 and body["detail"]["error_code"] == "INVALID_SELECTION"

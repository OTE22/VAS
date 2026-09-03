"""
ML pipeline Milestone 1: schema, config, RBAC, labels, retention, audit.
========================================================================
Run INSIDE the api container against the live app:

    docker exec face_recognition_api python -m pytest tests/test_ml_foundation.py -v

Pins the approval-binding rules: ORM == database (schema-consistency),
anomaly models DB-capped at shadow, RULES default mode, sampled full-vector
rate 0.0, unresolved assessments never become manual labels, only reviewed
manual labels count toward the supervised gate, optional dependencies report
four distinct statuses honestly.
"""

import json
import urllib.error
import urllib.request
import uuid as uuid_mod
from datetime import datetime, timedelta

import pytest

from conftest import run_on_shared_loop as run_async  # asyncpg is loop-bound

BASE = "http://localhost:8000"
PREFIX = "pytest-mlf-"

ML_TABLES = [
    "ml_feature_definitions", "ml_feature_snapshots", "ml_collection_checkpoints",
    "ml_labels", "ml_datasets", "ml_models", "ml_model_thresholds",
    "ml_predictions", "ml_shadow_comparisons", "ml_drift_reports",
    "ml_retraining_policies", "ml_audit_log",
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
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw or b"{}")
        except Exception:
            return e.code, {"_raw": raw.decode(errors="replace")}


@pytest.fixture(scope="module")
def token():
    status, body = _http("POST", "/api/auth/login",
                         {"username": "admin", "password": "admin123"})
    assert status == 200, body
    return body["access_token"]


async def _ensure_db():
    from db_connection import db_manager
    if not getattr(db_manager, "_initialized", False):
        await db_manager.init_db()


def _cleanup():
    async def _run():
        from db_connection import db_manager
        from sqlalchemy import text as sa_text
        await _ensure_db()
        async with db_manager.get_session() as db:
            await db.execute(sa_text(
                "DELETE FROM ml_labels WHERE created_by LIKE :p OR subject_id LIKE :p"),
                {"p": PREFIX + "%"})
            await db.execute(sa_text(
                "DELETE FROM threat_assessments WHERE subject_id LIKE :p"),
                {"p": PREFIX + "%"})
            await db.commit()
    run_async(_run())


@pytest.fixture(scope="module", autouse=True)
def cleanup_module():
    _cleanup()
    yield
    _cleanup()


# ---------------------------------------------------------------------------
# Schema consistency (approval correction E): ORM == database
# ---------------------------------------------------------------------------

def test_orm_and_database_schema_cannot_diverge():
    """Columns (name + nullability) and index names of every ML table must
    match between the ORM metadata and the live migrated database."""
    async def _run():
        from db_connection import db_manager
        from sqlalchemy import text as sa_text
        import db_models
        await _ensure_db()
        problems = []
        async with db_manager.get_session() as db:
            for table_name in ML_TABLES:
                orm_table = db_models.Base.metadata.tables[table_name]
                db_cols = {row[0]: row[1] for row in (await db.execute(sa_text(
                    "SELECT column_name, is_nullable FROM information_schema.columns "
                    "WHERE table_name = :t"), {"t": table_name})).all()}
                orm_cols = {c.name: ("YES" if c.nullable else "NO")
                            for c in orm_table.columns}
                if set(db_cols) != set(orm_cols):
                    problems.append(
                        f"{table_name}: columns differ "
                        f"(orm-only={set(orm_cols) - set(db_cols)}, "
                        f"db-only={set(db_cols) - set(orm_cols)})")
                    continue
                for name, orm_nullable in orm_cols.items():
                    if db_cols[name] != orm_nullable:
                        problems.append(
                            f"{table_name}.{name}: nullability orm={orm_nullable} db={db_cols[name]}")
                db_indexes = {row[0] for row in (await db.execute(sa_text(
                    "SELECT indexname FROM pg_indexes WHERE tablename = :t"),
                    {"t": table_name})).all()}
                orm_indexes = {i.name for i in orm_table.indexes}
                missing = orm_indexes - db_indexes
                if missing:
                    problems.append(f"{table_name}: indexes missing in db: {missing}")
        return problems
    problems = run_async(_run())
    assert not problems, "ORM/database divergence:\n" + "\n".join(problems)


def test_migration_head_and_seeds():
    async def _run():
        from db_connection import db_manager
        from sqlalchemy import text as sa_text
        await _ensure_db()
        async with db_manager.get_session() as db:
            head = (await db.execute(sa_text(
                "SELECT version_num FROM alembic_version"))).scalar()
            policies = (await db.execute(sa_text(
                "SELECT model_type, enabled FROM ml_retraining_policies ORDER BY model_type"))).all()
            ta_cols = {row[0] for row in (await db.execute(sa_text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name='threat_assessments' "
                "AND column_name IN ('decision_mode','ml_prediction_id')"))).all()}
            return head, policies, ta_cols
    head, policies, ta_cols = run_async(_run())
    # Head pin, updated deliberately when a migration is added.
    # d5f9b2c7e3a1 = identity_audit_log survives identity deletion, which revises
    # 7d3f91a2c4e6 = JSON null literals -> SQL NULL, which revises
    # f6a7b8c9d0e1 = create_all residue alignment (after c2d3e4f5a6b7 → d4e5f6a7b8c9), which revises
    # a9b0c1d2e3f4 (identity person_code), which revises
    # f8a9b0c1d2e3 (merge provenance), which revises
    # e7f8a9b0c1d2 (webhook ingest credentials), which revises
    # d6e7f8a9b0c1 (pending enrollment decisions), which revises
    # c5d6e7f8a9b0 (drop dead faiss_id + saved_searches) -> b4c5d6e7f8a9
    # (quality scorer provenance) -> a3b4c5d6e7f8 -> f2a3b4c5d6e7 ->
    # e1f2a3b4c5d6 -> d0e1f2a3b4c5 -> b8c9d0e1f2a3 (this ML-pipeline
    # migration) — so asserting the newest head still proves the ML lineage
    # is applied.
    assert head == "d6f8a2c4e7b1"
    assert [p[0] for p in policies] == [
        "behavior_anomaly_model", "coappearance_anomaly_model",
        "social_graph_anomaly_model", "threat_ranking_model"]
    assert all(p[1] is False for p in policies), "retraining must ship DISABLED"
    assert ta_cols == {"decision_mode", "ml_prediction_id"}


def test_anomaly_models_are_db_capped_at_shadow():
    """Approval correction B: even a direct SQL write cannot put an anomaly
    model into approved/production — the CHECK constraint refuses."""
    async def _run():
        from db_connection import db_manager
        from sqlalchemy import text as sa_text
        await _ensure_db()
        outcomes = {}
        async with db_manager.get_session() as db:
            for stage in ("production", "approved"):
                try:
                    await db.execute(sa_text(
                        "INSERT INTO ml_models (id, model_type, version, stage, algorithm, "
                        " model_purpose, score_type, is_probability, calibration_status, "
                        " artifact_name, artifact_path, artifact_hash, dependency_versions, "
                        " feature_set_version, feature_names, created_at) "
                        "VALUES (gen_random_uuid(), 'behavior_anomaly_model', 9999, :s, "
                        " 'isolation_forest', 'behavioral_anomaly_detection', 'anomaly_score', "
                        " false, 'not_applicable', 'x', 'x', 'x', '{}', 'v1', '[]', now())"),
                        {"s": stage})
                    outcomes[stage] = "ACCEPTED"
                    await db.rollback()
                except Exception as e:
                    outcomes[stage] = ("REFUSED"
                                       if "ck_ml_models_anomaly_shadow_cap" in str(e) else str(e)[:120])
                    await db.rollback()
        return outcomes
    outcomes = run_async(_run())
    assert outcomes == {"production": "REFUSED", "approved": "REFUSED"}, outcomes


# ---------------------------------------------------------------------------
# Configuration honesty
# ---------------------------------------------------------------------------

def test_rules_default_and_privacy_defaults():
    from config import settings
    assert settings.ML_DECISION_MODE == "rules"
    assert settings.ML_FEATURE_SAMPLED_FULL_VECTOR_RATE == 0.0, (
        "full-vector sampling must ship DISABLED (privacy default)")
    for flag in ("MLFLOW_ENABLED", "OPTUNA_ENABLED", "XGBOOST_ENABLED", "SHAP_ENABLED"):
        assert getattr(settings, flag) is False, f"{flag} must default off"


def test_optional_capabilities_report_four_statuses():
    from backend.ml.constants import all_optional_capabilities
    caps = all_optional_capabilities()
    assert set(caps) == {"mlflow", "optuna", "xgboost", "shap"}
    for name, status in caps.items():
        assert set(status) == {"configured", "implemented",
                               "dependency_available", "operational"}, name
        # A flag alone must never read as available.
        if not status["dependency_available"] or not status["implemented"]:
            assert status["operational"] is False, name


def test_ml_settings_surface_in_settings_api(token):
    status, _ = _http("GET", "/api/settings", token=token)
    assert status == 200
    status, setting = _http("GET", "/api/settings/ML_DECISION_MODE", token=token)
    assert status == 200, setting
    assert setting["value_type"] == "string"
    assert setting["requires_restart"] is False
    status, rate = _http("GET", "/api/settings/ML_FEATURE_SAMPLED_FULL_VECTOR_RATE", token=token)
    assert status == 200, rate
    assert float(rate["effective_value"]) == 0.0


def test_ml_manage_capability_granted_to_admin(token):
    status, body = _http("GET", "/api/auth/me/privileges", token=token)
    assert status == 200, body
    perms = body.get("permissions") or []
    assert "admin.ml.manage" in perms, perms


# ---------------------------------------------------------------------------
# Label workflow rules
# ---------------------------------------------------------------------------

def _seed_assessment(status_value):
    async def _run():
        from db_connection import db_manager
        from sqlalchemy import text as sa_text
        await _ensure_db()
        async with db_manager.get_session() as db:
            subject = PREFIX + uuid_mod.uuid4().hex[:8]
            row_id = (await db.execute(sa_text(
                "INSERT INTO threat_assessments (id, subject_type, subject_id, "
                " total_risk_score, severity, confidence, signals, model_version, "
                " status, source_timestamp, idempotency_key, created_at, updated_at) "
                "VALUES (gen_random_uuid(), 'identity', :s, 10.0, 'low', 0.5, '[]', "
                " 'risk-engine-v1', :st, now(), :k, now(), now()) RETURNING id"),
                {"s": subject, "st": status_value,
                 "k": PREFIX + uuid_mod.uuid4().hex})).scalar()
            await db.commit()
            return str(row_id), subject
    return run_async(_run())


def test_unresolved_assessment_cannot_back_a_manual_label():
    assessment_id, subject = _seed_assessment("open")

    async def _run():
        from db_connection import db_manager
        from backend.ml.labeling_service import labeling_service
        await _ensure_db()
        async with db_manager.get_session() as db:
            try:
                await labeling_service.create_label(
                    db, subject_id=subject, label="positive", label_kind="manual",
                    source="analyst_review", event_time=datetime.utcnow(),
                    created_by=PREFIX + "analyst", assessment_id=assessment_id)
                return "ACCEPTED"
            except ValueError as e:
                return str(e)
    outcome = run_async(_run())
    assert outcome.startswith("UNREVIEWED_ALERT"), outcome


def test_weak_label_from_unresolved_assessment_is_allowed_with_reduced_confidence():
    assessment_id, subject = _seed_assessment("open")

    async def _run():
        from db_connection import db_manager
        from backend.ml.labeling_service import labeling_service
        await _ensure_db()
        async with db_manager.get_session() as db:
            return await labeling_service.create_label(
                db, subject_id=subject, label="negative", label_kind="weak",
                source="weak_rule:pytest", event_time=datetime.utcnow(),
                created_by=PREFIX + "system", assessment_id=assessment_id)
    payload = run_async(_run())
    assert payload["label_kind"] == "weak"
    assert payload["confidence"] < 1.0, "weak labels never carry full confidence"
    assert payload["review_status"] == "unreviewed"


def test_label_idempotency_and_review_transitions():
    subject = PREFIX + uuid_mod.uuid4().hex[:8]
    event_time = datetime.utcnow() - timedelta(hours=1)

    async def _run():
        from db_connection import db_manager
        from backend.ml.labeling_service import labeling_service
        await _ensure_db()
        async with db_manager.get_session() as db:
            first = await labeling_service.create_label(
                db, subject_id=subject, label="negative", label_kind="manual",
                source="analyst_review", event_time=event_time,
                created_by=PREFIX + "analyst")
            duplicate = await labeling_service.create_label(
                db, subject_id=subject, label="negative", label_kind="manual",
                source="analyst_review", event_time=event_time,
                created_by=PREFIX + "analyst")
            confirmed = await labeling_service.review_label(
                db, first["id"], action="confirm", actor=PREFIX + "reviewer")
            retracted = await labeling_service.review_label(
                db, first["id"], action="retract", actor=PREFIX + "reviewer")
            try:
                await labeling_service.review_label(
                    db, first["id"], action="confirm", actor=PREFIX + "reviewer")
                after_retract = "ACCEPTED"
            except ValueError as e:
                after_retract = str(e)
        return first, duplicate, confirmed, retracted, after_retract
    first, duplicate, confirmed, retracted, after_retract = run_async(_run())
    assert duplicate["deduplicated"] is True and duplicate["id"] == first["id"]
    assert confirmed["review_status"] == "reviewed"
    assert retracted["status"] == "retracted"
    assert "retracted" in after_retract


def test_supervised_gate_counts_only_reviewed_manual_labels():
    async def _run():
        from db_connection import db_manager
        from backend.ml.labeling_service import labeling_service
        await _ensure_db()
        async with db_manager.get_session() as db:
            base = datetime.utcnow() - timedelta(days=2)
            subject = PREFIX + uuid_mod.uuid4().hex[:8]
            reviewed = await labeling_service.create_label(
                db, subject_id=subject, label="positive", label_kind="manual",
                source="analyst_review", event_time=base, created_by=PREFIX + "a")
            await labeling_service.review_label(
                db, reviewed["id"], action="confirm", actor=PREFIX + "r")
            # unreviewed manual + weak: must NOT count
            await labeling_service.create_label(
                db, subject_id=subject, label="positive", label_kind="manual",
                source="analyst_review", event_time=base + timedelta(days=1),
                created_by=PREFIX + "a")
            await labeling_service.create_label(
                db, subject_id=subject, label="positive", label_kind="weak",
                source="weak_rule:pytest", event_time=base + timedelta(hours=1),
                created_by=PREFIX + "s")
            stats = await labeling_service.label_stats(db)
            refusal = labeling_service.supervised_refusal(stats)
        return stats, refusal
    stats, refusal = run_async(_run())
    counted = stats["counted_reviewed_manual"]
    assert counted["positive"] >= 1
    assert stats["supervised_gate_open"] is False, "0-ish labels cannot open the gate"
    assert refusal["status"] == "blocked"
    assert refusal["reason"] == "INSUFFICIENT_REVIEWED_LABELS"
    for key in ("required_total", "required_per_class", "available_total",
                "available_positive", "available_negative"):
        assert key in refusal


# ---------------------------------------------------------------------------
# Audit + retention
# ---------------------------------------------------------------------------

def test_ml_audit_rows_written_and_immutable_by_convention():
    async def _run():
        from db_connection import db_manager
        from sqlalchemy import text as sa_text
        await _ensure_db()
        async with db_manager.get_session() as db:
            count = (await db.execute(sa_text(
                "SELECT count(*) FROM ml_audit_log WHERE action IN "
                "('label_create','label_review')"))).scalar()
        return count
    count = run_async(_run())
    assert count >= 2, "label actions above must have produced audit rows"
    # Immutability is by convention: no CODE path updates or deletes rows
    # (docstrings describing the rule must not satisfy/violate the check).
    with open("/app/backend/ml/audit.py", encoding="utf-8") as f:
        src = f.read()
    code = "\n".join(
        line for line in src.split('"""')[-1].splitlines()
        if not line.lstrip().startswith("#"))
    assert "delete" not in code.lower() and ".update(" not in code
    with open("/app/backend/core/data_retention.py", encoding="utf-8") as f:
        retention_src = f.read()
    assert '"ml_audit_log"' not in retention_src and "FROM ml_audit_log" not in retention_src, (
        "ml_audit_log must be excluded from retention sweeps")


def test_retention_covers_ml_tables():
    with open("/app/backend/core/data_retention.py", encoding="utf-8") as f:
        src = f.read()
    for table in ("ml_predictions", "ml_feature_snapshots", "ml_drift_reports"):
        assert table in src, f"retention must sweep {table}"
    for key in ("ML_PREDICTION_RETENTION_DAYS", "ML_SNAPSHOT_RETENTION_DAYS",
                "ML_DRIFT_REPORT_RETENTION_DAYS"):
        assert key in src
    for protected in ("ml_labels", "ml_models", "ml_datasets"):
        assert f'"{protected}"' not in src, (
            f"{protected} is provenance and must NOT be swept")

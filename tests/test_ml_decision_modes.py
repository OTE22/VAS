"""
ML Milestone 4: decision modes, shadow inference, fallback honesty.
===================================================================
Run INSIDE the api container against the live app:

    docker exec face_recognition_api python -m pytest tests/test_ml_decision_modes.py -v

Pins: RULES default byte-compatibility on the live endpoint; SHADOW never
changes the live result while persisting side-by-side comparisons in the
approved separate-concepts vocabulary (no score diffs anywhere); gated
HYBRID/ML resolve to rules with exact recorded reasons; every fallback
reason is honest and persisted; prediction idempotency; artifact tamper
falls back, never serves.
"""

import json
import urllib.error
import urllib.request
import uuid as uuid_mod
from datetime import datetime

import pytest

from backend.ml.constants import FEATURE_SET_VERSION

from conftest import run_on_shared_loop as run_async

BASE = "http://localhost:8000"
JOB_PREFIX = "pytest-mlm4-"


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


def _set_mode(token, mode, reason):
    status, body = _http("PUT", "/api/settings/ML_DECISION_MODE",
                         {"value": mode, "change_reason": reason},
                         token=token, headers={"X-Requested-With": "XMLHttpRequest"})
    assert status == 200, body


SEEDED_SUBJECT_NAME = "pytest-mlmode-subject"


def _get_identity(token):
    """An identity to assess, seeding one when the database is empty.

    This used to require pre-existing production data ("a real identity id
    from live data"), so the whole module failed once the enrollment gallery
    was purged — an empty system is now a legitimate starting state. The
    decision-mode contract does not depend on WHICH identity is assessed, only
    that one exists, so the test owns its subject.
    """
    async def _run():
        from db_connection import db_manager
        from sqlalchemy import text as sa_text
        await _ensure_db()
        async with db_manager.get_session() as db:
            existing = (await db.execute(sa_text(
                "SELECT id FROM identities WHERE display_name NOT LIKE 'pytest%' "
                "ORDER BY last_seen_at DESC NULLS LAST LIMIT 1"))).scalar()
            if existing:
                return str(existing)
            seeded = (await db.execute(sa_text(
                "SELECT id FROM identities WHERE display_name = :n"),
                {"n": SEEDED_SUBJECT_NAME})).scalar()
            if seeded:
                return str(seeded)
            created = (await db.execute(sa_text(
                "INSERT INTO identities (id, type, status, display_name, "
                "  first_seen_at, last_seen_at, created_at, updated_at, appearances_count) "
                "VALUES (gen_random_uuid(), 'KNOWN', 'ACTIVE', :n, now(), now(), "
                "  now(), now(), 0) RETURNING id"), {"n": SEEDED_SUBJECT_NAME})).scalar()
            await db.commit()
            return str(created)
    return run_async(_run())




async def _seed_snapshot_corpus(db, prefix, cam, count=20):
    """Seed identities + appearances and run the REAL collector over them.

    Training builds a dataset from ml_feature_snapshots; before the demo-data
    wipe this suite silently trained over ambient demo snapshots. It now makes
    its own - through run_collection, the path production uses.

    THE CORPUS MUST BE VARIED, NOT MERELY PRESENT. Training gates on
    `score_distribution_nondegenerate` (trainer.py:299) — std > 0 across the
    isolation forest's scores for the train split. A corpus in which every
    identity looks identical scores every row identically, so the std is
    exactly 0.0 and the gate fails; the same degeneracy then produces NaN in
    the metrics JSON and PostgreSQL rejects the ml_models insert outright
    ('Token "NaN" is invalid').

    The previous version was degenerate by construction: 14 identities, all
    UNKNOWN, all with exactly 2 appearances, all on one pipeline, all inside a
    single 14-minute window. Five of the six features the trainer selects were
    therefore constant, and the sixth (night_count_30d, appearances 00:00-04:59
    local) varied only when that window happened to straddle 05:00 — so the
    suite passed or failed on the wall-clock time it was run at.

    Each selected feature is now varied deliberately:

        is_unknown_identity        alternating UNKNOWN / KNOWN
        appearance_count_30d       1-4 appearances per identity
        appearance_count_7d        visits fan out over 30 days, so the 7-day
                                   count differs from the 30-day one
        distinct_pipelines_30d/7d  some identities on one camera, some on two
        night_count_30d            hours spread across 00:00-04:59 and daytime

    The corpus must also SPAN TIME. `temporal_group_split` is chronological,
    not proportional: it takes the earliest ~60% of the as_of range as train
    and reserves the tail for val/test. A corpus bunched into a few days puts
    almost everything in the holdout and trips `minimum_train_rows` (20) no
    matter how many rows there are — 50 rows inside a 3-day window yielded a
    train split of 15. So each identity's visits start well back in the window
    and step forward, giving the splitter a real timeline to cut.
    """
    from datetime import datetime, timedelta

    from sqlalchemy import text as sa_text

    from backend.ml.collector import run_collection

    now = datetime.utcnow().replace(microsecond=0)
    second_cam = cam + "2"
    for pipeline_id in (cam, second_cam):
        await db.execute(sa_text(
            "INSERT INTO pipelines (pipeline_id, created_at, is_active) "
            "VALUES (:p, now(), 1) ON CONFLICT (pipeline_id) DO NOTHING"),
            {"p": pipeline_id})

    base = (now - timedelta(days=2)).replace(minute=0, second=0)

    for index in range(count):
        visits = 1 + (index % 4)                      # 1..4
        # Each identity's first sighting is 9-24 days back, so the corpus
        # covers the whole window rather than clustering at one end.
        first_day = 24 - (index % 6) * 3
        stamps = []
        for visit in range(visits):
            # Even identities are night visitors (02:00-04:00), odd ones are
            # daytime (09:00-15:00). night_count_30d then genuinely varies.
            hour = (2 + visit % 3) if index % 2 == 0 else (9 + (visit * 2) % 7)
            day = max(0, first_day - visit * 3)
            stamps.append((base - timedelta(days=day)).replace(
                hour=hour, minute=(index * 7) % 60))

        identity_id = (await db.execute(sa_text(
            "INSERT INTO identities (id, type, status, display_name, first_seen_at, "
            " last_seen_at, created_at, updated_at, appearances_count) "
            "VALUES (gen_random_uuid(), :t, 'ACTIVE', :n, :first, :last, now(), "
            "        now(), 0) RETURNING id"),
            {"t": "UNKNOWN" if index % 2 == 0 else "KNOWN",
             "n": f"{prefix}corpus{index:02d}",
             "first": min(stamps), "last": max(stamps)})).scalar()

        for visit, timestamp in enumerate(stamps):
            # Every third identity also appears on the second camera, so
            # distinct_pipelines is 1 for most and 2 for some.
            pipeline_id = (second_cam if (index % 3 == 0 and visit % 2 == 1)
                           else cam)
            await db.execute(sa_text(
                "INSERT INTO identity_appearances (identity_id, pipeline_id, "
                " start_time, created_at) VALUES (:i, :p, :ts, now())"),
                {"i": identity_id, "p": pipeline_id, "ts": timestamp})
    await db.commit()
    stats = await run_collection(db, full_rebuild=True)
    await db.commit()
    assert stats["snapshots_written"] >= 10, (
        f"collector produced too few snapshots for a dataset build: {stats}")


def _train_and_shadow():
    """Train a fresh candidate and approve it into shadow (full approval)."""
    async def _run():
        from db_connection import db_manager
        from backend.core.task_history import task_history_manager
        from backend.ml import trainer
        from backend.ml.registry_service import registry_service
        await _ensure_db()
        job_id = JOB_PREFIX + uuid_mod.uuid4().hex[:8]
        async with db_manager.get_session() as db:
            await _seed_snapshot_corpus(db, JOB_PREFIX, JOB_PREFIX + "cam")
        assert trainer.try_acquire_training(job_id) is None
        await task_history_manager.create_job(
            job_id=job_id, task_type="ml_training",
            task_name="pytest m4 training", description="pytest")
        await trainer.run_training_job(job_id)
        task = await task_history_manager.get_task_by_job_id(job_id)
        assert task["status"] == "completed", task
        model = task["result"]
        async with db_manager.get_session() as db:
            shadowed = await registry_service.transition(
                db, model["model_id"], to_stage="shadow", actor="pytest-m4-admin",
                actor_user_id=1,
                shadow_approval={
                    "approved_by_user_id": 1,
                    "approved_by": "pytest-m4-admin",
                    "reason": "pytest shadow evaluation",
                    "dataset_version": model["dataset_id"],
                    "evaluation_report_ref": f"ml_models:{model['model_id']}:evaluation_report",
                    "artifact_checksum": model["artifact_hash"],
                    "feature_set_version": FEATURE_SET_VERSION,
                    "intended_scope": "all_pipelines",
                    "rollback_target": "stop shadow; rules remain the decision system",
                })
        from backend.ml.inference_service import inference_service
        inference_service.invalidate()
        return shadowed
    return run_async(_run())


def _stop_shadow_and_cleanup():
    async def _run():
        from db_connection import db_manager
        from backend.ml.registry_service import registry_service
        from backend.ml.inference_service import inference_service
        from sqlalchemy import text as sa_text
        import os
        await _ensure_db()
        async with db_manager.get_session() as db:
            await registry_service.stop_shadow(
                db, "behavior_anomaly_model", actor="pytest-m4-admin",
                reason="pytest teardown")
            paths = (await db.execute(sa_text(
                "SELECT artifact_path FROM ml_models WHERE training_job_id LIKE :p"),
                {"p": JOB_PREFIX + "%"})).scalars().all()
            # prediction history names its model/threshold (RESTRICT): test
            # residue goes first, then the models (thresholds cascade)
            for table in ("ml_shadow_comparisons", "ml_predictions"):
                await db.execute(sa_text(
                    f"DELETE FROM {table} WHERE model_id IN "
                    "(SELECT id FROM ml_models WHERE training_job_id LIKE :p)"),
                    {"p": JOB_PREFIX + "%"})
            await db.execute(sa_text(
                "DELETE FROM ml_models WHERE training_job_id LIKE :p"),
                {"p": JOB_PREFIX + "%"})
            dataset_paths = (await db.execute(sa_text(
                "SELECT storage_path FROM ml_datasets WHERE build_job_id LIKE :p"),
                {"p": JOB_PREFIX + "%"})).scalars().all()
            await db.execute(sa_text(
                "DELETE FROM ml_datasets WHERE build_job_id LIKE :p"),
                {"p": JOB_PREFIX + "%"})
            await db.execute(sa_text(
                "DELETE FROM background_task_history WHERE job_id LIKE :p"),
                {"p": JOB_PREFIX + "%"})
            # Remove the subject this module seeds when the database is empty.
            await db.execute(sa_text(
                "DELETE FROM threat_assessments WHERE person_id IN "
                "(SELECT id FROM identities WHERE display_name = :n)"),
                {"n": SEEDED_SUBJECT_NAME})
            await db.execute(sa_text(
                "DELETE FROM identities WHERE display_name = :n"),
                {"n": SEEDED_SUBJECT_NAME})
            # Corpus residue (identities/appearances/snapshots seeded so the
            # trainer has real feature snapshots to build from).
            await db.execute(sa_text(
                "DELETE FROM ml_feature_snapshots WHERE entity_id IN "
                "(SELECT id::text FROM identities WHERE display_name LIKE :p)"),
                {"p": JOB_PREFIX + "%"})
            await db.execute(sa_text(
                "DELETE FROM identity_appearances WHERE identity_id IN "
                "(SELECT id FROM identities WHERE display_name LIKE :p)"),
                {"p": JOB_PREFIX + "%"})
            await db.execute(sa_text(
                "DELETE FROM identities WHERE display_name LIKE :p"),
                {"p": JOB_PREFIX + "%"})
            await db.execute(sa_text(
                "DELETE FROM identity_appearances WHERE pipeline_id LIKE :p"),
                {"p": JOB_PREFIX + "%"})
            await db.execute(sa_text(
                "DELETE FROM pipelines WHERE pipeline_id LIKE :p"),
                {"p": JOB_PREFIX + "%"})
            await db.commit()
        inference_service.invalidate()
        # Bump the shared Redis marker AFTER the deletes so the SERVER
        # process reloads and sees the post-delete registry state (otherwise
        # its cache may briefly reference a deleted model row).
        try:
            from backend.core.cache_manager import cache_manager
            if getattr(cache_manager, "redis_client", None) is None:
                await cache_manager.initialize()
            client = getattr(cache_manager, "redis_client", None)
            if client is not None:
                await client.incr("fr:ml:active_version:behavior_anomaly_model")
        except Exception:
            pass
        for path in list(paths) + list(dataset_paths):
            if path and os.path.exists(path):
                os.remove(path)
    run_async(_run())


# ---------------------------------------------------------------------------
# RULES default — byte-compatible live behavior
# ---------------------------------------------------------------------------

def test_rules_default_live_contract(token):
    identity = _get_identity(token)
    status, body = _http("GET", f"/api/security/threat/{identity}", token=token, timeout=120)
    assert status == 200, body
    for key in ("identity_id", "display_name", "overall_risk_score", "risk_factors",
                "threat_level", "severity", "recommendations", "last_assessed",
                "algorithm_version", "score_type", "is_probability",
                "calibration_status", "engine"):
        assert key in body, f"legacy key {key} missing"
    assert body["score_type"] == "heuristic"
    assert body["is_probability"] is False
    assert body["decision"]["requested_mode"] == "rules"
    assert body["decision"]["actual_mode_used"] == "rules"


def test_mode_availability_reports_exact_gate_reasons():
    async def _run():
        from db_connection import db_manager
        from backend.ml.decision_service import decision_service
        await _ensure_db()
        async with db_manager.get_session() as db:
            return await decision_service.mode_availability(db)
    availability = run_async(_run())
    modes = availability["modes"]
    assert modes["rules"]["available"] is True
    assert modes["hybrid"]["available"] is False
    assert modes["ml"]["available"] is False
    assert any("release gate" in r for r in modes["hybrid"]["reasons"])
    # HYBRID still states the exact label shortfall; ML is now gated by the
    # validated signal-mapping policy (decision router), not by labels.
    assert any("reviewed labels insufficient" in r for r in modes["hybrid"]["reasons"]), (
        "the exact label shortfall must be stated")
    assert any("signal mapping" in r for r in modes["ml"]["reasons"]), (
        "ML must name the missing validated mapping policy")


def test_gated_modes_resolve_to_rules_with_recorded_reasons():
    """In-process: force hybrid/ml and confirm decide() serves rules with
    MODE_GATED + the exact reasons (the server process stays on rules)."""
    async def _run():
        from db_connection import db_manager
        from backend.ml.decision_service import decision_service
        from config import settings
        await _ensure_db()
        original = settings.ML_DECISION_MODE
        outcomes = {}
        try:
            async with db_manager.get_session() as db:
                from sqlalchemy import text as sa_text
                identity = str((await db.execute(sa_text(
                    "SELECT id FROM identities ORDER BY last_seen_at DESC NULLS LAST LIMIT 1"))).scalar())
                for mode in ("hybrid", "ml"):
                    settings.ML_DECISION_MODE = mode
                    outcome = await decision_service.decide(db, identity)
                    outcomes[mode] = outcome
        finally:
            settings.ML_DECISION_MODE = original
        return outcomes
    outcomes = run_async(_run())
    for mode, outcome in outcomes.items():
        assert outcome.actual_mode_used == "rules", mode
        if mode == "hybrid":
            assert outcome.fallback_reason == "MODE_GATED"
            assert outcome.gate_reasons, f"{mode} must record its unmet gates"
        else:
            # ML is routed per request now: without a validated mapping policy
            # it falls back with the exact reason, never a generic gate
            assert outcome.fallback_reason == "SIGNAL_MAPPING_UNVALIDATED"
            assert outcome.provenance.fallback is True
        assert outcome.assessment.overall_risk_score is not None


# ---------------------------------------------------------------------------
# SHADOW — live result untouched, comparisons persisted
# ---------------------------------------------------------------------------

def test_shadow_mode_never_changes_the_live_result(token):
    identity = _get_identity(token)
    # Baseline in rules mode
    status, rules_body = _http(f"GET", f"/api/security/threat/{identity}",
                               token=token, timeout=120)
    assert status == 200

    _train_and_shadow()
    _set_mode(token, "shadow", "pytest shadow drill")
    try:
        status, shadow_body = _http(f"GET", f"/api/security/threat/{identity}",
                                    token=token, timeout=120)
        assert status == 200, shadow_body
        # The LIVE decision fields are identical to rules mode.
        for key in ("overall_risk_score", "threat_level", "severity",
                    "score_type", "is_probability", "risk_factors"):
            assert shadow_body[key] == rules_body[key], (
                f"SHADOW changed live field {key}")
        assert shadow_body["decision"]["requested_mode"] == "shadow"
        assert "does not affect" in shadow_body["decision"]["note"].replace(
            "without affecting", "does not affect"), shadow_body["decision"]

        # Comparison rows persisted with the separate-concepts vocabulary
        async def _rows():
            from db_connection import db_manager
            from sqlalchemy import text as sa_text
            await _ensure_db()
            async with db_manager.get_session() as db:
                return (await db.execute(sa_text(
                    "SELECT rule_threat_score, rule_threat_severity, "
                    " behavioral_anomaly_score, ml_anomaly_band, rule_would_alert, "
                    " ml_would_flag_anomaly, operational_disagreement, ml_failed "
                    "FROM ml_shadow_comparisons WHERE subject_id = :s "
                    "ORDER BY created_at DESC LIMIT 1"), {"s": identity})).one_or_none()
        row = run_async(_rows())
        assert row is not None, "shadow must persist a comparison row"
        assert row[3] in ("normal", "elevated", "unusual", "highly_unusual", None)
        assert row[6] in ("both_flagged", "rules_only", "anomaly_only", "neither")
    finally:
        _set_mode(token, "rules", "pytest restore")
        _stop_shadow_and_cleanup()


def test_shadow_failure_never_breaks_rules(token):
    """Shadow mode with NO shadow model: the live endpoint still serves the
    rules result; the failed attempt is persisted as data."""
    identity = _get_identity(token)
    _set_mode(token, "shadow", "pytest failure isolation")
    try:
        status, body = _http(f"GET", f"/api/security/threat/{identity}",
                             token=token, timeout=120)
        assert status == 200, "a missing shadow model must never break the read"
        assert body["score_type"] == "heuristic"

        async def _recent_fallback():
            from db_connection import db_manager
            from sqlalchemy import text as sa_text
            await _ensure_db()
            async with db_manager.get_session() as db:
                return (await db.execute(sa_text(
                    "SELECT fallback_reason, actual_mode_used FROM ml_predictions "
                    "WHERE subject_id = :s AND created_at > now() - interval '2 minutes' "
                    "AND fallback_reason = 'NO_APPROVED_MODEL' LIMIT 1"),
                    {"s": identity})).one_or_none()
        row = run_async(_recent_fallback())
        assert row is not None, ("the no-model shadow attempt must persist an "
                                 "honest NO_APPROVED_MODEL fallback row")
        assert row[1] == "rules"
    finally:
        _set_mode(token, "rules", "pytest restore")


def test_no_score_diff_anywhere():
    """Approval correction: raw score differences between threat scores and
    anomaly scores must not exist — not in the schema, not in the services."""
    import db_models
    cols = [c.name for c in db_models.MLShadowComparison.__table__.columns]
    assert "score_diff" not in cols and "severity_diff" not in cols
    for path in ("/app/backend/ml/shadow_service.py",
                 "/app/backend/ml/decision_service.py"):
        with open(path, encoding="utf-8") as f:
            src = f.read()
        code = "\n".join(line for line in src.splitlines()
                         if not line.lstrip().startswith("#"))
        assert "score_diff" not in code
        assert "rule_score -" not in code and "- rule_score" not in code, (
            f"numeric differencing of different concepts in {path}")


def test_operational_disagreement_vocabulary():
    from backend.ml.shadow_service import operational_disagreement
    assert operational_disagreement(True, True) == "both_flagged"
    assert operational_disagreement(True, False) == "rules_only"
    assert operational_disagreement(False, True) == "anomaly_only"
    assert operational_disagreement(False, False) == "neither"
    assert operational_disagreement(False, None) == "neither"
    assert operational_disagreement(True, None) == "rules_only"


# ---------------------------------------------------------------------------
# Fallback honesty + idempotency + tamper
# ---------------------------------------------------------------------------

def test_prediction_timeout_falls_back_honestly():
    async def _run():
        from db_connection import db_manager
        from backend.ml.inference_service import inference_service
        from config import settings
        await _ensure_db()
        original = settings.ML_INFERENCE_TIMEOUT_MS
        settings.ML_INFERENCE_TIMEOUT_MS = 1  # 1ms — nothing completes
        try:
            async with db_manager.get_session() as db:
                from sqlalchemy import text as sa_text
                identity = str((await db.execute(sa_text(
                    "SELECT id FROM identities LIMIT 1"))).scalar())
                return await inference_service.predict_identity(db, identity)
        finally:
            settings.ML_INFERENCE_TIMEOUT_MS = original
    result = run_async(_run())
    assert result.ok is False
    assert result.failure_reason in ("PREDICTION_TIMEOUT", "NO_APPROVED_MODEL")


def test_tampered_artifact_is_never_served():
    shadowed = _train_and_shadow()
    try:
        async def _run():
            from db_connection import db_manager
            from backend.ml.inference_service import inference_service
            from sqlalchemy import text as sa_text
            await _ensure_db()
            async with db_manager.get_session() as db:
                path = (await db.execute(sa_text(
                    "SELECT artifact_path FROM ml_models WHERE id = :i"),
                    {"i": shadowed["id"]})).scalar()
                identity = str((await db.execute(sa_text(
                    "SELECT id FROM identities LIMIT 1"))).scalar())
                with open(path, "rb") as f:
                    original = f.read()
                try:
                    with open(path, "wb") as f:
                        f.write(original[:-1] + bytes([original[-1] ^ 0xFF]))
                    inference_service.invalidate()
                    return await inference_service.predict_identity(db, identity)
                finally:
                    with open(path, "wb") as f:
                        f.write(original)
                    inference_service.invalidate()
        result = run_async(_run())
        assert result.ok is False
        assert result.failure_reason == "ARTIFACT_HASH_MISMATCH", result.failure_reason
    finally:
        _stop_shadow_and_cleanup()


def test_prediction_persistence_is_idempotent():
    async def _run():
        from db_connection import db_manager
        from backend.ml.inference_service import (
            MLPredictionResult, inference_service)
        await _ensure_db()
        result = MLPredictionResult(
            ok=False, failure_reason="NO_APPROVED_MODEL",
            model_version_label="pytest-idem-v0")
        subject = "pytest-mlm4-" + uuid_mod.uuid4().hex[:8]
        async with db_manager.get_session() as db:
            first = await inference_service.persist_prediction(
                db, result, identity_id=subject,
                requested_mode="shadow", actual_mode_used="rules")
            second = await inference_service.persist_prediction(
                db, result, identity_id=subject,
                requested_mode="shadow", actual_mode_used="rules")
            from sqlalchemy import text as sa_text
            await db.execute(sa_text(
                "DELETE FROM ml_predictions WHERE subject_id = :s"), {"s": subject})
            await db.commit()
        return first, second
    first, second = run_async(_run())
    assert first is not None and first == second, (
        "same subject+model+mode+window must collapse onto one prediction row")


def test_full_vector_sampling_disabled_by_default():
    from config import settings
    assert settings.ML_FEATURE_SAMPLED_FULL_VECTOR_RATE == 0.0

    async def _run():
        from db_connection import db_manager
        from sqlalchemy import text as sa_text
        await _ensure_db()
        async with db_manager.get_session() as db:
            return (await db.execute(sa_text(
                "SELECT count(*) FROM ml_predictions WHERE full_features IS NOT NULL"
            ))).scalar()
    assert run_async(_run()) == 0, (
        "no prediction may carry a full feature vector while sampling is 0.0")

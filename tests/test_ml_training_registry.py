"""
ML Milestone 3: training, registry lifecycle, artifact security.
================================================================
Run INSIDE the api container against the live app:

    docker exec face_recognition_api python -m pytest tests/test_ml_training_registry.py -v

Pins: unsupervised training end-to-end on real data with ONLY truthful
metrics; the structured supervised refusal; VALIDATED->SHADOW strictly via
complete admin approval (checksum-bound); the anomaly shadow cap in the
transition graph; artifact tamper/traversal/schema refusals; one-shadow
invariant; shadow-stop rollback; path-free serialization; single-flight.
"""

import json
import os
import uuid as uuid_mod
from datetime import datetime

import pytest

from backend.ml.constants import FEATURE_SET_VERSION

from conftest import run_on_shared_loop as run_async

JOB_PREFIX = "pytest-mlt-"


async def _ensure_db():
    from db_connection import db_manager
    if not getattr(db_manager, "_initialized", False):
        await db_manager.init_db()




async def _seed_snapshot_corpus(db, prefix, cam, count=20):
    """Seed identities + appearances and run the REAL collector over them.

    build_dataset/train read ml_feature_snapshots; before the demo-data wipe
    these suites silently trained over ~100 ambient demo snapshots. A suite
    that needs snapshots must make them - through run_collection, the same
    path production uses, never by inserting synthetic snapshot rows.

    Same shape as test_ml_decision_modes._seed_snapshot_corpus, for the same
    two reasons discovered there:

      * the corpus must be VARIED — identical identities give the isolation
        forest a zero-std score distribution, which fails the
        score_distribution_nondegenerate gate and yields NaN metrics;
      * the corpus must SPAN TIME — temporal_group_split cuts chronologically
        (earliest ~60% is train), so rows bunched into a few days land almost
        entirely in the holdout and trip minimum_train_rows (20).

    The old version here had both defects and passed only while ANOTHER
    module's crashed run left its snapshots behind to train over — the exact
    inter-test dependency this suite is not allowed to have.
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
        # First sighting 9-24 days back, so the corpus covers the window.
        first_day = 24 - (index % 6) * 3
        stamps = []
        for visit in range(visits):
            # Even identities visit at night (02:00-04:00), odd ones in the
            # day (09:00-15:00), so night_count_30d genuinely varies.
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


def _cleanup():
    async def _run():
        from db_connection import db_manager
        from sqlalchemy import text as sa_text
        await _ensure_db()
        async with db_manager.get_session() as db:
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
            # Corpus residue (identities + snapshots seeded for training).
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
        for path in list(paths) + list(dataset_paths):
            if path and os.path.exists(path):
                os.remove(path)
    run_async(_run())


@pytest.fixture(scope="module", autouse=True)
def cleanup_module():
    _cleanup()

    async def _seed():
        from db_connection import db_manager
        await _ensure_db()
        async with db_manager.get_session() as db:
            await _seed_snapshot_corpus(db, JOB_PREFIX, JOB_PREFIX + "cam")
    run_async(_seed())
    yield
    _cleanup()


def _train(job_id, algorithm="isolation_forest"):
    async def _run():
        from db_connection import db_manager
        from backend.core.task_history import task_history_manager
        from backend.ml import trainer
        await _ensure_db()
        assert trainer.try_acquire_training(job_id) is None
        await task_history_manager.create_job(
            job_id=job_id, task_type="ml_training",
            task_name="pytest training", description="pytest")
        await trainer.run_training_job(job_id, algorithm=algorithm)
        return await task_history_manager.get_task_by_job_id(job_id)
    return run_async(_run())


@pytest.fixture(scope="module")
def trained_model():
    task = _train(JOB_PREFIX + uuid_mod.uuid4().hex[:8])
    assert task["status"] == "completed", task
    return task["result"]


# ---------------------------------------------------------------------------
# Training honesty
# ---------------------------------------------------------------------------

def test_unsupervised_training_produces_validated_candidate(trained_model):
    assert trained_model["stage"] == "validated"
    assert trained_model["quality_gates"]["passed"] is True
    assert trained_model["awaiting_shadow_approval"] is True, (
        "a validated model AWAITS approval — it never auto-enters shadow")


def test_anomaly_metrics_are_honest(trained_model):
    evaluation = trained_model["evaluation"]
    assert evaluation["score_type"] == "anomaly_score"
    assert evaluation["is_probability"] is False
    assert evaluation["calibration_status"] == "not_applicable"
    dumped = json.dumps(evaluation)
    for fabricated in ("precision", "recall", "auc", "f1"):
        assert f'"{fabricated}"' not in dumped, (
            f"an unlabeled anomaly model must not report {fabricated}")
    # Empty splits are declared, not faked
    for split_name, split in evaluation["splits"].items():
        if split.get("rows", 0) == 0:
            assert split.get("insufficient_data") is True, split_name
    # Descriptive crosstab carries the different-concepts note
    assert "different concepts" in evaluation["rule_severity_crosstab"]["note"]


def test_bands_are_anomaly_vocabulary_not_threat_severity(trained_model):
    """Bands are anomaly vocabulary. Rule-severity terms may appear ONLY
    inside the descriptive crosstab (the approved side-by-side view) — never
    in the model's own scores, bands, or splits."""
    evaluation = dict(trained_model["evaluation"])
    cutpoints = evaluation["band_cutpoints"]
    assert set(cutpoints) == {"elevated", "unusual", "highly_unusual"}
    crosstab = evaluation.pop("rule_severity_crosstab")
    assert "different concepts" in crosstab["note"]
    dumped = json.dumps(evaluation)
    for severity in ('"low"', '"moderate"', '"critical"'):
        assert severity not in dumped, (
            "threat-severity vocabulary outside the crosstab: " + severity)
    for split in evaluation["splits"].values():
        for band in (split.get("band_counts") or {}):
            assert band in ("normal", "elevated", "unusual", "highly_unusual")


def test_supervised_training_returns_structured_refusal():
    job_id = JOB_PREFIX + uuid_mod.uuid4().hex[:8]
    async def _go():
        from backend.core.task_history import task_history_manager
        from backend.ml import trainer
        await _ensure_db()
        await task_history_manager.create_job(
            job_id=job_id, task_type="ml_training", task_name="pytest threat rank",
            description="pytest reviewed-label gate")
        await trainer.run_training_job(
            job_id, model_type="threat_ranking_model", algorithm="logreg")
        return await task_history_manager.get_task_by_job_id(job_id)
    task = run_async(_go())
    assert task["status"] == "failed"
    assert task["error_code"] == "INSUFFICIENT_REVIEWED_LABELS"
    refusal = json.loads(task["error_message"])
    assert refusal["status"] == "blocked"
    assert refusal["reason"] == "INSUFFICIENT_REVIEWED_LABELS"
    for key in ("required_total", "required_per_class", "available_total",
                "available_positive", "available_negative"):
        assert key in refusal, refusal


def test_training_single_flight():
    from backend.ml import trainer
    job = JOB_PREFIX + "flight"
    assert trainer.try_acquire_training(job) is None
    assert trainer.try_acquire_training(JOB_PREFIX + "flight2") == job
    assert trainer.request_cancel(job) is True
    trainer.release_training(job)
    assert trainer.running_training_job() is None


# ---------------------------------------------------------------------------
# Shadow approval (correction D) + transition graph (correction B)
# ---------------------------------------------------------------------------

def _approval_for(model, **overrides):
    approval = {
        "approved_by_user_id": 1,
        "approved_by": "pytest-admin",
        "reason": "pytest shadow evaluation",
        "dataset_version": model["dataset_id"],
        "evaluation_report_ref": f"ml_models:{model['model_id']}:evaluation_report",
        "artifact_checksum": model["artifact_hash"],
        "feature_set_version": FEATURE_SET_VERSION,
        "intended_scope": "all_pipelines",
        "rollback_target": "stop shadow + return to rules-only observation",
    }
    approval.update(overrides)
    return approval


def test_shadow_requires_complete_admin_approval(trained_model):
    async def _run():
        from db_connection import db_manager
        from backend.ml.registry_service import registry_service, RegistryError
        await _ensure_db()
        outcomes = {}
        async with db_manager.get_session() as db:
            try:
                await registry_service.transition(
                    db, trained_model["model_id"], to_stage="shadow",
                    actor="pytest-admin")
                outcomes["no_approval"] = "ACCEPTED"
            except RegistryError as e:
                outcomes["no_approval"] = e.code
            try:
                await registry_service.transition(
                    db, trained_model["model_id"], to_stage="shadow",
                    actor="pytest-admin",
                    shadow_approval=_approval_for(trained_model,
                                                  artifact_checksum="0" * 64))
                outcomes["wrong_checksum"] = "ACCEPTED"
            except RegistryError as e:
                outcomes["wrong_checksum"] = e.code
        return outcomes
    outcomes = run_async(_run())
    assert outcomes["no_approval"] == "SHADOW_APPROVAL_INCOMPLETE"
    assert outcomes["wrong_checksum"] == "SHADOW_APPROVAL_CHECKSUM_MISMATCH"


def test_shadow_entry_and_one_shadow_invariant(trained_model):
    """A complete approval enters shadow with the payload persisted; a
    SECOND model entering shadow archives the first (one shadow per type)."""
    second = _train(JOB_PREFIX + uuid_mod.uuid4().hex[:8])["result"]

    async def _run():
        from db_connection import db_manager
        from backend.ml.registry_service import registry_service
        from sqlalchemy import text as sa_text
        await _ensure_db()
        async with db_manager.get_session() as db:
            first_shadow = await registry_service.transition(
                db, trained_model["model_id"], to_stage="shadow",
                actor="pytest-admin",
                shadow_approval=_approval_for(trained_model))
            second_shadow = await registry_service.transition(
                db, second["model_id"], to_stage="shadow",
                actor="pytest-admin", shadow_approval=_approval_for(second))
            stages = dict((await db.execute(sa_text(
                "SELECT id::text, stage FROM ml_models WHERE id IN (:a, :b)"),
                {"a": trained_model["model_id"], "b": second["model_id"]})).all())
        return first_shadow, second_shadow, stages
    first_shadow, second_shadow, stages = run_async(_run())
    approval = first_shadow["shadow_approval"]
    for field in ("approved_by_user_id", "approved_by", "reason", "approved_at",
                  "dataset_version", "evaluation_report_ref", "artifact_checksum",
                  "feature_set_version", "intended_scope", "rollback_target"):
        assert approval.get(field), f"shadow approval must persist {field}"
    assert stages[trained_model["model_id"]] == "archived", (
        "entering a second shadow must archive the first")
    assert stages[second["model_id"]] == "shadow"


def test_anomaly_model_cannot_reach_approved_or_production(trained_model):
    async def _run():
        from db_connection import db_manager
        from backend.ml.registry_service import registry_service, RegistryError
        await _ensure_db()
        outcomes = {}
        async with db_manager.get_session() as db:
            for target in ("approved", "production"):
                try:
                    await registry_service.transition(
                        db, trained_model["model_id"], to_stage=target,
                        actor="pytest-admin", reason="pytest")
                    outcomes[target] = "ACCEPTED"
                except RegistryError as e:
                    outcomes[target] = e.code
        return outcomes
    outcomes = run_async(_run())
    for target, code in outcomes.items():
        assert code in ("INVALID_TRANSITION", "ANOMALY_SHADOW_CAP"), (
            f"anomaly model reached {target}: {code}")


def test_stop_shadow_is_the_rollback(trained_model):
    async def _run():
        from db_connection import db_manager
        from backend.ml.registry_service import registry_service
        from sqlalchemy import text as sa_text
        await _ensure_db()
        async with db_manager.get_session() as db:
            stopped = await registry_service.stop_shadow(
                db, "behavior_anomaly_model", actor="pytest-admin",
                reason="pytest rollback drill")
            remaining = (await db.execute(sa_text(
                "SELECT count(*) FROM ml_models WHERE model_type='behavior_anomaly_model' "
                "AND stage='shadow' AND training_job_id LIKE :p"),
                {"p": JOB_PREFIX + "%"})).scalar()
        return stopped, remaining
    stopped, remaining = run_async(_run())
    assert stopped is not None and stopped["stage"] == "archived"
    assert remaining == 0, "rollback must leave no pytest shadow model running"


# ---------------------------------------------------------------------------
# Artifact security
# ---------------------------------------------------------------------------

def test_artifact_tamper_is_refused(trained_model):
    async def _run():
        from db_connection import db_manager
        from backend.ml.registry_service import validate_artifact, RegistryError
        from sqlalchemy import text as sa_text
        await _ensure_db()
        async with db_manager.get_session() as db:
            path, expected_hash, names, deps = (await db.execute(sa_text(
                "SELECT artifact_path, artifact_hash, feature_names, dependency_versions "
                "FROM ml_models WHERE id = :i"), {"i": trained_model["model_id"]})).one()
        with open(path, "rb") as f:
            original = f.read()
        try:
            with open(path, "wb") as f:   # flip one byte
                f.write(original[:-1] + bytes([original[-1] ^ 0xFF]))
            try:
                validate_artifact(path, expected_hash=expected_hash,
                                  expected_feature_names=names,
                                  expected_dependencies=deps)
                return "ACCEPTED"
            except RegistryError as e:
                return e.code
        finally:
            with open(path, "wb") as f:
                f.write(original)
    assert run_async(_run()) == "ARTIFACT_HASH_MISMATCH"


def test_artifact_path_traversal_refused():
    from backend.ml.registry_service import validate_artifact, RegistryError
    for evil in ("/etc/passwd", "models/ml/../../db_models.py",
                 "/tmp/external_model.pkl"):
        with pytest.raises(RegistryError) as excinfo:
            validate_artifact(evil, expected_hash="x",
                              expected_feature_names=[], expected_dependencies={})
        assert excinfo.value.code in ("ARTIFACT_PATH_INVALID", "ARTIFACT_MISSING"), evil
        # traversal specifically must be the PATH refusal, not a 'missing'
    with pytest.raises(RegistryError) as excinfo:
        validate_artifact("/etc/passwd", expected_hash="x",
                          expected_feature_names=[], expected_dependencies={})
    assert excinfo.value.code == "ARTIFACT_PATH_INVALID"


def test_artifact_schema_mismatch_refused(trained_model):
    async def _run():
        from db_connection import db_manager
        from backend.ml.registry_service import validate_artifact, RegistryError
        from sqlalchemy import text as sa_text
        await _ensure_db()
        async with db_manager.get_session() as db:
            path, expected_hash, deps = (await db.execute(sa_text(
                "SELECT artifact_path, artifact_hash, dependency_versions "
                "FROM ml_models WHERE id = :i"), {"i": trained_model["model_id"]})).one()
        try:
            validate_artifact(path, expected_hash=expected_hash,
                              expected_feature_names=["totally", "different", "schema"],
                              expected_dependencies=deps)
            return "ACCEPTED"
        except RegistryError as e:
            return e.code
    assert run_async(_run()) == "FEATURE_SCHEMA_MISMATCH"


def test_model_serialization_is_path_free(trained_model):
    async def _run():
        from db_connection import db_manager
        from backend.ml.registry_service import registry_service, serialize_model_row
        await _ensure_db()
        async with db_manager.get_session() as db:
            row = await registry_service.get_model(db, trained_model["model_id"])
            return serialize_model_row(row)
    payload = run_async(_run())
    dumped = json.dumps(payload)
    assert "artifact_path" not in dumped
    assert "models/ml" not in dumped and "/app/" not in dumped
    assert payload["artifact_hash"], "hash stays visible; path never"


# ---------------------------------------------------------------------------
# Several validated candidates
# ---------------------------------------------------------------------------

def test_second_validated_candidate_does_not_break_stage_lookup():
    """Only the SHADOW stage is unique by schema (uq_ml_models_one_shadow).
    Every training run leaves one more VALIDATED candidate behind, so the
    stage lookup must tolerate several rows and answer with the newest one.
    It used scalar_one_or_none(), so the second training without an approval
    in between made GET /api/ml/overview answer 500 ("Multiple rows were
    found") — the ML-Ops page went dark exactly when an admin was comparing
    candidates."""
    # two fresh candidates of our own: earlier tests in this module walk the
    # shared `trained_model` through shadow and rollback, so it is archived
    first = _train(JOB_PREFIX + uuid_mod.uuid4().hex[:8])
    assert first["status"] == "completed", first
    second = _train(JOB_PREFIX + uuid_mod.uuid4().hex[:8])
    assert second["status"] == "completed", second
    second_id = second["result"]["model_id"]
    assert second_id != first["result"]["model_id"]

    async def _run():
        from db_connection import db_manager
        from sqlalchemy import text as sa_text
        from backend.ml.registry_service import registry_service
        await _ensure_db()
        async with db_manager.get_session() as db:
            validated_rows = (await db.execute(sa_text(
                "SELECT count(*) FROM ml_models WHERE model_type = 'behavior_anomaly_model' "
                "AND stage = 'validated'"))).scalar()
            newest = await registry_service.get_stage_model(
                db, "behavior_anomaly_model", "validated")
            shadow_rows = (await db.execute(sa_text(
                "SELECT count(*) FROM ml_models WHERE model_type = 'behavior_anomaly_model' "
                "AND stage = 'shadow'"))).scalar()
            return validated_rows, newest, shadow_rows
    validated_rows, newest, shadow_rows = run_async(_run())
    assert validated_rows >= 2, "the precondition is two validated candidates"
    assert newest is not None and str(newest.id) == second_id, (
        "with several validated candidates the lookup answers with the newest")
    assert shadow_rows <= 1, "the one-shadow invariant is untouched"

    # the page that died: the overview through the real route, both stage
    # slots answered (validated = newest candidate, shadow = at most one)
    import urllib.request
    import urllib.error
    base = os.environ.get("REGRESSION_BASE_URL", "http://localhost:8000")
    req = urllib.request.Request(
        base + "/api/auth/login", method="POST",
        data=json.dumps({"username": "admin", "password": "admin123"}).encode(),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        bearer = json.loads(r.read())["access_token"]
    req = urllib.request.Request(base + "/api/ml/overview",
                                 headers={"Authorization": f"Bearer {bearer}"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            status, body = r.status, json.loads(r.read())
    except urllib.error.HTTPError as exc:
        status, body = exc.code, exc.read()[:300]
    assert status == 200, f"overview must survive two validated candidates: {status} {body}"
    assert body["models"]["validated_candidate"]["id"] == second_id

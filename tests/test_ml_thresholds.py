"""
ML threshold SETS (plan §9, migration d4e5f6a7b8c9): one row = one cutpoint set
per (model, scope, version); lifecycle candidate → active → retired; the DB
carries the invariants and backend/ml/threshold_service.py serialises writers
per model with a transaction-scoped advisory lock.

    uniqueness   uq_ml_threshold_scope_version, uq_ml_threshold_one_active
    scope        ck_ml_threshold_scope_canonical  ('global' ⇔ scope_id = '')
    payload      ck_ml_threshold_cutpoints (keys + order), ck_ml_threshold_status, ck_ml_threshold_source
    concurrency  8 concurrent create_candidate → versions 1..8; 8 concurrent
                 activate → exactly one active, exactly one activate audit row
    round-trip   create → activate → GET /api/ml/models/{id} — every field of the
                 API set equals the DB row (migration → ORM → service → API agree)

    docker exec face_recognition_api python -m pytest tests/test_ml_thresholds.py -q
"""
import asyncio
import json
import urllib.error
import urllib.request
import uuid

import pytest
from sqlalchemy import text

from conftest import run_on_shared_loop as run_async

BASE = "http://localhost:8000"
CUT = {"elevated": 0.55, "unusual": 0.7, "highly_unusual": 0.85}
COLUMNS = {"id", "model_id", "scope_type", "scope_id", "version", "status", "cutpoints", "quantiles", "source",
           "expected_metrics", "sample_count", "created_at", "activated_at", "activated_by", "retired_at",
           "retired_by", "notes"}
CONSTRAINTS = {"uq_ml_threshold_scope_version", "uq_ml_threshold_one_active", "ck_ml_threshold_scope_canonical",
               "ck_ml_threshold_status", "ck_ml_threshold_cutpoints", "ck_ml_threshold_source"}


async def _db():
    from db_connection import db_manager
    if not getattr(db_manager, "_initialized", False):
        await db_manager.init_db()
    return db_manager


def _sql(statement, params=None, fetch="all"):
    async def _run():
        dm = await _db()
        async with dm.get_session() as db:
            res = await db.execute(text(statement), params or {})
            if not res.returns_rows:
                await db.commit()
                return res.rowcount
            value = res.scalar() if fetch == "scalar" else res.all()
            await db.commit()
            return value
    return run_async(_run())


def _new_model(tag):
    mid = str(uuid.uuid4())
    _sql("INSERT INTO ml_models (id, model_type, version, stage, algorithm, model_purpose, score_type, is_probability, "
         " calibration_status, artifact_name, artifact_path, artifact_hash, dependency_versions, feature_set_version, "
         " feature_names, created_at) VALUES (CAST(:i AS uuid), 'behavior_anomaly_model', :v, 'candidate', "
         " 'isolation_forest', 'behavioral_anomaly_detection', 'anomaly_score', false, 'not_applicable', "
         " :n, :p, :h, '{}', 'v1', '[]', now())",
         {"i": mid, "v": 90000 + abs(hash(tag)) % 9000, "n": f"qa-thr-{tag}-{mid[:8]}",
          "p": f"/tmp/qa-thr-{mid[:8]}", "h": mid.replace("-", "")})
    return mid


def _drop_model(mid):
    _sql("DELETE FROM ml_audit_log WHERE object_type = 'ml_model_threshold' AND object_id IN "
         "(SELECT id::text FROM ml_model_thresholds WHERE model_id = CAST(:i AS uuid))", {"i": mid})
    _sql("DELETE FROM ml_model_thresholds WHERE model_id = CAST(:i AS uuid)", {"i": mid})
    _sql("DELETE FROM ml_models WHERE id = CAST(:i AS uuid)", {"i": mid})


@pytest.fixture
def model():
    mid = _new_model("m")
    try:
        yield mid
    finally:
        _drop_model(mid)


def _raw_insert(mid, *, version=1, status="candidate", scope_type="global", scope_id="", cutpoints=CUT, source="training"):
    return _sql("INSERT INTO ml_model_thresholds (id, model_id, scope_type, scope_id, version, status, cutpoints, source, "
                " sample_count, created_at) VALUES (gen_random_uuid(), CAST(:m AS uuid), :t, :s, :v, :st, "
                " CAST(:c AS jsonb), :src, 0, now())",
                {"m": mid, "t": scope_type, "s": scope_id, "v": version, "st": status, "c": json.dumps(cutpoints),
                 "src": source})


def _refused(fn, name):
    with pytest.raises(Exception) as exc:
        fn()
    assert name in str(exc.value), f"expected {name}, got: {str(exc.value)[:300]}"


# ---------------------------------------------------------------- schema

def test_live_schema_has_the_final_columns_and_all_six_constraints():
    cols = {r[0] for r in _sql("SELECT column_name FROM information_schema.columns WHERE table_name = 'ml_model_thresholds'")}
    assert cols == COLUMNS, cols ^ COLUMNS
    assert "threshold" not in cols and "objective" not in cols
    names = {r[0] for r in _sql("SELECT conname FROM pg_constraint WHERE conrelid = 'ml_model_thresholds'::regclass")}
    names |= {r[0] for r in _sql("SELECT indexname FROM pg_indexes WHERE tablename = 'ml_model_thresholds'")}
    assert CONSTRAINTS <= names, CONSTRAINTS - names
    assert _sql("SELECT is_nullable FROM information_schema.columns WHERE table_name = 'ml_model_thresholds' "
                "AND column_name = 'scope_id'", fetch="scalar") == "NO"


# ---------------------------------------------------------------- uniqueness / CHECKs

def test_two_global_sets_with_the_same_version_are_refused(model):
    _raw_insert(model, version=1)
    _refused(lambda: _raw_insert(model, version=1), "uq_ml_threshold_scope_version")


def test_two_active_global_sets_are_refused(model):
    _raw_insert(model, version=1, status="active")
    _refused(lambda: _raw_insert(model, version=2, status="active"), "uq_ml_threshold_one_active")


def test_same_version_or_active_on_another_model_or_scope_is_allowed(model):
    other = _new_model("o")
    try:
        _raw_insert(model, version=1, status="active")
        _raw_insert(other, version=1, status="active")                            # different model
        _raw_insert(model, version=1, status="active", scope_type="pipeline", scope_id="cam-1")   # different scope
        assert _sql("SELECT count(*) FROM ml_model_thresholds WHERE model_id IN (CAST(:a AS uuid), CAST(:b AS uuid))",
                    {"a": model, "b": other}, fetch="scalar") == 3
    finally:
        _drop_model(other)


def test_scope_representation_is_bidirectionally_canonical(model):
    _refused(lambda: _raw_insert(model, scope_type="global", scope_id="cam-1"), "ck_ml_threshold_scope_canonical")
    _refused(lambda: _raw_insert(model, scope_type="pipeline", scope_id=""), "ck_ml_threshold_scope_canonical")
    _raw_insert(model, scope_type="pipeline", scope_id="cam-1")                  # canonical non-global
    _raw_insert(model, scope_type="global", scope_id="")                         # canonical global


def test_cutpoints_status_and_source_checks(model):
    _refused(lambda: _raw_insert(model, cutpoints={"elevated": 0.9, "unusual": 0.7, "highly_unusual": 0.85}),
             "ck_ml_threshold_cutpoints")                                        # order
    _refused(lambda: _raw_insert(model, cutpoints={"elevated": 0.5, "unusual": 0.7}), "ck_ml_threshold_cutpoints")  # keys
    _refused(lambda: _raw_insert(model, status="shadow"), "ck_ml_threshold_status")
    _refused(lambda: _raw_insert(model, source="guess"), "ck_ml_threshold_source")


# ---------------------------------------------------------------- service concurrency

def _service():
    from backend.ml.threshold_service import threshold_service
    return threshold_service


def test_eight_concurrent_candidates_get_versions_1_to_8_without_gaps(model):
    svc = _service()

    async def one(i):
        dm = await _db()
        async with dm.get_session() as db:
            row = await svc.create_candidate(db, model_id=model, cutpoints=CUT, sample_count=i, source="training")
            await db.commit()
            return row.version

    async def go():
        return await asyncio.gather(*(one(i) for i in range(8)))
    versions = sorted(run_async(go()))
    assert versions == list(range(1, 9)), versions
    assert _sql("SELECT count(*) FROM ml_model_thresholds WHERE model_id = CAST(:m AS uuid)", {"m": model},
                fetch="scalar") == 8


def test_eight_concurrent_activations_yield_one_active_and_one_audit_row(model):
    svc = _service()

    async def seed():
        dm = await _db()
        async with dm.get_session() as db:
            await svc.create_candidate(db, model_id=model, cutpoints=CUT, source="training")
            await db.commit()
    run_async(seed())

    async def one(i):
        dm = await _db()
        async with dm.get_session() as db:
            row = await svc.activate_for_model(db, model_id=model, actor=f"qa{i}", reason="qa concurrent")
            await db.commit()
            return str(row.id), row.status

    async def go():
        return await asyncio.gather(*(one(i) for i in range(8)))
    results = run_async(go())
    assert {r[1] for r in results} == {"active"} and len({r[0] for r in results}) == 1, results
    active = _sql("SELECT id::text FROM ml_model_thresholds WHERE model_id = CAST(:m AS uuid) AND status = 'active'",
                  {"m": model})
    assert len(active) == 1
    audits = _sql("SELECT count(*) FROM ml_audit_log WHERE action = 'threshold_activate' AND object_id = :o",
                  {"o": active[0][0]}, fetch="scalar")
    assert audits == 1, f"expected exactly one threshold_activate audit row, found {audits}"


def test_activate_retires_the_previous_active_and_missing_candidate_is_named(model):
    svc = _service()

    async def go():
        dm = await _db()
        async with dm.get_session() as db:
            v1 = await svc.create_candidate(db, model_id=model, cutpoints=CUT, source="training")
            a1 = await svc.activate_for_model(db, model_id=model, actor="qa", reason="v1")
            v2 = await svc.create_candidate(db, model_id=model, cutpoints={"elevated": 0.6, "unusual": 0.75,
                                                                          "highly_unusual": 0.9}, source="manual")
            a2 = await svc.activate_for_model(db, model_id=model, actor="qa", reason="v2")
            out = (v1.version, a1.id, v2.version, a2.id)     # read before commit expires the rows
            await db.commit()
            return out
    v1, a1, v2, a2 = run_async(go())
    assert (v1, v2) == (1, 2) and a2 != a1
    rows = dict(_sql("SELECT version, status FROM ml_model_thresholds WHERE model_id = CAST(:m AS uuid)", {"m": model}))
    assert rows == {1: "retired", 2: "active"}, rows

    # idempotent no-op: with no candidate but an active set, activate returns the active row (no error)
    async def idem():
        dm = await _db()
        async with dm.get_session() as db:
            row = await svc.activate_for_model(db, model_id=model, actor="qa", reason="again")
            out = (row.version, row.status)                    # read before rollback expires the row
            await db.rollback()
            return out
    assert run_async(idem()) == (2, "active")
    fresh = _new_model("nocand")
    try:
        async def none():
            from backend.ml.registry_service import RegistryError
            dm = await _db()
            async with dm.get_session() as db:
                with pytest.raises(RegistryError) as exc:
                    await svc.activate_for_model(db, model_id=fresh, actor="qa", reason="x")
                await db.rollback()
                return exc.value.code
        assert run_async(none()) == "THRESHOLD_CANDIDATE_MISSING"
    finally:
        _drop_model(fresh)


def test_artifact_mismatch_is_refused_and_leaves_the_candidate_untouched(model):
    svc = _service()

    async def go():
        from backend.ml.registry_service import RegistryError
        dm = await _db()
        async with dm.get_session() as db:
            await svc.create_candidate(db, model_id=model, cutpoints=CUT, source="training")
            await db.commit()
        async with dm.get_session() as db:
            with pytest.raises(RegistryError) as exc:
                await svc.activate_for_model(db, model_id=model, actor="qa", reason="x",
                                             artifact_cutpoints={"elevated": 0.5, "unusual": 0.7, "highly_unusual": 0.85})
            await db.rollback()
            return exc.value.code
    assert run_async(go()) == "THRESHOLD_ARTIFACT_MISMATCH"
    assert _sql("SELECT status FROM ml_model_thresholds WHERE model_id = CAST(:m AS uuid)", {"m": model},
                fetch="scalar") == "candidate"


# ---------------------------------------------------------------- API round-trip

def _token():
    req = urllib.request.Request(BASE + "/api/auth/login",
                                 data=json.dumps({"username": "admin", "password": "admin123"}).encode(),
                                 method="POST", headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())["access_token"]


def test_fresh_db_threshold_service_api_round_trip(model):
    """migration → ORM → service → API: every field the API returns for a set
    equals the DB row (also runs, unchanged, inside the freshly migrated
    isolated regression stack)."""
    svc = _service()

    async def go():
        dm = await _db()
        async with dm.get_session() as db:
            await svc.create_candidate(db, model_id=model, cutpoints=CUT, quantiles={"p95": 0.7}, sample_count=42,
                                       source="training", expected_metrics={"precision": 0.9}, notes="qa round trip")
            row = await svc.activate_for_model(db, model_id=model, actor="qa-admin", reason="round trip")
            await db.commit()
            return str(row.id)
    tid = run_async(go())
    req = urllib.request.Request(f"{BASE}/api/ml/models/{model}", headers={"Authorization": f"Bearer {_token()}"})
    with urllib.request.urlopen(req, timeout=30) as r:
        assert r.status == 200
        payload = json.loads(r.read())
    sets = payload["thresholds"]
    assert len(sets) == 1 and sets[0]["id"] == tid, sets
    api = sets[0]
    db_row = _sql("SELECT id::text, model_id::text, scope_type, scope_id, version, status, cutpoints, quantiles, source, "
                  "expected_metrics, sample_count, created_at, activated_at, activated_by, retired_at, retired_by, notes "
                  "FROM ml_model_thresholds WHERE id = CAST(:i AS uuid)", {"i": tid})[0]
    (rid, rmodel, rscope_t, rscope_id, rver, rstatus, rcut, rquant, rsrc, rmetrics, rcount, rcreated, ractivated,
     ractor, rretired, rretired_by, rnotes) = db_row
    assert api["model_id"] == rmodel == model
    assert (api["scope_type"], api["scope_id"], api["version"], api["status"]) == (rscope_t, rscope_id, rver, rstatus) \
        == ("global", "", 1, "active")
    assert api["cutpoints"] == rcut == CUT and api["quantiles"] == rquant == {"p95": 0.7}
    assert api["source"] == rsrc == "training" and api["expected_metrics"] == rmetrics == {"precision": 0.9}
    assert api["sample_count"] == rcount == 42 and api["notes"] == rnotes == "qa round trip"
    assert api["activated_by"] == ractor == "qa-admin"
    assert api["created_at"] == rcreated.isoformat() + "Z" and api["activated_at"] == ractivated.isoformat() + "Z"
    assert api["retired_at"] is None and rretired is None and api["retired_by"] is None
    assert api["version_label"] == "global:global@v1"
    assert set(api) >= {"id", "model_id", "scope_type", "scope_id", "version", "status", "cutpoints", "quantiles",
                        "source", "sample_count", "created_at", "activated_at", "activated_by", "retired_at",
                        "retired_by", "notes", "expected_metrics"}
    # ml_models.threshold/objective never leak back in
    assert "threshold" not in api and "objective" not in api

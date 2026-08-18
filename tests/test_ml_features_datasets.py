"""
ML Milestone 2: point-in-time features, snapshots, validation, datasets.
========================================================================
Run INSIDE the api container against the live app:

    docker exec face_recognition_api python -m pytest tests/test_ml_features_datasets.py -v

Pins the leakage guarantees (as_of boundary, temporal+group split, holdout
integrity, target_adjacent exclusion), timezone correctness (Asia/Beirut),
graph readiness honesty (reasons, not zeros), snapshot idempotency, dataset
checksum stability and version immutability, and the no-biometrics rule.
"""

import uuid as uuid_mod
from datetime import datetime, timedelta

import pytest

from conftest import run_on_shared_loop as run_async

PREFIX = "pytest-mlfd-"
CAM_UTC = PREFIX + "cam-utc"
CAM_BEIRUT = PREFIX + "cam-beirut"


async def _ensure_db():
    from db_connection import db_manager
    if not getattr(db_manager, "_initialized", False):
        await db_manager.init_db()




async def _seed_snapshot_corpus(db, prefix, cam, count=14):
    """Seed identities + appearances and run the REAL collector over them.

    build_dataset/train read ml_feature_snapshots; before the demo-data wipe
    these suites silently trained over ~100 ambient demo snapshots. A suite
    that needs snapshots must make them - through run_collection, the same
    path production uses, never by inserting synthetic snapshot rows.
    `count` >= 14 keeps MIN_ROWS_UNSUPERVISED (10) satisfied with margin.
    """
    from datetime import datetime, timedelta

    from sqlalchemy import text as sa_text

    from backend.ml.collector import run_collection

    now = datetime.utcnow().replace(microsecond=0)
    await db.execute(sa_text(
        "INSERT INTO pipelines (pipeline_id, created_at, is_active) "
        "VALUES (:p, now(), 1) ON CONFLICT (pipeline_id) DO NOTHING"),
        {"p": cam})
    for index in range(count):
        identity_id = (await db.execute(sa_text(
            "INSERT INTO identities (id, type, status, display_name, first_seen_at, "
            " last_seen_at, created_at, updated_at, appearances_count) "
            "VALUES (gen_random_uuid(), 'UNKNOWN', 'ACTIVE', :n, :ts, :ts, now(), now(), 0) "
            "RETURNING id"),
            {"n": f"{prefix}corpus{index:02d}",
             "ts": now - timedelta(days=2)})).scalar()
        for day in (2, 3):
            await db.execute(sa_text(
                "INSERT INTO identity_appearances (identity_id, pipeline_id, start_time, created_at) "
                "VALUES (:i, :p, :ts, now())"),
                {"i": identity_id, "p": cam,
                 "ts": now - timedelta(days=day, minutes=index)})
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
            await db.execute(sa_text(
                "DELETE FROM ml_feature_snapshots WHERE entity_id IN "
                "(SELECT id::text FROM identities WHERE display_name LIKE :p)"),
                {"p": PREFIX + "%"})
            await db.execute(sa_text(
                "DELETE FROM ml_labels WHERE subject_id IN "
                "(SELECT id::text FROM identities WHERE display_name LIKE :p)"),
                {"p": PREFIX + "%"})
            await db.execute(sa_text(
                "DELETE FROM identity_appearances WHERE identity_id IN "
                "(SELECT id FROM identities WHERE display_name LIKE :p)"),
                {"p": PREFIX + "%"})
            await db.execute(sa_text(
                "DELETE FROM identities WHERE display_name LIKE :p"), {"p": PREFIX + "%"})
            await db.execute(sa_text(
                "DELETE FROM identity_appearances WHERE pipeline_id LIKE :p"),
                {"p": PREFIX + "cam-%"})
            await db.execute(sa_text(
                "DELETE FROM pipelines WHERE pipeline_id LIKE :p"), {"p": PREFIX + "cam-%"})
            await db.commit()
    run_async(_run())


@pytest.fixture(scope="module")
def seeded():
    _cleanup()

    async def _run():
        from db_connection import db_manager
        from sqlalchemy import text as sa_text
        await _ensure_db()
        now = datetime.utcnow().replace(microsecond=0)
        ids = {"now": now}
        async with db_manager.get_session() as db:
            await db.execute(sa_text(
                "INSERT INTO pipelines (pipeline_id, created_at, is_active) "
                "VALUES (:p, now(), 1) ON CONFLICT (pipeline_id) DO NOTHING"),
                {"p": CAM_UTC})
            await db.execute(sa_text(
                "INSERT INTO pipelines (pipeline_id, created_at, is_active, timezone) "
                "VALUES (:p, now(), 1, 'Asia/Beirut') ON CONFLICT (pipeline_id) DO NOTHING"),
                {"p": CAM_BEIRUT})

            async def ident(name):
                return str((await db.execute(sa_text(
                    "INSERT INTO identities (id, type, status, display_name, first_seen_at, "
                    " last_seen_at, created_at, updated_at, appearances_count) "
                    "VALUES (gen_random_uuid(), 'UNKNOWN', 'ACTIVE', :n, :ts, :ts, now(), now(), 0) "
                    "RETURNING id"), {"n": PREFIX + name, "ts": now})).scalar())

            async def appear(identity_id, cam, ts):
                await db.execute(sa_text(
                    "INSERT INTO identity_appearances (identity_id, pipeline_id, start_time, created_at) "
                    "VALUES (:i, :p, :ts, now())"), {"i": identity_id, "p": cam, "ts": ts})

            # Boundary identity: 5 appearances, one EXACTLY at the as_of cutoff
            ids["boundary"] = await ident("boundary")
            ids["boundary_asof"] = now - timedelta(days=1)
            for d in (5, 4, 3, 2):
                await appear(ids["boundary"], CAM_UTC, now - timedelta(days=d))
            await appear(ids["boundary"], CAM_UTC, ids["boundary_asof"])  # AT the cutoff

            # Beirut identity: 00:30 UTC rows = 02:30/03:30 local (off-hours
            # locally, NOT off-hours in UTC terms)
            ids["beirut"] = await ident("beirut")
            for d in (2, 3, 4):
                await appear(ids["beirut"], CAM_BEIRUT,
                             (now - timedelta(days=d)).replace(hour=0, minute=30, second=0))
            await db.commit()
        return ids
    ids = run_async(_run())
    yield ids
    _cleanup()


# ---------------------------------------------------------------------------
# Point-in-time correctness
# ---------------------------------------------------------------------------

def test_row_at_as_of_is_never_counted(seeded):
    """The strict `start_time < as_of` boundary: an appearance exactly AT
    as_of belongs to the future and must be invisible."""
    async def _run():
        from db_connection import db_manager
        from backend.ml.feature_store import feature_store
        await _ensure_db()
        async with db_manager.get_session() as db:
            at_boundary = await feature_store.compute_person_snapshot(
                db, seeded["boundary"], seeded["boundary_asof"], persist=False)
            just_after = await feature_store.compute_person_snapshot(
                db, seeded["boundary"],
                seeded["boundary_asof"] + timedelta(seconds=1), persist=False)
        return at_boundary, just_after
    at_boundary, just_after = run_async(_run())
    assert at_boundary["features"]["appearance_count_30d"] == 4.0, (
        "the row AT as_of leaked into the past")
    assert just_after["features"]["appearance_count_30d"] == 5.0


def test_snapshot_idempotency(seeded):
    async def _run():
        from db_connection import db_manager
        from backend.ml.feature_store import feature_store
        await _ensure_db()
        as_of = seeded["boundary_asof"] + timedelta(hours=1)
        async with db_manager.get_session() as db:
            first = await feature_store.compute_person_snapshot(
                db, seeded["boundary"], as_of, run_id="pytest-a")
            await db.commit()
            second = await feature_store.compute_person_snapshot(
                db, seeded["boundary"], as_of, run_id="pytest-b")
            await db.commit()
        return first, second
    first, second = run_async(_run())
    assert first["deduplicated"] is False
    assert second["deduplicated"] is True
    assert second["snapshot_id"] == first["snapshot_id"]
    assert second["features_checksum"] == first["features_checksum"]


def test_local_time_features_use_pipeline_timezone(seeded):
    """00:30 UTC at an Asia/Beirut camera is 02:30/03:30 LOCAL — inside the
    02-05 off-hours window and NOT in the 00-05 UTC night band's early hours
    interpretation. A UTC-only implementation returns off_hours_ratio 0."""
    async def _run():
        from db_connection import db_manager
        from backend.ml.feature_store import feature_store
        await _ensure_db()
        async with db_manager.get_session() as db:
            return await feature_store.compute_person_snapshot(
                db, seeded["beirut"], seeded["now"], persist=False)
    snapshot = run_async(_run())
    assert snapshot["features"]["off_hours_ratio_30d"] == 1.0, (
        f"local-time off-hours must see 02:30/03:30 Beirut: {snapshot['features']}")
    assert snapshot["local_timezone"] == "Asia/Beirut"


def test_unavailable_features_carry_reasons_not_zeros(seeded):
    async def _run():
        from db_connection import db_manager
        from backend.ml.feature_store import feature_store
        await _ensure_db()
        async with db_manager.get_session() as db:
            return await feature_store.compute_person_snapshot(
                db, seeded["beirut"], seeded["now"], persist=False)
    snapshot = run_async(_run())
    # 3 appearances -> hour circular stats need >=3... exactly 3 qualifies;
    # baseline deviation needs >=4 rows -> must be unavailable WITH a reason.
    assert "baseline_hour_deviation_last" not in snapshot["features"]
    assert snapshot["unavailable_features"].get("baseline_hour_deviation_last") == (
        "insufficient_history")
    assert 0.0 not in [snapshot["features"].get("baseline_hour_deviation_last")], (
        "an unavailable feature must never appear as a zero")


def test_graph_and_pair_features_gated_with_reasons(seeded):
    async def _run():
        from db_connection import db_manager
        from backend.ml.feature_builders import (
            graph_readiness, build_pair_co_appearance_count, FeatureUnavailable)
        await _ensure_db()
        async with db_manager.get_session() as db:
            ready, reasons, stats = await graph_readiness(db, datetime.utcnow())
            try:
                await build_pair_co_appearance_count(
                    db, seeded["boundary"], seeded["beirut"], datetime.utcnow(), {"days": 30})
                pair_outcome = "COMPUTED"
            except FeatureUnavailable as e:
                pair_outcome = e.reason
        return ready, reasons, pair_outcome
    ready, reasons, pair_outcome = run_async(_run())
    assert ready is False, "6-edge graph must fail the readiness floors"
    assert any("min_edges" in r or "min_nodes" in r for r in reasons), reasons
    assert pair_outcome.startswith("pair_below_min_appearances"), pair_outcome


def test_builders_never_touch_biometrics():
    """Privacy pin: behavioral aggregates only — no embeddings, no faces.
    Docstrings DESCRIBING the rule are stripped before scanning (the
    comment-vs-code trap)."""
    import re
    with open("/app/backend/ml/feature_builders.py", encoding="utf-8") as f:
        src = f.read()
    code = re.sub(r'"""[\s\S]*?"""', "", src)
    code = "\n".join(line for line in code.splitlines()
                     if not line.lstrip().startswith("#"))
    for banned in ("identity_embeddings", "IdentityEmbedding", "embedding",
                   "Face)", "faces."):
        assert banned not in code, f"biometric reference in feature builders: {banned}"


# ---------------------------------------------------------------------------
# Split + leakage + checksum
# ---------------------------------------------------------------------------

def _synthetic_rows(n=100, entities=10):
    """Staggered cohorts: entity e_k is active only in its own time band —
    realistic (new people appear over time) and the only shape where a
    group-integral temporal split CAN produce a non-empty holdout. (An
    entity active across the whole span belongs wholly to train by the
    earliest-bucket rule; its later rows are dropped, honestly.)"""
    base = datetime(2026, 1, 1)
    per_entity = n // entities
    rows = []
    for k in range(entities):
        band_start = base + timedelta(hours=k * per_entity)
        for j in range(per_entity):
            i = k * per_entity + j
            rows.append({
                "entity_id": f"e{k}",
                "as_of": band_start + timedelta(hours=j),
                "features": {"appearance_count_30d": float(i % 7)},
                "unavailable": {},
            })
    return rows


def test_temporal_group_split_no_entity_crosses_boundaries():
    from backend.ml.dataset_builder import temporal_group_split
    rows = _synthetic_rows()
    train, val, test, meta = temporal_group_split(rows)
    kept = len(train) + len(val) + len(test)
    assert kept + meta["dropped_for_group_integrity"] == len(rows), (
        "every row is either assigned or explicitly dropped — never silently lost")
    train_entities = {r["entity_id"] for r in train}
    val_entities = {r["entity_id"] for r in val}
    test_entities = {r["entity_id"] for r in test}
    assert not (train_entities & val_entities)
    assert not (train_entities & test_entities)
    assert not (val_entities & test_entities), "an entity crossed the holdout boundary"
    assert meta["method"] == "temporal_group" and meta["seed"] == 42
    # Determinism
    train2, val2, test2, _ = temporal_group_split(rows)
    assert [r["as_of"] for r in train2] == [r["as_of"] for r in train]


def test_holdout_period_is_pure_future():
    """Every test-split row must sit at/after the holdout boundary — the
    untouched final period is temporal, not random."""
    from backend.ml.dataset_builder import temporal_group_split
    rows = _synthetic_rows()
    train, val, test, meta = temporal_group_split(rows)
    boundary = datetime.fromisoformat(meta["holdout_boundary"].rstrip("Z"))
    assert test, "synthetic data must produce a holdout"
    assert all(r["as_of"] >= boundary for r in test)
    assert all(r["as_of"] < boundary for r in train), (
        "training rows at/after the holdout boundary = temporal leakage")


def test_dataset_fingerprint_stable_and_order_independent():
    from backend.ml.dataset_builder import dataset_fingerprint
    rows = _synthetic_rows(20)
    fp1 = dataset_fingerprint(rows)
    fp2 = dataset_fingerprint(list(reversed(rows)))
    assert fp1 == fp2, "fingerprint must be canonical (order-independent)"
    rows[0]["features"]["appearance_count_30d"] += 1.0
    assert dataset_fingerprint(rows) != fp1, "fingerprint must react to data changes"


def test_validator_null_rate_semantics():
    """Missingness: hard gate for supervised, honest warning for
    unsupervised (bootstrap-scale sparsity is data, not an error)."""
    from backend.ml.data_validator import validate_rows
    base = datetime(2026, 1, 1)
    definitions = [{"name": "a", "leakage_class": "safe"},
                   {"name": "sparse", "leakage_class": "safe"}]
    rows = [{"entity_id": f"e{i}", "as_of": base + timedelta(hours=i),
             "features": {"a": 1.0}, "unavailable": {"sparse": "no_history"}}
            for i in range(12)]
    unsup = validate_rows(rows, kind="unsupervised", definitions=definitions)
    assert unsup["passed"] is True, unsup["checks"]
    assert unsup["checks"]["max_null_rate"]["current"]["enforced"] is False
    assert any("sparse" in w for w in unsup["warnings"])

    sup_rows = [dict(r, label="positive" if i % 2 else "negative",
                     label_event_time=base + timedelta(hours=i, minutes=5))
                for i, r in enumerate(rows * 5)]
    # de-duplicate (entity, as_of) collisions from the *5 replication
    for i, r in enumerate(sup_rows):
        r["entity_id"] = f"s{i}"
    sup = validate_rows(sup_rows, kind="supervised", definitions=definitions)
    assert sup["checks"]["max_null_rate"]["passed"] is False, (
        "a feature missing everywhere must hard-fail a SUPERVISED build")


def test_validator_catches_leakage_and_duplicates():
    from backend.ml.data_validator import validate_rows
    definitions = [
        {"name": "appearance_count_30d", "leakage_class": "safe"},
        {"name": "assessment_count_30d", "leakage_class": "target_adjacent"},
    ]
    base = datetime(2026, 1, 1)
    ok_rows = [
        {"entity_id": f"e{i}", "as_of": base + timedelta(hours=i),
         "features": {"appearance_count_30d": 1.0}, "unavailable": {}}
        for i in range(12)
    ]
    report = validate_rows(ok_rows, kind="unsupervised", definitions=definitions)
    assert report["passed"] is True

    # duplicate (entity, as_of)
    dup = ok_rows + [dict(ok_rows[0])]
    assert validate_rows(dup, kind="unsupervised",
                         definitions=definitions)["checks"]["no_duplicates"]["passed"] is False

    # future as_of
    future = ok_rows + [{"entity_id": "f", "as_of": datetime.utcnow() + timedelta(days=2),
                         "features": {"appearance_count_30d": 1.0}, "unavailable": {}}]
    assert validate_rows(future, kind="unsupervised",
                         definitions=definitions)["checks"]["no_future_as_of"]["passed"] is False

    # target_adjacent feature in a supervised set + label anchored BEFORE features
    sup = [
        {"entity_id": f"s{i}", "as_of": base + timedelta(hours=i),
         "features": {"appearance_count_30d": 1.0, "assessment_count_30d": 2.0},
         "unavailable": {}, "label": "positive" if i % 2 else "negative",
         "label_event_time": base + timedelta(hours=i) - timedelta(minutes=5)}
        for i in range(60)
    ]
    report = validate_rows(sup, kind="supervised", definitions=definitions)
    assert report["passed"] is False
    assert report["checks"]["no_target_adjacent_features"]["passed"] is False
    assert report["checks"]["point_in_time_label_anchor"]["passed"] is False


def test_unsupervised_dataset_builds_and_versions_are_immutable(seeded):
    """Live build over real snapshots: registered, checksummed; a rebuild
    creates version N+1 — never overwrites."""
    async def _run():
        from db_connection import db_manager
        from backend.ml.dataset_builder import build_dataset, serialize_dataset
        from sqlalchemy import text as sa_text
        await _ensure_db()
        async with db_manager.get_session() as db:
            # Self-sufficiency: the build reads ml_feature_snapshots, and the
            # demo-data wipe means none exist unless this test makes them.
            await _seed_snapshot_corpus(db, PREFIX, PREFIX + "cam-corpus")
            first = await build_dataset(db, name="pytest-mlfd-behavior", kind="unsupervised")
            second = await build_dataset(db, name="pytest-mlfd-behavior", kind="unsupervised")
            rows = (await db.execute(sa_text(
                "SELECT version, status, checksum FROM ml_datasets "
                "WHERE name='pytest-mlfd-behavior' ORDER BY version"))).all()
            # cleanup dataset registry rows + parquet files
            paths = (await db.execute(sa_text(
                "SELECT storage_path FROM ml_datasets WHERE name='pytest-mlfd-behavior'"))).scalars().all()
            await db.execute(sa_text("DELETE FROM ml_datasets WHERE name='pytest-mlfd-behavior'"))
            await db.commit()
        import os
        for path in paths:
            if path and os.path.exists(path):
                os.remove(path)
        return first, second, rows
    first, second, rows = run_async(_run())
    assert first["status"] == "built", first
    assert second["status"] == "built"
    assert [r[0] for r in rows] == [1, 2], "rebuild must create a NEW version"
    assert first["row_count"] >= 10
    # Same source data -> same checksum across versions (stability)
    assert rows[0][2] == rows[1][2]


def test_dataset_serialization_is_path_free(seeded):
    from backend.ml.dataset_builder import serialize_dataset

    class FakeRow:
        id = uuid_mod.uuid4(); name = "x"; version = 1; kind = "unsupervised"
        feature_set_version = "v1"; label_definition_version = None
        row_count = 1; positive_count = None; negative_count = None
        time_range_start = None; time_range_end = None; holdout_boundary = None
        split_config = {}; checksum = "abc"; quality_report = {}
        status = "built"; code_version = None; created_at = None
    payload = serialize_dataset(FakeRow())
    import json
    dumped = json.dumps(payload)
    assert "storage_path" not in dumped and "models/" not in dumped

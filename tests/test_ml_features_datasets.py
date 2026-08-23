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


def test_temporal_split_keeps_every_row_and_measures_entity_overlap():
    """Split strategy 'temporal' (declared, never the silent default): same
    time boundaries, no row dropped, no future-period row in an earlier
    split, and the entity overlap is MEASURED — because a year of regular
    entities under group isolation leaves ~no val/test rows at all."""
    from backend.ml.dataset_builder import temporal_split, temporal_group_split, split_rows
    rows = _synthetic_rows()
    train, val, test, meta = temporal_split(rows)
    assert len(train) + len(val) + len(test) == len(rows)
    assert meta["method"] == "temporal" and meta["dropped_for_group_integrity"] == 0
    g_train, g_val, g_test, g_meta = temporal_group_split(rows)
    assert meta["holdout_boundary"] == g_meta["holdout_boundary"], "same boundaries as the group split"
    boundary = datetime.fromisoformat(meta["holdout_boundary"].rstrip("Z"))
    assert test and all(r["as_of"] >= boundary for r in test)
    assert all(r["as_of"] < boundary for r in train), "no future-period row in train"
    overlap = meta["entity_overlap"]["test"]
    train_entities = {r["entity_id"] for r in train}
    assert overlap["entities_shared_with_train"] == len({r["entity_id"] for r in test} & train_entities)
    assert overlap["rows_of_train_entities"] == sum(1 for r in test if r["entity_id"] in train_entities)
    assert "not generalisation to unseen entities" in meta["caveat"]
    # The group split drops what the temporal split keeps — the difference is exactly the overlap rows.
    assert g_meta["dropped_for_group_integrity"] >= 0
    # Dispatcher refuses an undeclared strategy.
    assert split_rows(rows, "temporal")[3]["method"] == "temporal"
    assert split_rows(rows, "temporal_group")[3]["method"] == "temporal_group"
    with pytest.raises(ValueError):
        split_rows(rows, "random")


def test_collector_drains_a_backlog_larger_than_one_batch(seeded, monkeypatch):
    """A full rebuild used to scan at most one batch (20 000 rows) and then
    move the watermark — a year imported at once was never fully collected.
    With a tiny batch size the collector must keep fetching keyset batches
    until the candidate window is exhausted, and say so in its stats."""
    import math
    from backend.ml import collector

    monkeypatch.setattr(collector, "BATCH_ROWS", 5)

    async def _run():
        from db_connection import db_manager
        from sqlalchemy import text as sa_text
        await _ensure_db()
        async with db_manager.get_session() as db:
            candidates = int((await db.execute(sa_text(
                "SELECT count(*) FROM identity_appearances"))).scalar())
            stats = await collector.run_collection(db, run_id="pytest-backlog", full_rebuild=True)
            await db.commit()
            checkpoint = (await db.execute(sa_text(
                "SELECT extras FROM ml_collection_checkpoints"))).scalar()
            # a second full rebuild over the same rows: idempotent by snapshot uniqueness
            again = await collector.run_collection(db, run_id="pytest-backlog-2", full_rebuild=True)
        return candidates, stats, checkpoint, again
    candidates, stats, checkpoint, again = run_async(_run())
    assert candidates > 5, "the seeded corpus must exceed one (patched) batch"
    assert stats["batch_rows"] == 5 and stats["cancelled"] is False
    assert stats["candidate_rows"] == candidates
    assert stats["rows_scanned"] == candidates, f"backlog not drained: {stats}"
    assert stats["batches"] == math.ceil(candidates / 5)
    assert checkpoint["last_rows"] == candidates and checkpoint["last_batches"] == stats["batches"]
    # Re-running over already-collected rows is idempotent (snapshot uniqueness):
    # every event snapshot of the second pass deduplicates, nothing is rewritten.
    assert again["rows_scanned"] == candidates
    assert again["snapshots_deduplicated"] >= candidates, again


def test_collector_phase2_cancel_is_durable_and_reconciled_on_restart(seeded, monkeypatch):
    """Phase 2 (one current-state snapshot per affected identity) commits per
    chunk, honours cancellation, records what is still pending, and the NEXT
    run recomputes exactly the identities that were left without a
    current-state snapshot - nothing is skipped permanently."""
    from backend.ml import collector
    monkeypatch.setattr(collector, "BATCH_ROWS", 5)
    monkeypatch.setattr(collector, "CURRENT_STATE_CHUNK", 1)

    async def _run():
        from db_connection import db_manager
        from sqlalchemy import text as sa_text
        await _ensure_db()
        async with db_manager.get_session() as db:
            # an identity of this test's own, with an appearance and NO snapshot
            fresh = str((await db.execute(sa_text(
                "INSERT INTO identities (id, type, status, display_name, first_seen_at, last_seen_at, "
                " created_at, updated_at, appearances_count) VALUES (gen_random_uuid(), 'UNKNOWN', 'ACTIVE', "
                " :n, now(), now(), now(), now(), 0) RETURNING id"), {"n": PREFIX + "p2-fresh"})).scalar())
            await db.execute(sa_text(
                "INSERT INTO identity_appearances (identity_id, pipeline_id, start_time, created_at) "
                "VALUES (CAST(:i AS uuid), :p, now() - interval '1 day', now())"), {"i": fresh, "p": CAM_UTC})
            await db.commit()
            candidates = int((await db.execute(sa_text("SELECT count(*) FROM identity_appearances"))).scalar())
            calls = {"n": 0}

            def cancel_after_phase1():
                calls["n"] += 1
                return calls["n"] > candidates + 1      # first check inside phase 2 -> cancel
            first = await collector.run_collection(db, run_id="pytest-p2-cancel", full_rebuild=True,
                                                   cancel_check=cancel_after_phase1)
            extras = (await db.execute(sa_text("SELECT extras FROM ml_collection_checkpoints"))).scalar()
            missing = await collector._identities_missing_current_state(db, extras["phase2"])
            second = await collector.run_collection(db, run_id="pytest-p2-resume", full_rebuild=False)
            extras_after = (await db.execute(sa_text("SELECT extras FROM ml_collection_checkpoints"))).scalar()
            started = datetime.fromisoformat(extras["phase2"]["started_at"].rstrip("Z"))
            fresh_has_state = int((await db.execute(sa_text(
                "SELECT count(*) FROM ml_feature_snapshots s WHERE s.entity_type = 'person' AND s.entity_id = :i "
                "AND s.event_timestamp IS NULL AND s.as_of_timestamp >= :t"), {"i": fresh, "t": started})).scalar())
        return candidates, fresh, first, extras, missing, second, extras_after, fresh_has_state
    candidates, fresh, first, extras, missing, second, extras_after, fresh_has_state = run_async(_run())
    assert first["cancelled"] is True and first["rows_scanned"] == candidates, first
    assert first["current_state_pending"] >= 1, "cancellation in phase 2 leaves work pending ..."
    assert extras["phase2"]["status"] == "in_progress" and extras["phase2"]["pending"] == first["current_state_pending"]
    assert fresh in missing, "... and the identities still lacking a current-state snapshot are found by SQL"
    assert second["cancelled"] is False and second["current_state_pending"] == 0
    assert extras_after["phase2"]["status"] == "complete" and extras_after["phase2"]["pending"] == 0
    assert fresh_has_state == 1, "after the restart the left-behind identity has its current-state snapshot"


def test_collector_job_renews_its_lock_and_refuses_a_concurrent_launch(seeded, monkeypatch):
    from backend.ml import collector
    from backend.core import distributed_lock as dl
    monkeypatch.setattr(collector, "BATCH_ROWS", 5)
    renewals = {"n": 0}
    original_renew = dl.DistributedLock.renew

    async def counting_renew(self):
        renewals["n"] += 1
        return await original_renew(self)
    monkeypatch.setattr(dl.DistributedLock, "renew", counting_renew)

    async def _run():
        import asyncio
        from backend.core.task_history import task_history_manager
        await _ensure_db()
        first = await collector.launch_collection_job(full_rebuild=True)
        second = await collector.launch_collection_job(full_rebuild=True)
        for _ in range(600):
            task = await task_history_manager.get_task_by_job_id(first["job_id"])
            if task and task["status"] in ("completed", "failed", "cancelled"):
                break
            await asyncio.sleep(0.1)
        return first, second, task
    first, second, task = run_async(_run())
    assert first["status"] == "scheduled" and first["job_id"].startswith("mlcollect-")
    assert second["status"] == "busy", second
    assert task["status"] == "completed", task
    assert task["result"]["batches"] >= 2
    assert renewals["n"] >= task["result"]["batches"], "the lock is renewed between batches/chunks"


def test_collector_watermark_progresses_and_incremental_runs_pick_up_new_rows(seeded):
    async def _run():
        from db_connection import db_manager
        from sqlalchemy import text as sa_text
        from backend.ml import collector
        await _ensure_db()
        async with db_manager.get_session() as db:
            await collector.run_collection(db, run_id="pytest-wm-1", full_rebuild=True)
            wm1 = (await db.execute(sa_text(
                "SELECT watermark_event_time, watermark_id FROM ml_collection_checkpoints"))).one()
            newest = (await db.execute(sa_text(
                "SELECT created_at, id FROM identity_appearances ORDER BY created_at DESC, id DESC LIMIT 1"))).one()
            # a new appearance arrives after the watermark
            await db.execute(sa_text(
                "INSERT INTO identity_appearances (identity_id, pipeline_id, start_time, created_at) "
                "VALUES ((SELECT identity_id FROM identity_appearances LIMIT 1), "
                "        (SELECT pipeline_id FROM identity_appearances LIMIT 1), now(), now())"))
            await db.commit()
            second = await collector.run_collection(db, run_id="pytest-wm-2", full_rebuild=False)
            wm2 = (await db.execute(sa_text(
                "SELECT watermark_event_time, watermark_id FROM ml_collection_checkpoints"))).one()
        return wm1, newest, second, wm2
    wm1, newest, second, wm2 = run_async(_run())
    assert (wm1[0], wm1[1]) == (newest[0], newest[1]), "the watermark is the newest processed (created_at, id)"
    assert second["rows_scanned"] >= 1 and second["full_rebuild"] is False
    assert (wm2[0], wm2[1]) > (wm1[0], wm1[1]), "an incremental run advances the watermark monotonically"

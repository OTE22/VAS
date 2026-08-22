"""
secintel-features-v2 — the two point-in-time fixes, proven on real rows.

  * is_unknown_identity is the type AS OF the snapshot: an identity promoted
    at T is 1.0 for as_of < T and 0.0 for as_of >= T; one known since
    creation (no audit transition) is 0.0 everywhere; one still unknown is 1.0.
  * days_since_last_seen is the exact MAX(start_time) < as_of gap even when
    the 90-day window exceeds the row cap; the windowed features then refuse
    (appearance_window_truncated) instead of undercounting.
  * snapshots are stamped secintel-features-v2; the v1 definition rows stay
    on record but inactive; a model trained under v1 is refused at inference
    against v2 snapshots (FEATURE_SCHEMA_MISMATCH), never silently scored.
"""

import uuid as uuid_mod
from datetime import datetime, timedelta

import pytest

from backend.ml.constants import FEATURE_SET_VERSION, PREVIOUS_FEATURE_SET_VERSION
from conftest import run_on_shared_loop as run_async

PREFIX = "pytest-fv2-"
CAM = PREFIX + "cam"


async def _ensure_db():
    from db_connection import db_manager
    if not getattr(db_manager, "_initialized", False):
        await db_manager.init_db()


def _run(coro_factory):
    async def _inner():
        from db_connection import db_manager
        await _ensure_db()
        async with db_manager.get_session() as db:
            return await coro_factory(db)
    return run_async(_inner())


def _cleanup():
    async def _c(db):
        from sqlalchemy import text as sa_text
        await db.execute(sa_text(
            "DELETE FROM identity_audit_log WHERE identity_id IN "
            "(SELECT id FROM identities WHERE display_name LIKE :p)"), {"p": PREFIX + "%"})
        await db.execute(sa_text(
            "DELETE FROM ml_feature_snapshots WHERE entity_id IN "
            "(SELECT id::text FROM identities WHERE display_name LIKE :p)"), {"p": PREFIX + "%"})
        await db.execute(sa_text(
            "DELETE FROM identity_appearances WHERE identity_id IN "
            "(SELECT id FROM identities WHERE display_name LIKE :p)"), {"p": PREFIX + "%"})
        await db.execute(sa_text("DELETE FROM identities WHERE display_name LIKE :p"), {"p": PREFIX + "%"})
        await db.commit()
    _run(_c)


@pytest.fixture(scope="module", autouse=True)
def module_cleanup():
    _cleanup()
    yield
    _cleanup()


async def _identity(db, name, itype):
    from sqlalchemy import text as sa_text
    await db.execute(sa_text(
        "INSERT INTO pipelines (pipeline_id, created_at, is_active) VALUES (:p, now(), 1) "
        "ON CONFLICT (pipeline_id) DO NOTHING"), {"p": CAM})
    return (await db.execute(sa_text(
        "INSERT INTO identities (id, type, status, display_name, first_seen_at, last_seen_at, "
        " created_at, updated_at, appearances_count) VALUES (gen_random_uuid(), :t, 'ACTIVE', :n, "
        " now() - interval '40 days', now(), now() - interval '40 days', now(), 0) RETURNING id"),
        {"t": itype, "n": name})).scalar()


async def _appearances(db, identity_id, stamps):
    from sqlalchemy import text as sa_text
    for ts in stamps:
        await db.execute(sa_text(
            "INSERT INTO identity_appearances (identity_id, pipeline_id, start_time, created_at) "
            "VALUES (:i, :p, :ts, now())"), {"i": identity_id, "p": CAM, "ts": ts})


async def _snapshot(db, identity_id, as_of):
    from backend.ml.feature_store import feature_store
    return await feature_store.compute_person_snapshot(db, str(identity_id), as_of, persist=False)


def test_is_unknown_identity_is_point_in_time():
    async def _t(db):
        from sqlalchemy import text as sa_text
        now = datetime.utcnow().replace(microsecond=0)
        promoted_at = now - timedelta(days=10)
        stamps = [now - timedelta(days=d, hours=3) for d in (20, 15, 12, 8, 5, 2)]

        promoted = await _identity(db, PREFIX + "promoted", "KNOWN")
        await _appearances(db, promoted, stamps)
        await db.execute(sa_text(
            "INSERT INTO identity_audit_log (user_id, username, action_type, identity_id, "
            " action_details, success, created_at) VALUES (NULL, 'pytest', 'promote', :i, '{}'::jsonb, true, :t)"),
            {"i": promoted, "t": promoted_at})
        since_birth = await _identity(db, PREFIX + "known-since-birth", "KNOWN")
        await _appearances(db, since_birth, stamps)
        still_unknown = await _identity(db, PREFIX + "unknown", "UNKNOWN")
        await _appearances(db, still_unknown, stamps)
        # a merge that turned an unknown target known, recorded the way the route records it
        merged = await _identity(db, PREFIX + "merged", "KNOWN")
        await _appearances(db, merged, stamps)
        merged_at = now - timedelta(days=6)
        await db.execute(sa_text(
            "INSERT INTO identity_audit_log (user_id, username, action_type, identity_id, "
            " action_details, before_state, after_state, success, created_at) VALUES (NULL, 'pytest', 'merge', :i, "
            " '{}'::jsonb, CAST(:before AS jsonb), CAST(:after AS jsonb), true, :t)"),
            {"i": merged, "t": merged_at,
             "before": '{"to_identity": {"type": "unknown"}, "from_identity": {"type": "known"}}',
             "after": '{"merged_identity": {"type": "known"}}'})
        await db.commit()

        before, after = now - timedelta(days=11), now - timedelta(days=1)
        out = {}
        for label, ident in (("promoted", promoted), ("since_birth", since_birth),
                             ("unknown", still_unknown), ("merged", merged)):
            out[label] = {
                "before": (await _snapshot(db, ident, before))["features"]["is_unknown_identity"],
                "after": (await _snapshot(db, ident, after))["features"]["is_unknown_identity"],
                "version": (await _snapshot(db, ident, after))["feature_set_version"],
            }
        return out
    out = _run(_t)
    assert out["promoted"] == {"before": 1.0, "after": 0.0, "version": FEATURE_SET_VERSION}, out
    assert out["merged"]["before"] == 1.0 and out["merged"]["after"] == 0.0, out
    assert out["since_birth"]["before"] == 0.0 and out["since_birth"]["after"] == 0.0, out
    assert out["unknown"]["before"] == 1.0 and out["unknown"]["after"] == 1.0, out


def test_days_since_last_seen_is_exact_and_truncated_windows_refuse():
    async def _t(db):
        from sqlalchemy import text as sa_text
        now = datetime.utcnow().replace(microsecond=0)
        busy = await _identity(db, PREFIX + "busy", "UNKNOWN")
        # 5,200 appearances spread over the last 60 days, newest 30 minutes before as_of
        await db.execute(sa_text(
            "INSERT INTO identity_appearances (identity_id, pipeline_id, start_time, created_at) "
            "SELECT :i, :p, CAST(:end AS timestamp) - (g * interval '16 minutes'), now() FROM generate_series(1, 5200) g"),
            {"i": busy, "p": CAM, "end": now - timedelta(minutes=14)})
        quiet = await _identity(db, PREFIX + "quiet", "UNKNOWN")
        await _appearances(db, quiet, [now - timedelta(days=3, hours=6), now - timedelta(days=1, hours=2)])
        await db.commit()
        busy_snap = await _snapshot(db, busy, now)
        quiet_snap = await _snapshot(db, quiet, now)
        last_busy = (await db.execute(sa_text(
            "SELECT max(start_time) FROM identity_appearances WHERE identity_id = :i AND start_time < :a"),
            {"i": busy, "a": now})).scalar()
        return busy_snap, quiet_snap, last_busy, now
    busy, quiet, last_busy, now = _run(_t)
    exact = (now - last_busy).total_seconds() / 86400.0
    assert abs(busy["features"]["days_since_last_seen"] - exact) < 1e-5, (busy["features"], exact)
    assert busy["features"]["days_since_last_seen"] < 0.05, "the newest row was NOT cut off"
    truncated = {k: v for k, v in busy["unavailable_features"].items() if v == "appearance_window_truncated"}
    assert "appearance_count_30d" in truncated and "off_hours_ratio_30d" in truncated, busy["unavailable_features"]
    assert "days_since_first_seen" in busy["features"] and "is_unknown_identity" in busy["features"]
    # an ordinary identity keeps every windowed feature and an exact last-seen
    assert "appearance_count_30d" in quiet["features"]
    assert abs(quiet["features"]["days_since_last_seen"] - (1 + 2 / 24)) < 1e-3


def test_definition_rows_and_snapshots_carry_the_versions():
    async def _t(db):
        from sqlalchemy import text as sa_text
        rows = (await db.execute(sa_text(
            "SELECT name, version, is_active, computation FROM ml_feature_definitions "
            "WHERE name IN ('is_unknown_identity', 'days_since_last_seen') ORDER BY name, version"))).all()
        active = (await db.execute(sa_text(
            "SELECT name, count(*) FROM ml_feature_definitions WHERE is_active GROUP BY name HAVING count(*) > 1"))).all()
        return rows, active
    rows, active = _run(_t)
    assert [tuple(r) for r in rows] == [
        ("days_since_last_seen", 1, False, "days_since_last_seen"),
        ("days_since_last_seen", 2, True, "days_since_last_seen_exact"),
        ("is_unknown_identity", 1, False, "is_unknown_identity"),
        ("is_unknown_identity", 2, True, "is_unknown_identity_as_of"),
    ]
    assert not active, "a feature name is active under exactly one version"
    assert FEATURE_SET_VERSION == "secintel-features-v2" and PREVIOUS_FEATURE_SET_VERSION == "secintel-features-v1"


def test_v1_model_never_scores_v2_snapshots():
    """A cached v1 artifact against a v2 online snapshot must fall back with
    FEATURE_SCHEMA_MISMATCH — same feature names, different semantics. The
    real predict path runs with only the model-cache step substituted."""
    from backend.ml.inference_service import inference_service

    class FakeCached:
        row_id = uuid_mod.uuid4()
        payload = {"feature_set_version": PREVIOUS_FEATURE_SET_VERSION,
                   "feature_names": ["appearance_count_7d"],
                   "imputation_medians": {"appearance_count_7d": 1.0},
                   "algorithm": "mad_baseline", "model": {},
                   "normalization": {"min": 0.0, "max": 1.0},
                   "band_cutpoints": {"elevated": 0.5, "unusual": 0.7, "highly_unusual": 0.9}}
        threshold = {"id": str(uuid_mod.uuid4()), "label": "global@v1",
                     "cutpoints": payload["band_cutpoints"]}
        version = 0

    async def _t(db):
        ident = await _identity(db, PREFIX + "v1model", "UNKNOWN")
        await _appearances(db, ident, [datetime.utcnow() - timedelta(days=2)])
        await db.commit()
        original = inference_service._ensure_current

        async def fake_ensure_current(_db, _model_type):
            return FakeCached()
        inference_service._ensure_current = fake_ensure_current
        try:
            return await inference_service.predict_identity(db, str(ident))
        finally:
            inference_service._ensure_current = original
    result = _run(_t)
    assert result.ok is False and result.failure_reason == "FEATURE_SCHEMA_MISMATCH", result
    assert result.behavioral_anomaly_score is None and result.ml_anomaly_band is None

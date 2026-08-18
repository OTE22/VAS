"""
Detection evidence — the ONE detection write path (backend/core/detection_evidence.py).

  CORE (atomic): detection + faces + appearance + exact embedding link + counter
  OPTIONAL A: live-alert triggers   (own savepoint)
  OPTIONAL B: watchlist alerts      (own savepoint)
  broadcast `detection_alerts` ONLY after commit, ONLY for rows persisted.

Covers: full-field watchlist alert row; idempotency per (entry, detection);
live trigger detection_id populated + idempotent; the four PASS/FAIL
combinations of the two optional subsystems (core always survives); core
failure commits nothing and broadcasts nothing; broadcast failure keeps rows;
`defer_commit` never commits; source and JS contracts.

    docker exec face_recognition_api python -m pytest tests/test_detection_evidence.py -q
"""
import importlib
import os
import uuid
from datetime import datetime

import pytest
from sqlalchemy import text

from conftest import run_on_shared_loop as run_async

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PIPE = "qa-evidence"


async def _db():
    from db_connection import db_manager
    if not getattr(db_manager, "_initialized", False):
        await db_manager.init_db()
    return db_manager


def _sql(statement, params=None, fetch="all", commit=False):
    async def _run():
        m = await _db()
        async with m.get_session() as db:
            res = await db.execute(text(statement), params or {})
            if commit:
                await db.commit()
                return None
            return res.scalar() if fetch == "scalar" else res.all()
    return run_async(_run())


def _ensure_services():
    ism = importlib.import_module("backend.core.identity_service")
    if ism.identity_service is None:
        from backend.core.identity_index_pgvector import get_pgvector_index
        ism.identity_service = ism.IdentityService(None, pgvector_index=get_pgvector_index())
    _sql("INSERT INTO pipelines (pipeline_id, created_at, updated_at, total_detections, is_active) "
         "VALUES (:p, now(), now(), 0, 1) ON CONFLICT (pipeline_id) DO NOTHING", {"p": PIPE}, commit=True)


def _identity(known=True):
    ident = str(uuid.uuid4())
    _sql("INSERT INTO identities (id, type, status, display_name, first_seen_at, last_seen_at, appearances_count, created_at, updated_at) "
         "VALUES (CAST(:i AS uuid), :t, 'ACTIVE', :n, now(), now(), 0, now(), now())",
         {"i": ident, "t": "KNOWN" if known else "UNKNOWN", "n": f"qa-evidence-{ident[:8]}" if known else None}, commit=True)
    return ident


def _embedding(ident):
    return _sql("INSERT INTO identity_embeddings (identity_id, pipeline_id, created_at) VALUES (CAST(:i AS uuid), :p, now()) RETURNING id",
                {"i": ident, "p": PIPE}, fetch="scalar")


def _watchlist_with(ident):
    wl = str(uuid.uuid4())
    _sql("INSERT INTO watchlists (id, name, alert_level, is_active, notify_dashboard, notify_email, notify_sms, notify_webhook, created_at, version) "
         "VALUES (CAST(:w AS uuid), :n, 'WARNING', true, true, false, false, false, now(), 1)", {"w": wl, "n": f"qa-evidence-{wl[:8]}"}, commit=True)
    entry = str(uuid.uuid4())
    _sql("INSERT INTO watchlist_entries (id, watchlist_id, identity_id, priority, is_active, added_at) "
         "VALUES (CAST(:e AS uuid), CAST(:w AS uuid), CAST(:i AS uuid), 'HIGH', true, now())", {"e": entry, "w": wl, "i": ident}, commit=True)
    return wl, entry


def _live_alert_for(ident):
    a = str(uuid.uuid4())
    _sql("INSERT INTO live_search_alerts (id, name, identity_id, status, min_similarity, time_window_enabled, cooldown_minutes, "
         "sound_alert, notify_dashboard, notify_email, notify_sms, notify_webhook, auto_capture_snapshot, auto_record_clip, "
         "clip_duration_seconds, expiration_type, triggers_count, created_at) VALUES (CAST(:a AS uuid), :n, CAST(:i AS uuid), 'ACTIVE', 0.0, "
         "false, 0, true, true, false, false, false, false, false, 0, 'NEVER', 0, now())",
         {"a": a, "n": f"qa-evidence-{a[:8]}", "i": ident}, commit=True)
    return a


def _detection_data(ident, emb_id, event_id=None):
    return {"pipeline_id": PIPE, "location_name": "QA",
            "detection": {"pipeline_id": PIPE, "timestamp": datetime.utcnow(), "image_size_bytes": 0,
                          "processing_time_ms": 1.0, "worker_id": 1},
            "faces": [{"name": "qa", "similarity": 0.91, "face_image_path": "qa/evidence.jpg",
                       "bbox_x1": 0.0, "bbox_y1": 0.0, "bbox_x2": 1.0, "bbox_y2": 1.0,
                       "identity_id": ident, "label_state": "auto_known", "quality": 0.7, "quality_scorer": "fq1",
                       "_embedding_id": emb_id, "_embedding_created_by_this_frame": emb_id is not None,
                       "_identity_created_by_this_frame": False, "_event_id": event_id or uuid.uuid4().hex,
                       "_is_known": True}]}


def _persist(detection_data):
    from backend.core.detection_evidence import persist_detection

    async def _run():
        m = await _db()
        async with m.get_session() as db:
            return await persist_detection(db, detection_data=detection_data)
    return run_async(_run())


def _cleanup(idents, watchlists=(), alerts=()):
    for w in watchlists:
        _sql("DELETE FROM watchlists WHERE id = CAST(:w AS uuid)", {"w": w}, commit=True)
    for a in alerts:
        _sql("DELETE FROM live_search_alerts WHERE id = CAST(:a AS uuid)", {"a": a}, commit=True)
    for i in idents:
        _sql("DELETE FROM faces WHERE identity_id = CAST(:i AS uuid)", {"i": i}, commit=True)
        _sql("DELETE FROM identity_embeddings WHERE identity_id = CAST(:i AS uuid)", {"i": i}, commit=True)
        _sql("DELETE FROM identity_appearances WHERE identity_id = CAST(:i AS uuid)", {"i": i}, commit=True)
        _sql("DELETE FROM identities WHERE id = CAST(:i AS uuid)", {"i": i}, commit=True)
    _sql("DELETE FROM detections d WHERE d.pipeline_id = :p AND NOT EXISTS (SELECT 1 FROM faces f WHERE f.detection_id = d.id)",
         {"p": PIPE}, commit=True)


# ---------------------------------------------------------------- persisted rows

def test_detection_watchlist_alert_is_persisted_with_every_field_and_is_idempotent():
    _ensure_services()
    ident = _identity()
    emb = _embedding(ident)
    wl, entry = _watchlist_with(ident)
    try:
        out = _persist(_detection_data(ident, emb))
        assert out.link_outcomes == {emb: "LINKED"}
        assert len(out.bundles) == 1 and len(out.bundles[0].watchlist_alerts) == 1
        row = _sql("SELECT triggered_by, detection_id, pipeline_id, snapshot_path, similarity_score, watchlist_entry_id::text "
                   "FROM watchlist_alerts WHERE watchlist_entry_id = CAST(:e AS uuid)", {"e": entry})
        assert len(row) == 1
        assert row[0][0] == "detection" and row[0][1] == out.detection_id and row[0][2] == PIPE
        assert row[0][3] == "qa/evidence.jpg" and abs(row[0][4] - 0.91) < 1e-6 and row[0][5] == entry
        assert out.bundles[0].watchlist_alerts[0]["alert_id"]
        # appearance + counter persisted in the same transaction
        assert _sql("SELECT count(*) FROM identity_appearances WHERE identity_id = CAST(:i AS uuid)", {"i": ident}, fetch="scalar") == 1
        # a retry for the SAME detection inserts nothing and returns nothing to broadcast
        from backend.core.watchlist_service import watchlist_service

        async def _retry():
            m = await _db()
            async with m.get_session() as db:
                again = await watchlist_service.record_detection_alerts(
                    db, identity_id=ident, detection_id=out.detection_id, pipeline_id=PIPE,
                    similarity=0.91, snapshot_path="qa/evidence.jpg")
                await db.commit()
                return again
        assert run_async(_retry()) == []
        assert _sql("SELECT count(*) FROM watchlist_alerts WHERE watchlist_entry_id = CAST(:e AS uuid)", {"e": entry}, fetch="scalar") == 1
        # raw duplicate → the partial unique index refuses
        with pytest.raises(Exception) as exc:
            _sql("INSERT INTO watchlist_alerts (id, watchlist_entry_id, triggered_by, detection_id, acknowledged, created_at) "
                 "VALUES (CAST(:n AS uuid), CAST(:e AS uuid), 'detection', :d, false, now())",
                 {"n": str(uuid.uuid4()), "e": entry, "d": out.detection_id}, commit=True)
        assert "uq_watchlist_alert_entry_detection" in str(exc.value)
    finally:
        _cleanup([ident], watchlists=[wl])


def test_live_alert_trigger_carries_the_detection_and_is_idempotent():
    _ensure_services()
    ident = _identity()
    emb = _embedding(ident)
    alert = _live_alert_for(ident)
    try:
        out = _persist(_detection_data(ident, emb))
        assert len(out.bundles) == 1 and len(out.bundles[0].live_alerts) == 1
        rows = _sql("SELECT detection_id, pipeline_id FROM live_alert_triggers WHERE alert_id = CAST(:a AS uuid)", {"a": alert})
        assert rows == [(out.detection_id, PIPE)], rows
        # same detection again → no second trigger row, nothing to broadcast
        from backend.core.live_alert_service import live_alert_service

        async def _retry():
            m = await _db()
            async with m.get_session() as db:
                t = await live_alert_service.check_detection_against_alerts(
                    db=db, identity_id=ident, similarity=0.91, pipeline_id=PIPE,
                    detection_id=out.detection_id, snapshot_path=None, defer_commit=True)
                await db.commit()
                return t
        assert run_async(_retry()) == []
        assert _sql("SELECT count(*) FROM live_alert_triggers WHERE alert_id = CAST(:a AS uuid)", {"a": alert}, fetch="scalar") == 1
    finally:
        _cleanup([ident], alerts=[alert])


# ---------------------------------------------------------------- four combinations

@pytest.mark.parametrize("live_fails,watchlist_fails", [(False, False), (True, False), (False, True), (True, True)])
def test_optional_subsystems_are_independently_isolated(monkeypatch, live_fails, watchlist_fails):
    """Core detection/face/appearance/link commit in every case; only the
    passing subsystem's rows persist and are broadcast."""
    _ensure_services()
    ident = _identity()
    emb = _embedding(ident)
    wl, entry = _watchlist_with(ident)
    alert = _live_alert_for(ident)
    import backend.core.live_alert_service as las
    import backend.core.watchlist_service as wls
    if live_fails:
        async def _boom(*a, **k):
            raise RuntimeError("live-alert subsystem down")
        monkeypatch.setattr(las.live_alert_service, "check_detection_against_alerts", _boom)
    if watchlist_fails:
        async def _boom2(*a, **k):
            raise RuntimeError("watchlist subsystem down")
        monkeypatch.setattr(wls.watchlist_service, "record_detection_alerts", _boom2)
    try:
        out = _persist(_detection_data(ident, emb))
        # CORE always committed
        assert _sql("SELECT count(*) FROM detections WHERE id = :d", {"d": out.detection_id}, fetch="scalar") == 1
        assert _sql("SELECT count(*) FROM faces WHERE detection_id = :d", {"d": out.detection_id}, fetch="scalar") == 1
        assert _sql("SELECT count(*) FROM identity_appearances WHERE identity_id = CAST(:i AS uuid)", {"i": ident}, fetch="scalar") == 1
        assert _sql("SELECT detection_id FROM identity_embeddings WHERE id = :e", {"e": emb}, fetch="scalar") == out.detection_id
        n_live = _sql("SELECT count(*) FROM live_alert_triggers WHERE alert_id = CAST(:a AS uuid)", {"a": alert}, fetch="scalar")
        n_wl = _sql("SELECT count(*) FROM watchlist_alerts WHERE watchlist_entry_id = CAST(:e AS uuid)", {"e": entry}, fetch="scalar")
        assert n_live == (0 if live_fails else 1)
        assert n_wl == (0 if watchlist_fails else 1)
        broadcast_live = sum(len(b.live_alerts) for b in out.bundles)
        broadcast_wl = sum(len(b.watchlist_alerts) for b in out.bundles)
        assert broadcast_live == n_live and broadcast_wl == n_wl, "broadcast only what was persisted"
    finally:
        _cleanup([ident], watchlists=[wl], alerts=[alert])


# ---------------------------------------------------------------- core failure / broadcast

def test_core_failure_commits_nothing_and_broadcasts_nothing(monkeypatch):
    """A cross-linked embedding (EmbeddingLinkError) is CORE: the whole
    detection transaction rolls back and no bundle exists to broadcast."""
    from backend.core import detection_evidence as de
    _ensure_services()
    ident = _identity()
    emb = _embedding(ident)
    wl, entry = _watchlist_with(ident)
    other = _sql("INSERT INTO detections (uuid, pipeline_id, timestamp, image_size_bytes, processing_time_ms) "
                 "VALUES (:u, :p, now(), 0, 1) RETURNING id", {"u": uuid.uuid4().hex, "p": PIPE}, fetch="scalar")
    _sql("UPDATE identity_embeddings SET detection_id = :d WHERE id = :e", {"d": other, "e": emb}, commit=True)
    sent = []
    async def _spy(bundles, **kw):
        sent.append(bundles)
        return len(bundles)
    monkeypatch.setattr(de, "broadcast_detection_alerts", _spy)
    before = _sql("SELECT count(*) FROM detections WHERE pipeline_id = :p", {"p": PIPE}, fetch="scalar")
    try:
        with pytest.raises(de.EmbeddingLinkError):
            _persist(_detection_data(ident, emb))
        assert _sql("SELECT count(*) FROM detections WHERE pipeline_id = :p", {"p": PIPE}, fetch="scalar") == before
        assert _sql("SELECT count(*) FROM identity_appearances WHERE identity_id = CAST(:i AS uuid)", {"i": ident}, fetch="scalar") == 0
        assert _sql("SELECT count(*) FROM watchlist_alerts WHERE watchlist_entry_id = CAST(:e AS uuid)", {"e": entry}, fetch="scalar") == 0
        assert sent == []
    finally:
        _sql("DELETE FROM detections WHERE id = :d", {"d": other}, commit=True)
        _cleanup([ident], watchlists=[wl])


def test_broadcast_failure_keeps_committed_rows(monkeypatch):
    from backend.core import detection_evidence as de
    _ensure_services()
    ident = _identity()
    emb = _embedding(ident)
    wl, entry = _watchlist_with(ident)
    try:
        out = _persist(_detection_data(ident, emb))
        import backend.core as core_pkg
        async def _boom(*a, **k):
            raise RuntimeError("websocket down")
        monkeypatch.setattr(core_pkg.ws_manager, "broadcast", _boom)
        sent = run_async(de.broadcast_detection_alerts(out.bundles, location_name="QA"))
        assert sent == 0
        assert _sql("SELECT count(*) FROM watchlist_alerts WHERE watchlist_entry_id = CAST(:e AS uuid)", {"e": entry}, fetch="scalar") == 1
    finally:
        _cleanup([ident], watchlists=[wl])


def test_defer_commit_never_commits(monkeypatch):
    """With defer_commit=True the live-alert path joins the caller's
    transaction: it must not commit or roll back the session itself."""
    from backend.core.live_alert_service import live_alert_service
    _ensure_services()
    ident = _identity()
    alert = _live_alert_for(ident)
    committed = []
    try:
        async def _run():
            m = await _db()
            async with m.get_session() as db:
                orig = db.commit
                async def _c():
                    committed.append(1)
                    await orig()
                db.commit = _c
                await live_alert_service.check_detection_against_alerts(
                    db=db, identity_id=ident, similarity=0.9, pipeline_id=PIPE, detection_id=None,
                    snapshot_path=None, defer_commit=True)
                during = list(committed)          # commits issued BY THE SERVICE only
                await db.rollback()               # the caller owns the transaction
                return during
        assert run_async(_run()) == []
        assert _sql("SELECT count(*) FROM live_alert_triggers WHERE alert_id = CAST(:a AS uuid)", {"a": alert}, fetch="scalar") == 0
    finally:
        _cleanup([ident], alerts=[alert])


# ---------------------------------------------------------------- source / JS contracts

def _read(rel):
    return open(f"{REPO}/{rel}", encoding="utf-8").read()


def test_pre_commit_payloads_carry_no_alerts_and_the_publisher_is_single():
    ip = _read("backend/services/image_processing.py")
    code = "\n".join(l for l in ip.splitlines() if not l.strip().startswith("#"))
    assert '"watchlist_matches"' not in code and '"live_alerts"' not in code, "pre-commit detection payload must not carry alerts"
    assert "check_identities_against_watchlists" not in code, "watchlist checks live in detection_evidence only"
    de = _read("backend/core/detection_evidence.py")
    assert '"type": "detection_alerts"' in de
    # the literal event publish exists in exactly one module
    publishers = [rel for rel in ("backend/services/image_processing.py", "backend/core/batch_writer.py",
                                  "backend/core/watchlist_service.py", "backend/core/live_alert_service.py",
                                  "backend/core/websocket_manager.py")
                  if '"type": "detection_alerts"' in _read(rel)]
    assert publishers == [], publishers
    bw = _read("backend/core/batch_writer.py")
    assert "persist_detection" in bw and "broadcast_detection_alerts" in bw
    assert "insert(Face)" not in bw and "insert(Detection)" not in bw, "no second write path"


def test_dashboard_and_live_alert_pages_consume_the_persisted_event():
    dash = _read("frontend/js/dashboard.js")
    assert "'detection_alerts'" in dash and "handleDetectionAlerts" in dash
    assert "alreadyProcessed(eventId)" in dash
    la = _read("frontend/js/admin-live-alerts.js")
    assert "message.type !== 'detection_alerts'" in la
    assert "message.type !== 'new_detection' && message.type !== 'live_alert_test'" not in la
    for rel in ("frontend/js/dashboard.js", "frontend/js/admin-unknown.js", "frontend/js/admin-watchlists.js"):
        assert "watchlist_matches" not in _read(rel), rel


def test_alert_listing_exposes_detection_id():
    assert '"detection_id": alert.detection_id' in _read("backend/routes/watchlists.py")

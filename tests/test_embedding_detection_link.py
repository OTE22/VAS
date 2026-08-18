"""
Deterministic embedding → detection provenance.

  * link_embedding_to_detection: exact id, NULL-guarded UPDATE, asserted
    outcomes (LINKED / ALREADY_LINKED / NO_EMBEDDING / CROSS_LINK_REFUSED /
    EMBEDDING_MISSING) — never "newest NULL-linked embedding".
  * compensate_failed_detection: removes ONLY the embeddings this frame created
    (ownership flag), never pre-existing / enrollment / preload rows.
  * reconcile_orphan_camera_embeddings: crash-safe — camera-origin embeddings
    whose detection never persisted are removed after the grace, through the
    canonical vector-index path; young / linked / enrollment rows are kept;
    idempotent.
  * concurrency: three frames of one identity → E1→D1, E2→D2, E3→D3 exactly.
  * source contracts: no heuristic left anywhere.

    docker exec face_recognition_api python -m pytest tests/test_embedding_detection_link.py -q
"""
import asyncio
import os
import re
import uuid
from datetime import datetime, timedelta

import pytest
from sqlalchemy import text

from conftest import run_on_shared_loop as run_async

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PIPE = "qa-emb-link"


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


def _ensure_identity_service():
    """The API process builds identity_service at startup; the test process
    must publish an equivalent instance (pgvector write path, no FAISS)."""
    import importlib
    ism = importlib.import_module("backend.core.identity_service")   # the package re-exports the instance name
    if ism.identity_service is None:
        from backend.core.identity_index_pgvector import get_pgvector_index
        ism.identity_service = ism.IdentityService(None, pgvector_index=get_pgvector_index())
    return ism.identity_service


def _ensure_pipeline():
    _sql("INSERT INTO pipelines (pipeline_id, created_at, updated_at, total_detections, is_active) "
         "VALUES (:p, now(), now(), 0, 1) ON CONFLICT (pipeline_id) DO NOTHING", {"p": PIPE}, commit=True)


def _identity():
    ident = str(uuid.uuid4())
    _sql("INSERT INTO identities (id, type, status, first_seen_at, last_seen_at, appearances_count, created_at, updated_at) "
         "VALUES (CAST(:i AS uuid), 'UNKNOWN', 'ACTIVE', now(), now(), 0, now(), now())", {"i": ident}, commit=True)
    return ident


def _embedding(ident, *, pipeline=PIPE, created_at=None, detection_id=None, image_id=None):
    return _sql("INSERT INTO identity_embeddings (identity_id, pipeline_id, detection_id, image_id, created_at) "
                "VALUES (CAST(:i AS uuid), :p, :d, :g, COALESCE(:c, now())) RETURNING id",
                {"i": ident, "p": pipeline, "d": detection_id, "g": image_id, "c": created_at}, fetch="scalar")


def _detection():
    return _sql("INSERT INTO detections (uuid, pipeline_id, timestamp, image_size_bytes, processing_time_ms) "
                "VALUES (:u, :p, now(), 0, 1) RETURNING id", {"u": uuid.uuid4().hex, "p": PIPE}, fetch="scalar")


def _cleanup(idents, detections=()):
    for d in detections:
        _sql("DELETE FROM detections WHERE id = :d", {"d": d}, commit=True)
    for i in idents:
        _sql("DELETE FROM identity_embeddings WHERE identity_id = CAST(:i AS uuid)", {"i": i}, commit=True)
        _sql("DELETE FROM identity_appearances WHERE identity_id = CAST(:i AS uuid)", {"i": i}, commit=True)
        _sql("DELETE FROM identities WHERE id = CAST(:i AS uuid)", {"i": i}, commit=True)


# ---------------------------------------------------------------- link outcomes

def test_link_outcomes_are_exact_and_asserted():
    from backend.core.detection_evidence import link_embedding_to_detection, LinkOutcome, EmbeddingLinkError
    _ensure_pipeline()
    ident = _identity()
    e_old = _embedding(ident)          # an older NULL-linked row of the SAME identity
    e_new = _embedding(ident)
    d1 = _detection()
    d2 = _detection()

    async def _run():
        m = await _db()
        async with m.get_session() as db:
            assert await link_embedding_to_detection(db, embedding_id=None, detection_id=d1) is LinkOutcome.NO_EMBEDDING
            assert await link_embedding_to_detection(db, embedding_id=e_new, detection_id=d1) is LinkOutcome.LINKED
            # idempotent retry, same detection: no write, success
            assert await link_embedding_to_detection(db, embedding_id=e_new, detection_id=d1) is LinkOutcome.ALREADY_LINKED
            # another detection may NEVER take the row over
            with pytest.raises(EmbeddingLinkError) as exc:
                await link_embedding_to_detection(db, embedding_id=e_new, detection_id=d2)
            assert exc.value.outcome is LinkOutcome.CROSS_LINK_REFUSED
            with pytest.raises(EmbeddingLinkError) as exc2:
                await link_embedding_to_detection(db, embedding_id=999999999, detection_id=d2)
            assert exc2.value.outcome is LinkOutcome.EMBEDDING_MISSING
            await db.commit()
    try:
        run_async(_run())
        rows = {r[0]: r[1] for r in _sql("SELECT id, detection_id FROM identity_embeddings WHERE identity_id = CAST(:i AS uuid)", {"i": ident})}
        assert rows[e_new] == d1 and rows[e_old] is None, "the older NULL-linked row must stay untouched"
    finally:
        _cleanup([ident], [d1, d2])


# ---------------------------------------------------------------- compensation

def test_compensation_removes_only_frame_created_embeddings():
    from backend.core.detection_evidence import compensate_failed_detection
    _ensure_pipeline()
    ident = _identity()
    pre_existing = _embedding(ident)                          # was there before the frame
    enrollment = _embedding(ident, pipeline=None)              # enrolled photo (no camera)
    frame_created = _embedding(ident)                          # THIS frame's row
    fresh_ident = _identity()                                  # created by the frame, only evidence = its embedding
    fresh_emb = _embedding(fresh_ident)
    detection_data = {"pipeline_id": PIPE, "faces": [
        {"identity_id": ident, "_embedding_id": frame_created, "_embedding_created_by_this_frame": True,
         "_identity_created_by_this_frame": False},
        {"identity_id": fresh_ident, "_embedding_id": fresh_emb, "_embedding_created_by_this_frame": True,
         "_identity_created_by_this_frame": True},
        {"identity_id": ident, "_embedding_id": pre_existing, "_embedding_created_by_this_frame": False,
         "_identity_created_by_this_frame": False},   # NOT owned: must survive
    ]}
    try:
        removed = run_async(compensate_failed_detection(detection_data))
        assert removed["embeddings"] == 2 and removed["identities"] == 1, removed
        left = {r[0] for r in _sql("SELECT id FROM identity_embeddings WHERE identity_id = CAST(:i AS uuid)", {"i": ident})}
        assert left == {pre_existing, enrollment}, left
        assert _sql("SELECT count(*) FROM identities WHERE id = CAST(:i AS uuid)", {"i": fresh_ident}, fetch="scalar") == 0
    finally:
        _cleanup([ident, fresh_ident])


# ---------------------------------------------------------------- reconciliation

def test_stale_camera_embeddings_are_reconciled_after_the_grace_only():
    from backend.core.identity_retention import identity_retention_manager, STALE_CAMERA_EMBEDDING_GRACE
    _ensure_pipeline()
    ident = _identity()
    old = datetime.utcnow() - STALE_CAMERA_EMBEDDING_GRACE - timedelta(minutes=1)
    stale = _embedding(ident, created_at=old)                                # camera-origin, never linked, old → REMOVED
    young = _embedding(ident)                                                # in flight → kept
    d = _detection()
    linked_old = _embedding(ident, created_at=old, detection_id=d)           # linked → kept
    enrollment_old = _embedding(ident, pipeline=None, created_at=old)        # not a camera → kept
    ghost = _identity()                                                      # a frame-created identity whose only row is stale
    ghost_emb = _embedding(ghost, created_at=old)
    try:
        first = run_async(identity_retention_manager.reconcile_orphan_camera_embeddings())
        assert first["embeddings"] == 2, first
        assert first["identities"] == 1, first
        left = {r[0] for r in _sql("SELECT id FROM identity_embeddings WHERE identity_id = CAST(:i AS uuid)", {"i": ident})}
        assert left == {young, linked_old, enrollment_old}, left
        assert _sql("SELECT count(*) FROM identities WHERE id = CAST(:i AS uuid)", {"i": ghost}, fetch="scalar") == 0
        second = run_async(identity_retention_manager.reconcile_orphan_camera_embeddings())
        assert second["embeddings"] == 0 and second["identities"] == 0, "must be idempotent"
    finally:
        _cleanup([ident, ghost], [d])


def test_reconciliation_uses_the_canonical_vector_removal_path():
    src = open(f"{REPO}/backend/core/identity_retention.py", encoding="utf-8").read()
    body = src[src.index("async def reconcile_orphan_camera_embeddings"):src.index("async def _cleanup_old_snapshots")]
    assert "remove_embedding_keys" in body, "vector-index keys must be removed before the rows"
    assert body.index("remove_embedding_keys") < body.index("DELETE FROM identity_embeddings")
    assert "STALE_CAMERA_EMBEDDING_GRACE" in src


def test_steady_state_orphan_query_is_zero():
    from backend.core.identity_retention import STALE_CAMERA_EMBEDDING_GRACE
    n = _sql("SELECT count(*) FROM identity_embeddings WHERE pipeline_id IS NOT NULL AND detection_id IS NULL "
             "AND created_at < :b", {"b": datetime.utcnow() - STALE_CAMERA_EMBEDDING_GRACE}, fetch="scalar")
    assert n == 0, f"{n} stale unlinked camera embeddings"


def test_no_incorrectly_linked_camera_embeddings():
    n = _sql("SELECT count(*) FROM identity_embeddings e JOIN detections d ON d.id = e.detection_id "
             "WHERE e.pipeline_id IS NOT NULL AND e.pipeline_id <> d.pipeline_id", fetch="scalar")
    assert n == 0, f"{n} embeddings linked to a detection of a different camera"


# ---------------------------------------------------------------- concurrency

def test_three_concurrent_frames_of_one_identity_link_exactly():
    """E1→D1, E2→D2, E3→D3 through persist_detection under asyncio.gather —
    the exact id travels with each frame, so no cross-link is possible."""
    from backend.core.detection_evidence import persist_detection
    _ensure_identity_service()
    _ensure_pipeline()
    ident = _identity()
    embs = [_embedding(ident) for _ in range(3)]

    async def _one(emb_id):
        m = await _db()
        async with m.get_session() as db:
            outcome = await persist_detection(db, detection_data={
                "pipeline_id": PIPE, "location_name": None,
                "detection": {"pipeline_id": PIPE, "timestamp": datetime.utcnow(),
                              "image_size_bytes": 0, "processing_time_ms": 1.0, "worker_id": 1},
                "faces": [{"name": "Unknown", "similarity": 0.0, "face_image_path": None,
                           "bbox_x1": 0.0, "bbox_y1": 0.0, "bbox_x2": 1.0, "bbox_y2": 1.0,
                           "identity_id": ident, "label_state": "auto_unknown",
                           "quality": None, "quality_scorer": None,
                           "_embedding_id": emb_id, "_embedding_created_by_this_frame": True,
                           "_identity_created_by_this_frame": False, "_event_id": uuid.uuid4().hex,
                           "_is_known": False}]})
            return outcome.detection_id, outcome.link_outcomes

    async def _all():
        return await asyncio.gather(*[_one(e) for e in embs])
    dets = []
    try:
        results = run_async(_all())
        dets = [r[0] for r in results]
        assert len(set(dets)) == 3
        for (det, outcomes), emb in zip(results, embs):
            assert outcomes == {emb: "LINKED"}, outcomes
        rows = {r[0]: r[1] for r in _sql("SELECT id, detection_id FROM identity_embeddings WHERE identity_id = CAST(:i AS uuid)", {"i": ident})}
        assert rows == {e: d for e, d in zip(embs, dets)}, rows
    finally:
        _cleanup([ident], dets)


# ---------------------------------------------------------------- source contracts

def test_no_embedding_heuristic_remains():
    """The batch writer and the ingest path carry the exact embedding id; the
    only NULL guard in the codebase is the exact-id UPDATE in the helper."""
    for rel in ("backend/core/batch_writer.py", "backend/services/image_processing.py"):
        src = open(f"{REPO}/{rel}", encoding="utf-8").read()
        code = "\n".join(l for l in src.splitlines() if not l.strip().startswith(("#", "*", "/*")))
        assert "detection_id.is_(None)" not in code, f"{rel}: NULL-embedding heuristic"
        assert "order_by(IdentityEmbedding.created_at.desc())" not in code, f"{rel}: newest-embedding heuristic"
    de = open(f"{REPO}/backend/core/detection_evidence.py", encoding="utf-8").read()
    assert de.count("IdentityEmbedding.detection_id.is_(None)") == 1
    assert "created_at.desc()" not in de

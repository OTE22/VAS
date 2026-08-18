"""
Reconciliation: converge the index onto PostgreSQL.
===================================================
Run inside the api container:

    docker exec face_recognition_api python -m pytest tests/test_vector_index_reconcile.py -v

The point of every test here is that drift is found by comparing **stable keys
and content**, not counts. The old repair logic compared `COUNT(*)` against
`ntotal`, which is blind to the most common real drift: one vector missing and
one stale vector present counts identically to a healthy index.
"""

import uuid as uuid_module

import numpy as np
import pytest

from conftest import run_on_shared_loop as run_async

from backend.core.vector_index import (EMBEDDING_DIM, FlatFaissIndex,
                                       SYNC_FAILED, SYNC_PENDING, SYNC_SYNCED,
                                       vector_checksum)
from backend.core.vector_index.reconcile import pending_backlog, reconcile

DIM = EMBEDDING_DIM
PREFIX = "qa-vecrec2-"


def unit(seed: int) -> np.ndarray:
    rng = np.random.default_rng(seed)
    v = rng.standard_normal(DIM).astype(np.float32)
    return v / np.linalg.norm(v)


def _sql(sql, params=None, fetch="none"):
    async def _run():
        from sqlalchemy import text
        from db_connection import db_manager
        if not getattr(db_manager, "_initialized", False):
            await db_manager.init_db()
        async with db_manager.get_session() as db:
            result = await db.execute(text(sql), params or {})
            out = None
            if fetch == "scalar":
                out = result.scalar()
            elif fetch == "all":
                out = result.all()
            await db.commit()
            return out
    return run_async(_run())


def _cleanup():
    _sql("DELETE FROM identity_embeddings WHERE identity_id IN "
         "(SELECT id FROM identities WHERE display_name LIKE :p)", {"p": PREFIX + "%"})
    _sql("DELETE FROM identities WHERE display_name LIKE :p", {"p": PREFIX + "%"})


def _ensure_qa_pipeline():
    """identity_embeddings.pipeline_id is a real FK (RESTRICT): the test camera
    must exist, exactly as live ingest registers a camera before any evidence."""
    _sql("INSERT INTO pipelines (pipeline_id, created_at, updated_at, total_detections, is_active) "
         "VALUES ('qa', now(), now(), 0, 1) ON CONFLICT (pipeline_id) DO NOTHING")


def _seed(n=3, state=SYNC_PENDING):
    _ensure_qa_pipeline()
    rows = []
    for i in range(n):
        ident = _sql(
            "INSERT INTO identities (id, type, status, display_name, first_seen_at, "
            " last_seen_at, created_at, updated_at, appearances_count) "
            "VALUES (gen_random_uuid(), 'KNOWN', 'ACTIVE', :n, now(), now(), now(), "
            " now(), 0) RETURNING id", {"n": f"{PREFIX}{uuid_module.uuid4().hex[:6]}"},
            fetch="scalar")
        seed = 5000 + i
        vec = unit(seed)
        literal = "[" + ",".join(f"{float(x):.8f}" for x in vec) + "]"
        emb = _sql(
            "INSERT INTO identity_embeddings (identity_id, pipeline_id, embedding, "
            " vector_index_sync_state, embedding_model_version, created_at) "
            "VALUES (:i, 'qa', CAST(:v AS vector), :s, 'm1', now()) RETURNING id",
            {"i": ident, "v": literal, "s": state}, fetch="scalar")
        rows.append((int(emb), seed, ident))
    return rows


@pytest.fixture
def seeded(tmp_path):
    _cleanup()
    rows = _seed()
    yield rows
    _cleanup()


@pytest.fixture
def index(tmp_path):
    return FlatFaissIndex(str(tmp_path / "idx"), model_version="m1")


def _reconcile(index):
    from db_connection import db_manager

    async def _run():
        async with db_manager.get_session() as db:
            return await reconcile(index, db)
    return run_async(_run())


def _state_of(emb_id):
    return _sql("SELECT vector_index_sync_state FROM identity_embeddings WHERE id=:e",
                {"e": emb_id}, fetch="scalar")


# ---------------------------------------------------------------------------
# Drift in both directions
# ---------------------------------------------------------------------------

def test_missing_vectors_are_added_and_marked_synced(seeded, index):
    """pending -> synced."""
    assert index.keys() == set()
    report = _reconcile(index)

    keys = {k for k, _s, _i in seeded}
    assert keys <= index.keys()
    assert report.added_missing >= len(keys)
    for emb_id, _seed, _ident in seeded:
        assert _state_of(emb_id) == SYNC_SYNCED


def test_stale_index_entries_are_removed(seeded, index):
    """A key the database no longer vouches for must leave the index."""
    _reconcile(index)
    ghost_key = 999_999_001
    index.add(ghost_key, unit(42))
    assert ghost_key in index.keys()

    report = _reconcile(index)
    assert ghost_key not in index.keys(), "a stale vector survived reconciliation"
    assert report.removed_stale >= 1


def test_equal_counts_but_wrong_set_is_still_detected(seeded, index):
    """The failure the old count-based check was blind to: one missing, one
    stale — `COUNT(*)` and `ntotal` agree, yet the index is wrong."""
    _reconcile(index)
    # Count what a healthy index holds RIGHT NOW, rather than assuming the
    # database contains only this test's rows. Reconciliation is global by
    # nature — it compares the index against every searchable identity — so
    # pinning the expected size to len(seeded) made the test pass only against
    # an empty database and fail the moment anything else was enrolled.
    healthy_size = len(index.keys())
    victim = seeded[0][0]
    index.remove([victim])                    # one genuine key missing
    index.add(999_999_002, unit(77))          # one stale key present
    assert len(index.keys()) == healthy_size  # counts still match: the blind spot

    report = _reconcile(index)
    assert victim in index.keys(), "the missing vector was not restored"
    assert 999_999_002 not in index.keys(), "the stale vector was not removed"
    assert report.drift >= 2


def test_deleted_rows_leave_the_index(seeded, index):
    _reconcile(index)
    victim = seeded[0][0]
    _sql("DELETE FROM identity_embeddings WHERE id=:e", {"e": victim})

    _reconcile(index)
    assert victim not in index.keys()


def test_inactive_identities_leave_the_index(seeded, index):
    _reconcile(index)
    victim_key, _seed, ident = seeded[0]
    _sql("UPDATE identities SET status='INACTIVE' WHERE id=:i", {"i": ident})

    report = _reconcile(index)
    assert victim_key not in index.keys(), "an inactive identity stayed searchable"
    assert report.removed_stale >= 1


# ---------------------------------------------------------------------------
# Content drift: checksum and model version
# ---------------------------------------------------------------------------

def _stored_checksum(emb_id):
    """Checksum of the vector AS THE DATABASE HOLDS IT.

    The stored text form is rounded, so the round-tripped float32 bytes differ
    slightly from the in-memory vector — the database copy is the one that
    matters, since that is what a rebuild would produce.
    """
    from backend.core.vector_index import validate_vector
    raw = _sql("SELECT embedding::text FROM identity_embeddings WHERE id=:e",
               {"e": emb_id}, fetch="scalar")
    return vector_checksum(validate_vector(raw))


def test_checksum_mismatch_is_refreshed(seeded, index):
    """The key is present but the indexed BYTES differ from the row."""
    _reconcile(index)
    victim, _seed_value, _ident = seeded[0]
    index.add(victim, unit(999))              # wrong vector under the right key

    report = _reconcile(index)
    assert report.refreshed_mismatched >= 1
    _model, checksum = index.entry_meta(victim)
    assert checksum == _stored_checksum(victim), "index was not refreshed from the DB"


def test_model_version_mismatch_is_refreshed(seeded, index):
    """Vectors from a different model must not silently coexist."""
    _reconcile(index)
    victim, _seed, _ident = seeded[0]
    _sql("UPDATE identity_embeddings SET embedding_model_version='m2' WHERE id=:e",
         {"e": victim})

    report = _reconcile(index)
    assert report.refreshed_mismatched >= 1
    model, _checksum = index.entry_meta(victim)
    assert model == "m2"


# ---------------------------------------------------------------------------
# NULL model_version: legitimate, and it must CONVERGE
#
# Observed: 5 of 6 searchable embeddings had embedding_model_version IS NULL,
# all from camera ingest, and reconcile reported drift on every single pass
# forever. Two independent faults produced that:
#
#   * the patch-up in _create_unknown_identity stamped quality_scorer_version
#     but not embedding_model_version, so ingest created NULL rows; and
#   * FlatFaissIndex.add substituted its own configured model whenever
#     model_version was None, so a NULL row was indexed as 'w600k_r50' and the
#     next comparison of NULL against 'w600k_r50' found drift again.
#
# The column stays nullable — NULL means "provenance unknown, recorded
# honestly" — so the fix is to make NULL round-trip, not to backfill it.
# ---------------------------------------------------------------------------

def test_a_null_model_version_converges(index):
    """Drift on the first pass (the row is genuinely missing), zero on the
    second. Before the sentinel fix the second pass reported drift too, and so
    did every pass after it."""
    _cleanup()
    rows = _seed(1)
    try:
        emb_id = rows[0][0]
        _sql("UPDATE identity_embeddings SET embedding_model_version = NULL, "
             "vector_index_sync_state = :p WHERE id = :e",
             {"e": emb_id, "p": SYNC_PENDING})

        first = _reconcile(index)
        second = _reconcile(index)

        assert second.drift == 0, (
            f"a NULL-model row never converges: pass 1 drift={first.drift}, "
            f"pass 2 drift={second.drift}")
        assert second.refreshed_mismatched == 0
    finally:
        _cleanup()


def test_a_null_model_version_is_indexed_as_null_not_as_the_default(index):
    """The distinction the sentinel exists to preserve: 'the caller passed
    None' is provenance, and must not be overwritten with the index's own
    configured model."""
    _cleanup()
    rows = _seed(1)
    try:
        emb_id = rows[0][0]
        _sql("UPDATE identity_embeddings SET embedding_model_version = NULL, "
             "vector_index_sync_state = :p WHERE id = :e",
             {"e": emb_id, "p": SYNC_PENDING})
        _reconcile(index)

        model, _checksum = index.entry_meta(emb_id)
        assert model is None, (
            f"a NULL provenance was indexed as {model!r}; comparing that back "
            f"against NULL reports drift on every pass")
    finally:
        _cleanup()


def test_no_write_path_creates_a_null_model_version():
    """Going forward there should be no NEW nulls to converge.

    Every path that inserts an identity_embeddings row must stamp the column
    or explicitly pass it through. identity_index_pgvector.add_embedding is the
    one shared constructor, and the startup loader reaches it directly —
    without the parameter, every preloaded known face was written NULL.
    """
    import ast

    source = open("/app/backend/core/identity_index_pgvector.py",
                  encoding="utf-8").read()
    tree = ast.parse(source)
    add = next((n for n in ast.walk(tree)
                if isinstance(n, ast.AsyncFunctionDef) and n.name == "add_embedding"),
               None)
    assert add is not None, "add_embedding not found"
    args = {a.arg for a in add.args.args} | {a.arg for a in add.args.kwonlyargs}
    assert "model_version" in args, (
        "add_embedding cannot record provenance, so every caller that does not "
        "patch the row up afterwards writes a NULL model version")
    assert any(
        isinstance(kw, ast.keyword) and kw.arg == "embedding_model_version"
        for node in ast.walk(add) if isinstance(node, ast.Call)
        for kw in node.keywords), (
        "add_embedding builds the row without embedding_model_version")

    loader = open("/app/backend/core/identity_loader.py", encoding="utf-8").read()
    calls = [n for n in ast.walk(ast.parse(loader))
             if isinstance(n, ast.Call)
             and isinstance(n.func, ast.Attribute)
             and n.func.attr == "add_embedding"]
    assert calls, "identity_loader no longer calls add_embedding"
    for call in calls:
        assert any(kw.arg == "model_version" for kw in call.keywords), (
            "a startup loader call omits model_version, so preloaded known "
            "faces are written with NULL provenance again")


# ---------------------------------------------------------------------------
# State machine
# ---------------------------------------------------------------------------

def test_failed_rows_are_retried(index):
    """failed -> pending -> synced."""
    _cleanup()
    rows = _seed(2, state=SYNC_FAILED)
    try:
        report = _reconcile(index)
        assert report.retried_failed >= 1
        for emb_id, _seed_value, _ident in rows:
            assert _state_of(emb_id) == SYNC_SYNCED
            assert emb_id in index.keys()
    finally:
        _cleanup()


def test_a_row_the_index_already_holds_is_marked_synced(seeded, index):
    """The index was right and the row's state lied — fix the row, not the index."""
    _reconcile(index)
    victim = seeded[0][0]
    _sql("UPDATE identity_embeddings SET vector_index_sync_state=:s WHERE id=:e",
         {"s": SYNC_PENDING, "e": victim})

    _reconcile(index)
    assert _state_of(victim) == SYNC_SYNCED


def test_a_row_without_a_vector_is_marked_failed_not_synced(index):
    """No vector means it cannot be indexed — recorded honestly, never guessed."""
    _cleanup()
    ident = _sql(
        "INSERT INTO identities (id, type, status, display_name, first_seen_at, "
        " last_seen_at, created_at, updated_at, appearances_count) "
        "VALUES (gen_random_uuid(), 'KNOWN', 'ACTIVE', :n, now(), now(), now(), now(), 0) "
        "RETURNING id", {"n": PREFIX + "novec"}, fetch="scalar")
    _ensure_qa_pipeline()
    emb = _sql("INSERT INTO identity_embeddings (identity_id, pipeline_id, "
               " vector_index_sync_state, created_at) "
               "VALUES (:i, 'qa', 'synced', now()) RETURNING id",
               {"i": ident}, fetch="scalar")
    try:
        report = _reconcile(index)
        assert report.unusable_rows >= 1
        assert _state_of(emb) == SYNC_FAILED, (
            "a row with no vector must not keep claiming to be synced")
        assert emb not in index.keys()
    finally:
        _cleanup()


def test_pending_backlog_counts_unconfirmed_rows(seeded):
    from db_connection import db_manager

    async def _run():
        async with db_manager.get_session() as db:
            return await pending_backlog(db)
    assert run_async(_run()) >= len(seeded)


def test_reconcile_is_idempotent(seeded, index):
    _reconcile(index)
    second = _reconcile(index)
    assert second.drift == 0, f"a converged index still reported drift: {second.as_dict()}"
    assert second.still_failing == 0

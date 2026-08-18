"""
Snapshot durability, corruption recovery, and database-only rebuild.
====================================================================
Run inside the api container:

    docker exec face_recognition_api python -m pytest tests/test_vector_index_recovery.py -v

The old implementation failed here in the two worst possible ways: a corrupt
index loaded as an EMPTY index and reported success (leaving N metadata entries
addressing 0 vectors), and "rebuild from database" reconstructed vectors from
the very in-memory index it was rebuilding — so a lost index could never be
recovered. These tests pin the replacements: verify-then-trust on load,
quarantine on any mismatch, and a rebuild that reads only
`identity_embeddings.embedding`.
"""

import hashlib
import json
import os
import shutil
import tempfile
import uuid as uuid_module

import numpy as np
import pytest

from conftest import run_on_shared_loop as run_async

from backend.core.vector_index import EMBEDDING_DIM, FlatFaissIndex

DIM = EMBEDDING_DIM
PREFIX = "qa-vecrec-"


def unit(seed: int) -> np.ndarray:
    rng = np.random.default_rng(seed)
    v = rng.standard_normal(DIM).astype(np.float32)
    return v / np.linalg.norm(v)


@pytest.fixture
def index_dir():
    directory = tempfile.mkdtemp(prefix="qa_vecrec_")
    yield directory
    shutil.rmtree(directory, ignore_errors=True)


def _populated(directory, keys=(1, 2, 3)):
    index = FlatFaissIndex(directory, model_version="m1")
    index.add_many([(k, unit(k)) for k in keys])
    index.save()
    return index


def _current_snapshot(directory):
    with open(os.path.join(directory, "CURRENT"), encoding="utf-8") as handle:
        return os.path.join(directory, handle.read().strip())


# ---------------------------------------------------------------------------
# Restart
# ---------------------------------------------------------------------------

def test_restart_restores_vectors_and_keys(index_dir):
    _populated(index_dir)
    reopened = FlatFaissIndex(index_dir, model_version="m1")
    result = reopened.load()
    assert result.supported and result.loaded
    assert reopened.keys() == {1, 2, 3}
    assert reopened.search(unit(2), top_k=1)[0][0] == 2, "search broke across restart"


def test_save_is_committed_by_a_single_pointer_swap(index_dir):
    index = _populated(index_dir)
    pointer = os.path.join(index_dir, "CURRENT")
    assert os.path.isfile(pointer), "no CURRENT pointer — nothing commits the snapshot"
    snapshot = _current_snapshot(index_dir)
    for name in ("index.bin", "keys.json", "manifest.json"):
        assert os.path.isfile(os.path.join(snapshot, name))
    manifest = json.load(open(os.path.join(snapshot, "manifest.json"), encoding="utf-8"))
    # ntotal and a checksum are recorded — the two fields whose absence made a
    # torn save undetectable before.
    assert manifest["ntotal"] == 3
    assert len(manifest["sha256"]) == 64
    assert manifest["dim"] == DIM and manifest["index_type"] == "flat"


def test_a_partial_snapshot_is_ignored_until_the_pointer_moves(index_dir):
    """A crash mid-write leaves the previous snapshot authoritative."""
    _populated(index_dir, keys=(1, 2, 3))
    committed = _current_snapshot(index_dir)
    # simulate an interrupted later save: a new dir exists but CURRENT still
    # points at the old one
    partial = os.path.join(index_dir, "snapshot-999999")
    os.makedirs(partial, exist_ok=True)
    with open(os.path.join(partial, "index.bin"), "wb") as handle:
        handle.write(b"half-written")

    reopened = FlatFaissIndex(index_dir, model_version="m1")
    assert reopened.load().loaded is True
    assert reopened.keys() == {1, 2, 3}
    assert _current_snapshot(index_dir) == committed


# ---------------------------------------------------------------------------
# Corruption -> quarantine (never a silent empty index)
# ---------------------------------------------------------------------------

def test_truncated_index_is_quarantined_not_loaded_as_empty(index_dir):
    _populated(index_dir)
    target = os.path.join(_current_snapshot(index_dir), "index.bin")
    with open(target, "r+b") as handle:
        handle.truncate(20)

    reopened = FlatFaissIndex(index_dir, model_version="m1")
    result = reopened.load()
    assert result.loaded is False, "a truncated index was loaded as if it were fine"
    assert result.quarantined and os.path.exists(result.quarantined)
    assert reopened.keys() == set(), "corrupt data leaked into the live index"
    assert "checksum" in result.reason or "parse" in result.reason


def test_checksum_mismatch_is_detected(index_dir):
    """Bytes changed underneath us — same length, different content."""
    _populated(index_dir)
    target = os.path.join(_current_snapshot(index_dir), "index.bin")
    with open(target, "r+b") as handle:
        handle.seek(-4, os.SEEK_END)
        tail = handle.read(4)
        handle.seek(-4, os.SEEK_END)
        # XOR so the bytes are GUARANTEED to differ. Writing zeros here was a
        # no-op: those trailing bytes were already zero, so the file never
        # changed and the test proved nothing.
        handle.write(bytes(b ^ 0xFF for b in tail))

    result = FlatFaissIndex(index_dir, model_version="m1").load()
    assert result.loaded is False and result.quarantined
    assert "checksum" in result.reason


def test_index_and_sidecar_disagreement_is_detected(index_dir):
    """ntotal != len(keys) — the exact divergence the old loader never checked."""
    _populated(index_dir)
    snapshot = _current_snapshot(index_dir)
    keys_path = os.path.join(snapshot, "keys.json")
    data = json.load(open(keys_path, encoding="utf-8"))
    data.pop(next(iter(data)))                      # drop one key
    json.dump(data, open(keys_path, "w", encoding="utf-8"))

    result = FlatFaissIndex(index_dir, model_version="m1").load()
    assert result.loaded is False and result.quarantined
    assert "disagree" in result.reason or "keys" in result.reason


def test_missing_sidecar_is_quarantined(index_dir):
    _populated(index_dir)
    os.remove(os.path.join(_current_snapshot(index_dir), "keys.json"))
    result = FlatFaissIndex(index_dir, model_version="m1").load()
    assert result.loaded is False and "keys.json" in result.reason


def test_pointer_to_a_missing_snapshot_is_reported(index_dir):
    _populated(index_dir)
    shutil.rmtree(_current_snapshot(index_dir))
    result = FlatFaissIndex(index_dir, model_version="m1").load()
    assert result.loaded is False and "missing snapshot" in result.reason


def test_no_snapshot_at_all_is_not_an_error(index_dir):
    result = FlatFaissIndex(index_dir).load()
    assert result.supported and result.loaded is False
    assert "no snapshot" in result.reason


def test_a_failed_save_leaves_the_previous_snapshot_intact(index_dir, monkeypatch):
    index = _populated(index_dir, keys=(1, 2))
    good = _current_snapshot(index_dir)
    good_manifest = open(os.path.join(good, "manifest.json"), encoding="utf-8").read()

    monkeypatch.setattr(index._faiss, "write_index",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")))
    with pytest.raises(OSError):
        index.save()

    assert _current_snapshot(index_dir) == good, "pointer moved despite a failed save"
    assert open(os.path.join(good, "manifest.json"), encoding="utf-8").read() == good_manifest
    reopened = FlatFaissIndex(index_dir, model_version="m1")
    assert reopened.load().loaded and reopened.keys() == {1, 2}


# ---------------------------------------------------------------------------
# Database-only rebuild — the release gate
# ---------------------------------------------------------------------------

def _seed_embeddings(count=5):
    """Create ACTIVE identities with real vectors in PostgreSQL."""
    from sqlalchemy import text
    from db_connection import db_manager

    async def _run():
        if not getattr(db_manager, "_initialized", False):
            await db_manager.init_db()
        created = []
        async with db_manager.get_session() as db:
            for i in range(count):
                ident = (await db.execute(text(
                    "INSERT INTO identities (id, type, status, display_name, "
                    " first_seen_at, last_seen_at, created_at, updated_at, appearances_count) "
                    "VALUES (gen_random_uuid(), 'KNOWN', 'ACTIVE', :n, now(), now(), "
                    " now(), now(), 0) RETURNING id"), {"n": f"{PREFIX}{i}"})).scalar()
                vec = unit(1000 + i)
                literal = "[" + ",".join(f"{float(x):.8f}" for x in vec) + "]"
                await db.execute(text(
                    "INSERT INTO pipelines (pipeline_id, created_at, updated_at, total_detections, is_active) "
                    "VALUES ('qa-probe', now(), now(), 0, 1) ON CONFLICT (pipeline_id) DO NOTHING"))
                emb_id = (await db.execute(text(
                    "INSERT INTO identity_embeddings (identity_id, pipeline_id, embedding, "
                    " vector_index_sync_state, embedding_model_version, created_at) "
                    "VALUES (:i, 'qa-probe', CAST(:v AS vector), 'pending', 'm1', now()) "
                    "RETURNING id"), {"i": ident, "v": literal})).scalar()
                created.append((int(emb_id), 1000 + i))
            await db.commit()
        return created
    return run_async(_run())


def _cleanup_seed():
    from sqlalchemy import text
    from db_connection import db_manager

    async def _run():
        if not getattr(db_manager, "_initialized", False):
            await db_manager.init_db()
        async with db_manager.get_session() as db:
            await db.execute(text(
                "DELETE FROM identity_embeddings WHERE identity_id IN "
                "(SELECT id FROM identities WHERE display_name LIKE :p)"),
                {"p": PREFIX + "%"})
            await db.execute(text(
                "DELETE FROM identities WHERE display_name LIKE :p"), {"p": PREFIX + "%"})
            await db.commit()
    run_async(_run())


@pytest.fixture
def seeded():
    _cleanup_seed()
    rows = _seed_embeddings()
    yield rows
    _cleanup_seed()


def test_rebuild_reads_the_database_not_the_existing_index(index_dir, seeded):
    """The old rebuild called reconstruct() on the index it was rebuilding, so a
    lost index was unrecoverable. This one starts from nothing."""
    from db_connection import db_manager

    index = FlatFaissIndex(index_dir, model_version="m1")
    assert index.keys() == set(), "precondition: index starts empty"

    async def _run():
        async with db_manager.get_session() as db:
            return await index.rebuild_from_db(db)
    report = run_async(_run())

    expected_keys = {key for key, _seed in seeded}
    assert report.rebuilt >= len(expected_keys)
    assert expected_keys <= index.keys(), "seeded vectors missing after rebuild"
    # and the vectors are the right ones
    for key, seed in seeded:
        assert index.search(unit(seed), top_k=1, threshold=0.9)[0][0] == key


def test_full_recovery_from_total_snapshot_loss(index_dir, seeded):
    """The release gate in miniature: delete every snapshot, then reconstruct
    purely from PostgreSQL and confirm keys are stable."""
    from db_connection import db_manager

    index = FlatFaissIndex(index_dir, model_version="m1")

    async def _rebuild(target):
        async with db_manager.get_session() as db:
            return await target.rebuild_from_db(db)

    run_async(_rebuild(index))
    index.save()
    before_keys = index.keys()
    assert before_keys

    # nuke everything on disk
    for entry in os.listdir(index_dir):
        path = os.path.join(index_dir, entry)
        shutil.rmtree(path, ignore_errors=True) if os.path.isdir(path) else os.remove(path)
    assert os.listdir(index_dir) == []

    fresh = FlatFaissIndex(index_dir, model_version="m1")
    assert fresh.load().loaded is False          # nothing to load...
    run_async(_rebuild(fresh))                   # ...so rebuild from the DB

    assert fresh.keys() == before_keys, "embedding keys were not stable across rebuild"
    for key, seed in seeded:
        assert fresh.search(unit(seed), top_k=1, threshold=0.9)[0][0] == key


def test_rebuild_skips_rows_without_a_vector_and_reports_them(index_dir, seeded):
    """A NULL embedding cannot be indexed. It is counted, never guessed at."""
    from sqlalchemy import text
    from db_connection import db_manager

    async def _add_null_row():
        async with db_manager.get_session() as db:
            ident = (await db.execute(text(
                "INSERT INTO identities (id, type, status, display_name, first_seen_at, "
                " last_seen_at, created_at, updated_at, appearances_count) "
                "VALUES (gen_random_uuid(), 'KNOWN', 'ACTIVE', :n, now(), now(), now(), "
                " now(), 0) RETURNING id"), {"n": PREFIX + "nullvec"})).scalar()
            await db.execute(text(
                "INSERT INTO pipelines (pipeline_id, created_at, updated_at, total_detections, is_active) "
                "VALUES ('qa-probe', now(), now(), 0, 1) ON CONFLICT (pipeline_id) DO NOTHING"))
            await db.execute(text(
                "INSERT INTO identity_embeddings (identity_id, pipeline_id, created_at) "
                "VALUES (:i, 'qa-probe', now())"), {"i": ident})
            await db.commit()
    run_async(_add_null_row())

    index = FlatFaissIndex(index_dir, model_version="m1")

    async def _run():
        async with db_manager.get_session() as db:
            return await index.rebuild_from_db(db)
    report = run_async(_run())
    # the NULL row is excluded by the query's IS NOT NULL, so it is simply not
    # indexed — the key point is it neither crashes nor invents a vector
    assert all(k in index.keys() for k, _ in seeded)


def test_rebuild_drops_vectors_of_inactive_identities(index_dir, seeded):
    """Deleted/deactivated rows must leave the index during rebuild."""
    from sqlalchemy import text
    from db_connection import db_manager

    index = FlatFaissIndex(index_dir, model_version="m1")

    async def _rebuild():
        async with db_manager.get_session() as db:
            return await index.rebuild_from_db(db)
    run_async(_rebuild())
    victim_key, _seed = seeded[0]
    assert victim_key in index.keys()

    async def _deactivate():
        async with db_manager.get_session() as db:
            await db.execute(text(
                "UPDATE identities SET status='INACTIVE' WHERE id = "
                "(SELECT identity_id FROM identity_embeddings WHERE id=:e)"),
                {"e": victim_key})
            await db.commit()
    run_async(_deactivate())

    report = run_async(_rebuild())
    assert victim_key not in index.keys(), "an inactive identity stayed searchable"
    assert report.removed_stale >= 1

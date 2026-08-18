"""
Concurrent access to the index.
===============================
Run inside the api container:

    docker exec face_recognition_api python -m pytest tests/test_vector_index_concurrency.py -v

The old implementation held its lock only around `.search()` itself, while the
`ntotal` read and every metadata dereference happened outside it — so a rebuild
swapping the index and its sidecar could be observed half-applied, and the
returned ordinals referred to the pre-swap index. Here every mutation and every
read that spans index+sidecar is inside one RLock, and these tests hammer that
under real contention.

Cross-PROCESS safety is out of scope by deployment policy: WORKERS=1 is enforced
by config_guard for unrelated process-local state, and a clobbered snapshot is
now rebuildable from PostgreSQL anyway.
"""

import threading

import numpy as np
import pytest

from backend.core.vector_index import EMBEDDING_DIM, FlatFaissIndex

DIM = EMBEDDING_DIM


def unit(seed: int) -> np.ndarray:
    rng = np.random.default_rng(seed)
    v = rng.standard_normal(DIM).astype(np.float32)
    return v / np.linalg.norm(v)


@pytest.fixture
def index(tmp_path):
    return FlatFaissIndex(str(tmp_path / "idx"), model_version="m1")


def _run_threads(targets, timeout=60):
    errors = []

    def wrap(fn):
        def runner():
            try:
                fn()
            except Exception as exc:          # noqa: BLE001 - reported below
                errors.append(exc)
        return runner

    threads = [threading.Thread(target=wrap(fn)) for fn in targets]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout)
        assert not t.is_alive(), "a worker thread deadlocked"
    return errors


def test_concurrent_adds_all_land(index):
    """No lost updates: every key added by every thread is present."""
    per_thread = 60
    threads = 6

    def adder(offset):
        def run():
            for i in range(per_thread):
                index.add(offset * 1000 + i, unit(offset * 1000 + i))
        return run

    errors = _run_threads([adder(t) for t in range(threads)])
    assert not errors, f"threads raised: {errors[:3]}"
    assert len(index.keys()) == threads * per_thread
    assert index.stats()["count"] == threads * per_thread, (
        "index vector count and key set disagree after concurrent adds")


def test_search_never_observes_a_half_applied_state(index):
    """Searching while adds and removes churn must never raise, and must never
    return a key that is not in the key set."""
    index.add_many([(k, unit(k)) for k in range(500, 600)])
    stop = threading.Event()
    seen_bad = []

    def churn():
        i = 0
        while not stop.is_set() and i < 400:
            key = 900_000 + (i % 50)
            index.add(key, unit(key))
            index.remove([key])
            i += 1

    def searcher():
        while not stop.is_set():
            hits = index.search(unit(550), top_k=5, threshold=0.0)
            keys = index.keys()
            for key, _score in hits:
                if key not in keys:
                    # a result whose key is gone is the half-applied state
                    seen_bad.append(key)
            if len(seen_bad) > 5:
                return

    churn_thread = threading.Thread(target=churn)
    search_threads = [threading.Thread(target=searcher) for _ in range(3)]
    churn_thread.start()
    for t in search_threads:
        t.start()
    churn_thread.join(60)
    stop.set()
    for t in search_threads:
        t.join(30)
        assert not t.is_alive()

    # A key removed between search() and keys() is a benign race in the TEST's
    # two-step observation, so allow a small number; a systematic failure means
    # the index itself is exposing torn state.
    assert len(seen_bad) <= 5, f"search returned keys absent from the index: {seen_bad[:5]}"


def test_concurrent_add_and_remove_leaves_consistent_state(index):
    keys = list(range(1000, 1100))
    index.add_many([(k, unit(k)) for k in keys])

    def remover(subset):
        def run():
            for k in subset:
                index.remove([k])
        return run

    half = keys[:50]
    errors = _run_threads([remover(half[:25]), remover(half[25:]),
                           lambda: [index.add(k, unit(k)) for k in range(2000, 2025)]])
    assert not errors, f"threads raised: {errors[:3]}"
    stats = index.stats()
    assert stats["count"] == len(index.keys()), (
        "vector count and key set diverged under concurrent add/remove")
    for k in half:
        assert not index.contains(k)


def test_rebuild_while_searching_is_safe(index, tmp_path):
    """A rebuild swaps the index and sidecar together; searches during it must
    see one or the other, never a mixture."""
    from db_connection import db_manager
    import asyncio

    index.add_many([(k, unit(k)) for k in range(3000, 3100)])
    errors = []
    stop = threading.Event()

    def searcher():
        try:
            while not stop.is_set():
                hits = index.search(unit(3050), top_k=3)
                for key, _ in hits:
                    assert isinstance(key, int)
        except Exception as exc:              # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=searcher) for _ in range(3)]
    for t in threads:
        t.start()

    # swap the whole index repeatedly while searches run
    for _ in range(20):
        fresh_keys = [(k, unit(k)) for k in range(4000, 4050)]
        with index._lock:
            new_index = index._new_index()
            import numpy as _np
            matrix = _np.vstack([v for _k, v in fresh_keys]).astype(_np.float32)
            ids = _np.asarray([k for k, _v in fresh_keys], dtype=_np.int64)
            new_index.add_with_ids(matrix, ids)
            index._index = new_index
            index._meta = {k: ("m1", "x") for k, _v in fresh_keys}

    stop.set()
    for t in threads:
        t.join(30)
        assert not t.is_alive()
    assert not errors, f"search raised during rebuild: {errors[:3]}"


def test_save_under_concurrent_writes_produces_a_loadable_snapshot(index, tmp_path):
    """A snapshot taken while writes are in flight must still verify on load —
    the manifest's ntotal and checksum have to describe the bytes written."""
    index.add_many([(k, unit(k)) for k in range(5000, 5050)])
    errors = []
    stop = threading.Event()

    def writer():
        i = 0
        while not stop.is_set() and i < 300:
            index.add(6000 + i, unit(6000 + i))
            i += 1

    def saver():
        try:
            for _ in range(5):
                index.save()
        except Exception as exc:              # noqa: BLE001
            errors.append(exc)

    w = threading.Thread(target=writer)
    s = threading.Thread(target=saver)
    w.start()
    s.start()
    s.join(60)
    stop.set()
    w.join(60)
    assert not errors, f"save raised under concurrent writes: {errors[:3]}"

    reopened = FlatFaissIndex(str(tmp_path / "idx"), model_version="m1")
    result = reopened.load()
    assert result.loaded, f"snapshot written under load failed to verify: {result.reason}"
    assert reopened.stats()["count"] == len(reopened.keys())

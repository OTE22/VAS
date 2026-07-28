"""FAISS index persistence integrity.

What these tests lock down: the index save used to write all four artifacts
(two .bin indexes + two .json metadata files) IN PLACE — a crash, OOM kill or
worker recycle mid-save left index and metadata mutually inconsistent, which
is precisely the corruption the repair worker exists to clean up after. Saves
are now write-temp → fsync → atomic-rename with a shared save_version stamped
into both metadata files, repair and rebuild serialize on a maintenance mutex,
the rebuild queue is popped under the same lock its appends use, and rebuilds
defer below a memory floor.

Runs in-container (faiss installed); uses tmp_path — never the real index dir.
"""

import asyncio
import json
import os

import numpy as np
import pytest

from conftest import run_on_shared_loop


def _make_index(tmp_path, n_vectors=5):
    from backend.core.identity_index import IdentityIndexService

    index = IdentityIndexService(embedding_size=32, db_path=str(tmp_path))
    rng = np.random.default_rng(42)
    for i in range(n_vectors):
        vec = rng.random(32, dtype=np.float32)
        index.add_known(f"identity-{i}", vec)
    return index


def _artifacts(tmp_path):
    return [
        tmp_path / "known_faiss_index.bin",
        tmp_path / "known_metadata.json",
        tmp_path / "unknown_faiss_index.bin",
        tmp_path / "unknown_metadata.json",
    ]


# ---------------------------------------------------------------------------
# Atomic save
# ---------------------------------------------------------------------------

def test_save_is_atomic_and_versioned(tmp_path):
    index = _make_index(tmp_path)
    index.save()

    for artifact in _artifacts(tmp_path):
        assert artifact.exists(), f"missing {artifact.name}"
    leftovers = list(tmp_path.glob("*.tmp"))
    assert not leftovers, f"temp files left behind: {leftovers}"

    known_meta = json.loads((tmp_path / "known_metadata.json").read_text())
    unknown_meta = json.loads((tmp_path / "unknown_metadata.json").read_text())
    assert known_meta["save_version"] == unknown_meta["save_version"] == 1, (
        "both metadata files must carry the same save_version — that equality "
        "is what makes a torn save detectable"
    )

    index.save()
    known_meta2 = json.loads((tmp_path / "known_metadata.json").read_text())
    assert known_meta2["save_version"] == 2, "version must increment per save"


def test_failed_save_leaves_previous_snapshot_untouched(tmp_path, monkeypatch):
    """If the swap fails partway through serialization, the old files survive."""
    index = _make_index(tmp_path)
    index.save()
    baseline = {p.name: p.read_bytes() for p in _artifacts(tmp_path)}

    calls = {"n": 0}
    real_replace = os.replace

    def failing_replace(src, dst):
        calls["n"] += 1
        raise OSError("disk gone")

    monkeypatch.setattr(os, "replace", failing_replace)
    with pytest.raises(Exception):
        index.save()
    monkeypatch.setattr(os, "replace", real_replace)

    for p in _artifacts(tmp_path):
        assert p.read_bytes() == baseline[p.name], (
            f"{p.name} was modified by a FAILED save — atomicity broken"
        )
    assert not list(tmp_path.glob("*.tmp")), "failed save left temp files"


def test_load_removes_stale_partial_saves(tmp_path):
    index = _make_index(tmp_path)
    index.save()

    stale = tmp_path / "known_faiss_index.bin.tmp"
    stale.write_bytes(b"partial garbage from a crashed save")

    assert index.load() is True
    assert not stale.exists(), "stale .tmp from a crashed save must be removed"


def test_load_restores_save_version(tmp_path):
    index = _make_index(tmp_path)
    index.save()
    index.save()

    reloaded = _make_index(tmp_path, n_vectors=0)  # constructor calls load()
    assert reloaded._save_version == 2, (
        "save_version must survive a reload or the next save would reuse a "
        "version number"
    )


# ---------------------------------------------------------------------------
# Maintenance mutual exclusion
# ---------------------------------------------------------------------------

def test_repair_and_rebuild_cannot_overlap(tmp_path):
    """The maintenance mutex must serialize the two workers' critical bodies."""
    index = _make_index(tmp_path)
    overlap = {"active": 0, "max_active": 0}

    async def critical(duration):
        async with index._maintenance_lock:
            overlap["active"] += 1
            overlap["max_active"] = max(overlap["max_active"], overlap["active"])
            await asyncio.sleep(duration)
            overlap["active"] -= 1

    async def scenario():
        await asyncio.gather(critical(0.05), critical(0.05), critical(0.05))

    run_on_shared_loop(scenario())
    assert overlap["max_active"] == 1, (
        f"maintenance critical sections overlapped ({overlap['max_active']} at once)"
    )


def test_rebuild_queue_pop_is_locked():
    """Structural: the queue pop must happen under self.lock like the appends."""
    with open("/app/backend/core/identity_index.py", encoding="utf-8") as f:
        source = f.read()
    worker = source.split("async def _background_rebuild_worker", 1)[1].split("\n        self._rebuild_task", 1)[0]
    pop_at = worker.index("._rebuild_queue.pop(0)")
    lock_at = worker.rfind("with self.lock:", 0, pop_at)
    assert lock_at != -1, "rebuild queue pop is not under self.lock"
    # The lock must be the nearest enclosing statement, not one from an
    # earlier unrelated block: no blank-line-separated statement between them.
    between = worker[lock_at:pop_at]
    assert between.count("\n") <= 2, "queue pop not directly inside the lock"


# ---------------------------------------------------------------------------
# Memory guard
# ---------------------------------------------------------------------------

def test_rebuild_defers_when_memory_is_low(tmp_path, monkeypatch):
    """psutil is NOT installed in the container; the guard reads /proc/meminfo
    via _available_memory_mb — patched here to simulate memory pressure."""
    index = _make_index(tmp_path)
    monkeypatch.setattr(type(index), "_available_memory_mb",
                        staticmethod(lambda: 64))  # 64MB — below any sane floor

    async def scenario():
        return await index._rebuild_index_from_database(None, "known")

    # db_session=None would explode if the guard did not return first.
    assert run_on_shared_loop(scenario()) is False, (
        "rebuild proceeded despite critically low memory"
    )


def test_memory_probe_works_in_the_container():
    """The guard is only real if /proc/meminfo is actually readable here."""
    from backend.core.identity_index import IdentityIndexService
    available = IdentityIndexService._available_memory_mb()
    assert available is not None and available > 0, (
        "memory probe returned nothing — the rebuild memory guard is inert"
    )


# ---------------------------------------------------------------------------
# Event loop protection + shutdown bound
# ---------------------------------------------------------------------------

def test_async_contexts_never_call_sync_save():
    """Structural: inside identity_index, every save from a coroutine must go
    through save_async (to_thread) — a sync save() freezes the event loop for
    the whole serialization."""
    with open("/app/backend/core/identity_index.py", encoding="utf-8") as f:
        source = f.read()
    assert "self.save()" not in source, (
        "a bare sync self.save() call is back inside identity_index — "
        "async paths must use save_async()"
    )
    assert "asyncio.to_thread(self.save)" in source


def test_shutdown_flush_is_bounded_and_off_loop():
    with open("/app/backend/lifespan.py", encoding="utf-8") as f:
        src = f.read()
    block = src.split('component_name == "identity_index"', 1)[1][:800]
    assert "asyncio.to_thread(component.save)" in block, "shutdown save blocks the loop"
    assert "wait_for" in block, "shutdown save is unbounded"
    assert "timeout" in block

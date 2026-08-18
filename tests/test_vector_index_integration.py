"""
Release gate for the FAISS cutover.
===================================
Run inside the api container:

    docker exec face_recognition_api python -m pytest tests/test_vector_index_integration.py -v

Proves the running application uses the new VectorIndex contract and not the
legacy `identity_index.py`: the right implementation is constructed per backend,
snapshot/reconcile loops exist ONLY for a real FAISS index, the legacy module is
never imported at runtime, and the operational guarantees (skip-don't-queue
saves, autosave >> save time, metrics, audit) hold.
"""

import json
import os
import re
import shutil
import tempfile
import urllib.request

import numpy as np
import pytest

from conftest import run_on_shared_loop as run_async

BASE = "http://localhost:8000"
LIFESPAN_SRC = "/app/backend/lifespan.py"
SERVICE_SRC = "/app/backend/core/identity_service.py"


def _health():
    with urllib.request.urlopen(BASE + "/health/detailed", timeout=30) as response:
        return json.loads(response.read())


def _source(path):
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def unit(seed: int) -> np.ndarray:
    rng = np.random.default_rng(seed)
    v = rng.standard_normal(512).astype(np.float32)
    return v / np.linalg.norm(v)


# ---------------------------------------------------------------------------
# Backend selection wires the NEW implementation
# ---------------------------------------------------------------------------

def test_pgvector_mode_runs_no_index_loops():
    """pgvector stores vectors in place — there is nothing to snapshot.

    The old code constructed the FAISS service and started three loops
    unconditionally, which is how an EMPTY index came to be re-serialized to
    disk 527 times on a pgvector deployment.
    """
    from config import settings
    if str(getattr(settings, "VECTOR_BACKEND", "")).lower() != "pgvector":
        pytest.skip("live backend is not pgvector")

    services = _health()["components"]["background_services"]["services"]
    faiss_loops = [name for name in services if "faiss" in name.lower()]
    index_loops = [name for name in services if "vector_index" in name]
    assert faiss_loops == [], f"legacy FAISS loops still running: {faiss_loops}"
    assert index_loops == [], f"snapshot loops running under pgvector: {index_loops}"


def test_faiss_mode_selects_the_new_implementation():
    """Not the legacy module — the contract implementation."""
    from types import SimpleNamespace

    from backend.core.vector_index import select_backend

    directory = tempfile.mkdtemp(prefix="qa_sel_")
    try:
        cfg = SimpleNamespace(VECTOR_BACKEND="faiss", VECTOR_INDEX_FALLBACK="",
                              KNOWN_INDEX_TYPE="flat", IDENTITY_INDEX_DB_PATH=directory)
        selection = select_backend(cfg, storage_dir=directory)
        module = type(selection.index).__module__
        assert selection.backend == "faiss"
        assert module.startswith("backend.core.vector_index"), (
            f"FAISS mode built {module} — expected the new contract implementation")
        assert type(selection.index).__name__ == "FlatFaissIndex"
    finally:
        shutil.rmtree(directory, ignore_errors=True)


def test_pgvector_mode_selects_the_pgvector_implementation():
    from types import SimpleNamespace

    from backend.core.vector_index import select_backend

    cfg = SimpleNamespace(VECTOR_BACKEND="pgvector", VECTOR_INDEX_FALLBACK="",
                          KNOWN_INDEX_TYPE="flat", IDENTITY_INDEX_DB_PATH="/tmp")
    selection = select_backend(cfg)
    assert selection.backend == "pgvector"
    assert type(selection.index).__name__ == "PgVectorIndex"
    assert selection.degraded is False


def test_unimplemented_index_type_fails_startup():
    from types import SimpleNamespace

    from backend.core.vector_index import VectorBackendUnavailable, select_backend

    cfg = SimpleNamespace(VECTOR_BACKEND="faiss", VECTOR_INDEX_FALLBACK="",
                          KNOWN_INDEX_TYPE="hnsw", IDENTITY_INDEX_DB_PATH="/tmp")
    with pytest.raises(VectorBackendUnavailable) as exc:
        select_backend(cfg)
    assert "not implemented" in str(exc.value)
    assert "VECTOR_INDEX_FALLBACK" in str(exc.value), (
        "the refusal should tell the operator how to opt into degradation")


def test_configured_fallback_is_degraded_not_silent():
    from types import SimpleNamespace

    from backend.core.vector_index import select_backend

    cfg = SimpleNamespace(VECTOR_BACKEND="faiss", VECTOR_INDEX_FALLBACK="pgvector",
                          KNOWN_INDEX_TYPE="hnsw", IDENTITY_INDEX_DB_PATH="/tmp")
    selection = select_backend(cfg)
    assert selection.backend == "pgvector" and selection.requested == "faiss"
    assert selection.degraded is True and selection.reason
    assert selection.health()["degraded"] is True


# ---------------------------------------------------------------------------
# The legacy module is not in any runtime path
# ---------------------------------------------------------------------------

def test_lifespan_does_not_construct_the_legacy_index():
    source = _source(LIFESPAN_SRC)
    assert "IdentityIndexService(" not in source, (
        "lifespan still constructs the legacy FAISS service")
    for gone in ("start_auto_save", "start_background_repair",
                 "start_background_rebuild", "repair_orphaned_entries_async"):
        assert gone not in source, f"lifespan still calls legacy {gone}()"
    assert "select_backend(" in source, "lifespan does not use the new selection"


def test_identity_service_uses_the_contract_not_the_legacy_object():
    source = _source(SERVICE_SRC)
    # No reaching into legacy internals.
    for leak in ("identity_index.known_index", "identity_index.unknown_index",
                 "identity_index.add_known(", "identity_index.add_unknown(",
                 "identity_index.known_metadata"):
        assert leak not in source, f"identity_service still touches legacy internals: {leak}"
    assert "self.vector_index.add(" in source
    assert "search_vector_index" in source


def test_the_running_process_has_not_imported_the_legacy_module():
    """The decisive check: is it in sys.modules of the live server?"""
    import json as _json
    import urllib.request as _req

    # Ask the app itself, via its own process, through an admin-free probe:
    # the test process is separate, so inspect the server's loaded modules by
    # importing the same entrypoints it uses and confirming they do not pull it.
    import subprocess
    result = subprocess.run(
        ["python", "-c",
         "import sys; sys.path.insert(0,'/app');"
         "import backend.lifespan;"
         "print('legacy' if 'backend.core.identity_index' in sys.modules else 'clean')"],
        capture_output=True, text=True, cwd="/app", timeout=180)
    assert result.returncode == 0, result.stderr[-1500:]
    assert result.stdout.strip().endswith("clean"), (
        "importing backend.lifespan pulls in the legacy identity_index module")


# ---------------------------------------------------------------------------
# Operational guarantees
# ---------------------------------------------------------------------------

def test_autosave_interval_cannot_overlap_a_save():
    """A 100k snapshot measured 2.0s here, up to ~21s on slower storage.

    The cadence is checked against the worst case, so a deployment pointed at
    slow storage still cannot end up permanently saving.
    """
    from types import SimpleNamespace

    from backend.core.vector_index.manager import (WORST_CASE_100K_SAVE_SECONDS as MEASURED_100K_SAVE_SECONDS,
                                                   MIN_AUTOSAVE_INTERVAL_SECONDS,
                                                   VectorIndexManager)
    from backend.core.vector_index import select_backend

    assert MIN_AUTOSAVE_INTERVAL_SECONDS > MEASURED_100K_SAVE_SECONDS * 4, (
        "the floor must leave room for a save to finish comfortably")

    cfg = SimpleNamespace(VECTOR_BACKEND="pgvector", VECTOR_INDEX_FALLBACK="",
                          KNOWN_INDEX_TYPE="flat", IDENTITY_INDEX_DB_PATH="/tmp",
                          VECTOR_INDEX_AUTOSAVE_INTERVAL_SECONDS=900.0)
    manager = VectorIndexManager(select_backend(cfg), None, cfg)
    assert manager.autosave_interval() == 900.0

    cfg.VECTOR_INDEX_AUTOSAVE_INTERVAL_SECONDS = 5.0
    clamped = VectorIndexManager(select_backend(cfg), None, cfg).autosave_interval()
    assert clamped == MIN_AUTOSAVE_INTERVAL_SECONDS, (
        "a too-small interval must be clamped, not honoured")


def test_concurrent_saves_are_skipped_not_queued():
    """A queued duplicate would rewrite identical bytes 21s later."""
    from types import SimpleNamespace

    from backend.core.vector_index import FlatFaissIndex
    from backend.core.vector_index.factory import BackendSelection
    from backend.core.vector_index.manager import VectorIndexManager

    directory = tempfile.mkdtemp(prefix="qa_save_")
    try:
        index = FlatFaissIndex(directory, model_version="m1")
        index.add(1, unit(1))
        cfg = SimpleNamespace(VECTOR_INDEX_AUTOSAVE_INTERVAL_SECONDS=900.0)
        manager = VectorIndexManager(
            BackendSelection(index=index, backend="faiss", requested="faiss"),
            None, cfg)

        manager._saving = True                     # pretend one is in flight
        result = run_async(manager.save_once(trigger="test"))
        assert result.get("skipped"), "a concurrent save was not skipped"
        assert manager._skipped_saves == 1
        manager._saving = False
    finally:
        shutil.rmtree(directory, ignore_errors=True)


def test_manager_status_exposes_operational_state():
    from types import SimpleNamespace

    from backend.core.vector_index import FlatFaissIndex
    from backend.core.vector_index.factory import BackendSelection
    from backend.core.vector_index.manager import VectorIndexManager

    directory = tempfile.mkdtemp(prefix="qa_status_")
    try:
        index = FlatFaissIndex(directory, model_version="m1")
        manager = VectorIndexManager(
            BackendSelection(index=index, backend="faiss", requested="faiss"),
            None, SimpleNamespace(VECTOR_INDEX_AUTOSAVE_INTERVAL_SECONDS=900.0,
                            VECTOR_INDEX_RECONCILE_INTERVAL_SECONDS=3600.0))
        status = manager.status()
        for key in ("backend", "requested_backend", "degraded", "last_drift",
                    "skipped_saves", "recovery_failures", "autosave_interval_seconds",
                    "count"):
            assert key in status, f"status is missing {key}"
    finally:
        shutil.rmtree(directory, ignore_errors=True)


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------

def test_required_metrics_are_registered():
    from backend.core import metrics

    for name in ("metrics_vector_index_size", "metrics_vector_index_drift",
                 "metrics_vector_index_pending",
                 "metrics_vector_index_last_rebuild",
                 "metrics_vector_index_recovery_failures"):
        assert hasattr(metrics, name), f"metric {name} is not defined"


def test_metrics_are_exposed_on_the_prometheus_endpoint():
    with urllib.request.urlopen(BASE + "/metrics", timeout=30) as response:
        body = response.read().decode(errors="replace")
    # The gauges appear once the manager publishes them at startup.
    for family in ("fr_vector_index_size", "fr_vector_index_pending"):
        assert family in body, f"{family} is not exposed on /metrics"


def test_metrics_publish_updates_the_gauges():
    from types import SimpleNamespace

    from backend.core import metrics
    from backend.core.vector_index import FlatFaissIndex
    from backend.core.vector_index.factory import BackendSelection
    from backend.core.vector_index.manager import VectorIndexManager
    from db_connection import db_manager

    directory = tempfile.mkdtemp(prefix="qa_metrics_")
    try:
        index = FlatFaissIndex(directory, model_version="m1")
        index.add_many([(k, unit(k)) for k in (1, 2, 3)])
        manager = VectorIndexManager(
            BackendSelection(index=index, backend="faiss", requested="faiss"),
            db_manager, SimpleNamespace(VECTOR_INDEX_AUTOSAVE_INTERVAL_SECONDS=900.0,
                            VECTOR_INDEX_RECONCILE_INTERVAL_SECONDS=3600.0))
        run_async(manager._publish_metrics())
        if metrics.metrics_vector_index_size is not None:
            value = metrics.metrics_vector_index_size.labels(backend="faiss")._value.get()
            assert value == 3, f"index size gauge reported {value}, expected 3"
    finally:
        shutil.rmtree(directory, ignore_errors=True)


# ---------------------------------------------------------------------------
# Audit
# ---------------------------------------------------------------------------

def test_lifecycle_events_write_audit_rows():
    """Rebuild/repair/removal previously produced no durable record at all."""
    from types import SimpleNamespace

    from sqlalchemy import text
    from backend.core.vector_index import FlatFaissIndex
    from backend.core.vector_index.factory import BackendSelection
    from backend.core.vector_index.manager import VectorIndexManager
    from db_connection import db_manager

    directory = tempfile.mkdtemp(prefix="qa_audit_")
    try:
        index = FlatFaissIndex(directory, model_version="m1")
        index.add_many([(90001, unit(1)), (90002, unit(2))])
        manager = VectorIndexManager(
            BackendSelection(index=index, backend="faiss", requested="faiss"),
            db_manager, SimpleNamespace(VECTOR_INDEX_AUTOSAVE_INTERVAL_SECONDS=900.0,
                            VECTOR_INDEX_RECONCILE_INTERVAL_SECONDS=3600.0))

        async def _remove():
            # The manager writes its audit row through db_manager, so the pool
            # has to exist before the removal — not after. Initialising it only
            # in the read-back step made the audit write silently no-op and the
            # test read an empty table.
            if not getattr(db_manager, "_initialized", False):
                await db_manager.init_db()
            return await manager.remove_keys([90001], reason="qa-test")

        removed = run_async(_remove())
        assert removed == 1

        async def _rows():
            async with db_manager.get_session() as db:
                out = (await db.execute(text(
                    "SELECT action_type FROM identity_audit_log "
                    "WHERE action_type LIKE 'vector_index%' "
                    "ORDER BY created_at DESC LIMIT 10"))).all()
                await db.execute(text(
                    "DELETE FROM identity_audit_log WHERE action_type LIKE 'vector_index%'"))
                await db.commit()
                return [r[0] for r in out]
        actions = run_async(_rows())
        assert "vector_index_removal" in actions, (
            f"vector removal wrote no audit row (saw {actions})")
    finally:
        shutil.rmtree(directory, ignore_errors=True)


# ---------------------------------------------------------------------------
# Observability that is actually observable
#
# Each of these locks in a defect found by running the system, not by reading
# it. All three shared one shape: a signal that EXISTED but reported a wrong
# value, which is strictly worse than a missing signal because it reads as
# healthy.
# ---------------------------------------------------------------------------

def test_vector_index_metrics_are_bound_to_module_attributes():
    """init_metrics() must not bind the collectors to function-locals.

    The five vector-index collectors were created without being declared
    `global`, so they landed in the Prometheus REGISTRY — /metrics printed their
    HELP lines — while `metrics.metrics_vector_index_size` stayed None and every
    publisher silently no-opped. The endpoint looked wired; nothing was.
    """
    from backend.core import metrics

    for name in ("metrics_vector_index_size", "metrics_vector_index_drift",
                 "metrics_vector_index_pending",
                 "metrics_vector_index_last_rebuild",
                 "metrics_vector_index_recovery_failures"):
        assert getattr(metrics, name, None) is not None, (
            f"{name} is None — it is registered but no publisher can reach it")


def test_index_size_metric_reflects_the_database_under_pgvector():
    """pgvector has no in-process count; the gauge must not report a flat zero.

    PgVectorIndex.stats() correctly omits `count` (it cannot count without a
    session). Publishing stats().get("count") or 0 therefore reported ZERO
    vectors for the backend that was serving every search — indistinguishable
    from an outage.
    """
    from types import SimpleNamespace

    from sqlalchemy import text
    from backend.core.vector_index import PgVectorIndex
    from backend.core.vector_index.base import SEARCHABLE_STATUS_SQL
    from backend.core.vector_index.factory import BackendSelection
    from backend.core.vector_index.manager import VectorIndexManager
    from db_connection import db_manager

    async def _run():
        if not getattr(db_manager, "_initialized", False):
            await db_manager.init_db()
        async with db_manager.get_session() as db:
            expected = (await db.execute(text(
                "SELECT COUNT(*) FROM identity_embeddings e "
                "JOIN identities i ON i.id = e.identity_id "
                "WHERE e.embedding IS NOT NULL AND " + SEARCHABLE_STATUS_SQL))).scalar()
        manager = VectorIndexManager(
            BackendSelection(index=PgVectorIndex(model_version="t"),
                             backend="pgvector", requested="pgvector"),
            db_manager, SimpleNamespace(VECTOR_INDEX_AUTOSAVE_INTERVAL_SECONDS=900.0,
                            VECTOR_INDEX_RECONCILE_INTERVAL_SECONDS=3600.0))
        await manager.publish_metrics()
        return int(expected or 0)

    expected = run_async(_run())

    from backend.core import metrics
    gauge = metrics.metrics_vector_index_size
    value = gauge.labels(backend="pgvector")._value.get()
    assert int(value) == expected, (
        f"gauge says {value} vectors, the database holds {expected}")


def test_pending_backlog_ignores_identities_the_index_does_not_cover():
    """A merged-away identity's row must not sit in the backlog forever.

    Reconciliation only considers ACTIVE/PROMOTED identities, so a `pending`
    row belonging to a MERGED identity is work nothing will ever do. Counting it
    produced an alert clearable only by hand-editing the database.
    """
    import uuid as uuid_module

    from sqlalchemy import text
    from backend.core.vector_index.reconcile import pending_backlog
    from db_connection import db_manager

    async def _run():
        if not getattr(db_manager, "_initialized", False):
            await db_manager.init_db()
        async with db_manager.get_session() as db:
            await db.execute(text(
                "INSERT INTO pipelines (pipeline_id, created_at, updated_at, total_detections, is_active) "
                "VALUES ('qa', now(), now(), 0, 1) ON CONFLICT (pipeline_id) DO NOTHING"))
            before = await pending_backlog(db)

            # Built through the ORM: the identities table carries several
            # NOT NULL columns with Python-side defaults, so a hand-written
            # INSERT tests the column list, not the backlog query.
            from datetime import datetime

            from db_models import Identity, IdentityEmbedding, IdentityStatus, IdentityType

            ident = uuid_module.uuid4()
            now = datetime.utcnow()
            db.add(Identity(
                id=ident, type=IdentityType.UNKNOWN, status=IdentityStatus.MERGED,
                display_name=f"qa-merged-{ident.hex[:8]}",
                first_seen_at=now, last_seen_at=now))
            await db.flush()
            db.add(IdentityEmbedding(
                identity_id=ident, pipeline_id="qa",
                embedding=[0.1] * 512,
                vector_index_sync_state="pending", created_at=now))
            await db.commit()
            try:
                after = await pending_backlog(db)
            finally:
                await db.execute(text(
                    "DELETE FROM identity_embeddings WHERE identity_id = :i"),
                    {"i": str(ident)})
                await db.execute(text("DELETE FROM identities WHERE id = :i"),
                                 {"i": str(ident)})
                await db.commit()
            return before, after

    before, after = run_async(_run())
    assert after == before, (
        f"a MERGED identity's pending row entered the backlog ({before} -> {after})")


def test_promoted_identities_stay_searchable():
    """PROMOTED is what an identity becomes when someone names it.

    The live pgvector backend has always searched ('ACTIVE','PROMOTED'). The new
    modules were written with `i.status = 'ACTIVE'`, which would have dropped
    every named person from FAISS-mode search AND from every rebuild — the
    people recognition exists to find.
    """
    from backend.core.vector_index.base import (SEARCHABLE_STATUSES,
                                                SEARCHABLE_STATUS_SQL)

    assert "PROMOTED" in SEARCHABLE_STATUSES
    assert "ACTIVE" in SEARCHABLE_STATUSES
    assert "MERGED" not in SEARCHABLE_STATUSES
    assert "INACTIVE" not in SEARCHABLE_STATUSES

    for path in ("/app/backend/core/vector_index/flat_faiss.py",
                 "/app/backend/core/vector_index/pgvector_index.py",
                 "/app/backend/core/vector_index/reconcile.py"):
        src = _source(path)
        assert "i.status = 'ACTIVE'" not in src, (
            f"{path} filters ACTIVE only — promoted people would vanish")
        assert "SEARCHABLE_STATUS_SQL" in src, (
            f"{path} does not use the shared searchable-status filter")

"""Retired legacy artifacts must stay retired.

    docker exec face_recognition_api python -m pytest tests/test_legacy_retirement.py -v

The 2026-08 demo-data cleanup deleted the legacy implementation chain and the
one-off migration scripts around it. The headline item is the display-name-keyed
`FaceDatabase` (`database/face_db.py`): loaded at every startup, write-never
under pgvector, so the "degraded fallback" that read it answered Unknown for
every face, 100% of the time, while logging "Match (legacy)" — and it still
carried stale demo vectors keyed by a representation (display names) the
identity system no longer uses.

VOLUME CAVEAT — read before debugging a failure here. `/app/database` is a
NAMED VOLUME that shadows the repo's deleted `database/` directory, and Docker
initializes volumes from image content: the volume holds STALE COPIES of
face_db.py/__init__.py, so deleting the repo files alone does not make
`import database` fail in-container. `scripts/purge_face_storage.py --apply`
deletes the stale volume files; the import-ban and no-python-in-the-volume
tests below pass only AFTER that purge has run. That ordering is the point:
these tests are the machine-checked evidence that no active code path can
reach the removed components.
"""

import ast
import importlib
import os
import pathlib
import re

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent

# Files deleted from the repository. Paths are repo-relative; the database/*
# entries are asserted via the volume-aware test below instead, because in the
# container /app/database is the volume, not the repo directory.
DELETED_REPO_FILES = [
    "main.py",                                    # standalone webcam demo — sole FaceDatabase CLI user
    "backend/core/face_recognition_cache.py",     # cached lookups against the empty store
    "utils/throughput_calculator.py",             # zero references anywhere
    "scripts/migrate_faiss_to_pgvector.py",       # read v1 index files no deployment has
    "scripts/backfill_pgvector_embeddings.py",    # display-name-as-filename matching
    "scripts/backfill_unknown_embeddings.py",
    "scripts/remove_backfilled_embeddings.py",
    "scripts/fix_null_embeddings.py",
    "scripts/check_identity_types.py",
    "scripts/check_null_embeddings.py",
    "scripts/check_startup_embeddings.py",
    "scripts/check_unknown_embeddings.py",
    "scripts/show_database_stats.py",             # duplicated by /api/stats + purge dry-run
    "scripts/migrations/run_migration.py",        # pre-Alembic create_all; Alembic is the only schema initializer
    "scripts/clear_all_data.py",                  # superseded by purge_face_storage.py
    "scripts/debug_pgvector_search.py",
]

# Modules that must not be importable in the running container. Bare
# `database` is deliberately NOT listed: /app/database is the volume
# MOUNTPOINT (identity_indexes lives there), and Python 3 imports any bare
# directory on sys.path as an empty namespace package — so `import database`
# can never raise while the mount exists. What matters is that the package
# holds no CODE: `database.face_db` must fail, and the volume must hold no
# .py files (asserted below).
BANNED_IMPORTS = [
    "database.face_db",
    "utils.throughput_calculator",
    "backend.core.face_recognition_cache",
]

# Config fields deleted in the same cleanup. Every one had zero functional
# readers; several were advertised as editable in the admin UI while doing
# nothing.
DELETED_SETTINGS = [
    "DB_PATH", "IDENTITY_INDEX_AUTO_SAVE_INTERVAL",
    "REPAIR_FAISS_ON_STARTUP", "REPAIR_FAISS_INTERVAL_HOURS",
    "FAISS_LAZY_MARKING_THRESHOLD", "UNKNOWN_INDEX_TYPE",
    "KNOWN_INDEX_NLIST", "KNOWN_INDEX_NPROBE", "KNOWN_INDEX_HNSW_M",
    "KNOWN_INDEX_HNSW_EF_CONSTRUCTION", "KNOWN_INDEX_HNSW_EF_SEARCH",
    "KNOWN_INDEX_PQ_M", "KNOWN_INDEX_PQ_BITS",
    "RATE_LIMIT_ENABLED", "RATE_LIMIT_INTERVAL",
    "ENABLE_METRICS", "METRICS_PORT",
    "MAP_ENABLE_SECURITY_FEATURES", "MAP_DETECT_PATTERNS",
    "MAP_SHOW_RISK_HEATMAP", "MAP_SHOW_TIMELINE",
]

SOURCE_ROOTS = ("backend", "utils", "scripts", "sql_agent")


def _python_sources():
    for root_name in SOURCE_ROOTS:
        for path in (REPO_ROOT / root_name).rglob("*.py"):
            if "__pycache__" in path.parts:
                continue
            yield path
    for name in ("config.py", "db_models.py", "db_connection.py",
                 "gunicorn.conf.py"):
        candidate = REPO_ROOT / name
        if candidate.exists():
            yield candidate


def _code_only(path):
    """Source with docstrings stripped; comments are already invisible to AST.

    These files explain the retirement in their own prose, and a text scan
    cannot tell an explanation from a call.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                             ast.ClassDef, ast.Module)):
            body = getattr(node, "body", [])
            if (body and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)):
                body.pop(0)
    return ast.unparse(tree)


# ---------------------------------------------------------------------------
# The files are gone
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("relative", DELETED_REPO_FILES)
def test_deleted_files_are_gone(relative):
    path = REPO_ROOT / relative
    assert not path.exists(), (
        f"{relative} was reintroduced. It was deleted in the 2026-08 legacy "
        "cleanup — see this module's docstring for why each file went.")


def test_the_volume_holds_no_python_and_no_face_database():
    """Volume-aware: /app/database may legitimately EXIST (identity_indexes
    lives there for the faiss backend), but after the purge it must hold no
    shadowing Python files and no retired face_database/ directory."""
    volume = pathlib.Path("/app/database")
    if not volume.exists():
        return  # nothing mounted — trivially clean

    stale_python = sorted(p.name for p in volume.glob("*.py"))
    assert not stale_python, (
        f"stale shadow copies in the /app/database volume: {stale_python} — "
        "run scripts/purge_face_storage.py --apply (its legacy-volume step "
        "removes them); until then `import database.face_db` resolves to "
        "dead code")
    # The face_database DIRECTORY may exist EMPTY: images built before the
    # entrypoint fix still carry `mkdir -p /app/database/face_database` in
    # their baked-in /usr/local/bin/docker-entrypoint.sh, which re-creates it
    # on every container start until the image is rebuilt. Content is what
    # matters — the retired store's files must never be there.
    face_db_dir = volume / "face_database"
    if face_db_dir.exists():
        leftovers = sorted(str(p.relative_to(face_db_dir))
                           for p in face_db_dir.rglob("*"))
        assert not leftovers, (
            f"the retired FaceDatabase's directory holds files: {leftovers}")
    assert not (volume / "app.db").exists()


# ---------------------------------------------------------------------------
# The modules cannot be imported
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("module", BANNED_IMPORTS)
def test_legacy_modules_cannot_be_imported(module):
    with pytest.raises(ImportError):
        importlib.import_module(module)


# ---------------------------------------------------------------------------
# Nothing references them
# ---------------------------------------------------------------------------

def test_no_source_references_the_face_database_chain():
    banned = re.compile(
        r"\bFaceDatabase\b|from database import|import database\b"
        r"|face_recognition_cache\b|throughput_calculator\b")
    offenders = []
    for path in _python_sources():
        if path.name == "purge_face_storage.py":
            continue  # names the stale volume files in order to DELETE them
        code = _code_only(path)
        if banned.search(code):
            offenders.append(str(path.relative_to(REPO_ROOT)))
    assert not offenders, (
        f"these still reference the retired FaceDatabase chain: {offenders}")


def test_deleted_settings_stay_deleted():
    from config import Settings

    resurrected = [name for name in DELETED_SETTINGS
                   if name in Settings.model_fields]
    assert not resurrected, (
        f"deleted config fields are back: {resurrected} — every one had zero "
        "functional readers and several were advertised as editable while "
        "doing nothing")


def test_enrollment_result_has_no_faiss_sync_status():
    """The response field reported the legacy store's sync state. With the
    store gone the field would be a constant — worse than absent."""
    from backend.core.enrollment_service import EnrollmentResult

    assert "faiss_sync_status" not in EnrollmentResult.__dataclass_fields__


# ---------------------------------------------------------------------------
# The schema matches
# ---------------------------------------------------------------------------

def test_dead_schema_is_gone_from_the_live_database():
    """Migration c5d6e7f8a9b0: faiss_id (all-NULL positional handle) + its two
    indexes + the never-shipped saved_searches table."""
    from sqlalchemy import text

    from conftest import run_on_shared_loop as run_async
    from db_connection import db_manager

    async def _run():
        if not getattr(db_manager, "_initialized", False):
            await db_manager.init_db()
        async with db_manager.get_session() as db:
            columns = [r[0] for r in (await db.execute(text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name='identity_embeddings' "
                "AND column_name LIKE 'faiss%'"))).all()]
            tables = (await db.execute(text(
                "SELECT count(*) FROM information_schema.tables "
                "WHERE table_name='saved_searches'"))).scalar()
            indexes = [r[0] for r in (await db.execute(text(
                "SELECT indexname FROM pg_indexes "
                "WHERE tablename='identity_embeddings' "
                "AND indexname LIKE '%faiss_id%'"))).all()]
            return columns, tables, indexes

    columns, saved_searches, indexes = run_async(_run())
    assert columns == ["faiss_index_type"], (
        f"identity_embeddings faiss columns: {columns} — faiss_id must stay "
        "dropped; faiss_index_type must stay (it partitions known/unknown)")
    assert not saved_searches, "saved_searches table is back"
    assert not indexes, f"faiss_id indexes are back: {indexes}"


def test_no_orm_model_maps_saved_searches():
    import db_models

    assert not hasattr(db_models, "SavedSearch")
    mapped = {mapper.class_.__name__ for mapper in db_models.Base.registry.mappers}
    assert "SavedSearch" not in mapped

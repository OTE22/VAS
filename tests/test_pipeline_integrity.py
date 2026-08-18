"""
Pipeline relationship integrity (migration c2d3e4f5a6b7).

    identity_appearances.pipeline_id  → pipelines.pipeline_id  RESTRICT  (NOT NULL)
    identity_embeddings.pipeline_id   → pipelines.pipeline_id  RESTRICT  (NULL = not a camera sighting)
    detections.pipeline_id            → pipelines.pipeline_id  RESTRICT  (was CASCADE)
    watchlist_alerts / live_alert_triggers .pipeline_id → SET NULL

One delete policy: a camera with evidence is deactivated (pipelines.is_active),
never hard-deleted; the rename flow moves every child first. Enrollment and
preload embeddings carry pipeline_id NULL — no sentinel strings anywhere.

    docker exec face_recognition_api python -m pytest tests/test_pipeline_integrity.py -q
"""
import os
import re
import uuid
from datetime import datetime

import pytest
from sqlalchemy import text

from conftest import run_on_shared_loop as run_async

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _sql(statement, params=None, fetch="all", commit=False):
    from db_connection import db_manager

    async def _run():
        if not getattr(db_manager, "_initialized", False):
            await db_manager.init_db()
        async with db_manager.get_session() as db:
            result = await db.execute(text(statement), params or {})
            if commit:
                await db.commit()
                return None
            if fetch == "scalar":
                return result.scalar()
            return result.all()
    return run_async(_run())


def _fk_rule(table, column):
    return _sql("""
        SELECT c.confdeltype::text, c.confrelid::regclass::text
        FROM pg_constraint c
        WHERE c.contype = 'f' AND c.conrelid = CAST(:t AS regclass)
          AND (SELECT array_agg(attname) FROM pg_attribute
               WHERE attrelid = c.conrelid AND attnum = ANY(c.conkey)) = ARRAY[CAST(:c AS name)]
    """, {"t": table, "c": column})


# ---------------------------------------------------------------- live schema

@pytest.mark.parametrize("table,column,rule", [
    ("identity_appearances", "pipeline_id", "r"),
    ("identity_embeddings", "pipeline_id", "r"),
    ("detections", "pipeline_id", "r"),
    ("watchlist_alerts", "pipeline_id", "n"),
    ("live_alert_triggers", "pipeline_id", "n"),
])
def test_pipeline_fk_exists_with_the_stated_rule(table, column, rule):
    rows = _fk_rule(table, column)
    assert len(rows) == 1, f"{table}.{column}: expected exactly one FK, found {rows}"
    assert rows[0][0] == rule and rows[0][1] == "pipelines", rows


def test_embeddings_pipeline_is_nullable_and_appearances_is_not():
    rows = {r[0]: r[1] for r in _sql(
        "SELECT table_name, is_nullable FROM information_schema.columns "
        "WHERE column_name = 'pipeline_id' AND table_name IN ('identity_embeddings', 'identity_appearances')")}
    assert rows == {"identity_embeddings": "YES", "identity_appearances": "NO"}, rows


# ---------------------------------------------------------------- behaviour

def _new_identity(db_ident):
    _sql("INSERT INTO identities (id, type, status, first_seen_at, last_seen_at, appearances_count, created_at, updated_at) "
         "VALUES (CAST(:i AS uuid), 'UNKNOWN', 'ACTIVE', now(), now(), 0, now(), now())", {"i": db_ident}, commit=True)


def test_evidence_for_an_unregistered_pipeline_is_refused():
    ident = str(uuid.uuid4())
    _new_identity(ident)
    try:
        with pytest.raises(Exception) as exc:
            _sql("INSERT INTO identity_appearances (identity_id, pipeline_id, start_time, created_at) "
                 "VALUES (CAST(:i AS uuid), :p, now(), now())", {"i": ident, "p": f"no-such-camera-{ident[:8]}"}, commit=True)
        assert "fk_identity_appearances_pipeline" in str(exc.value)
        with pytest.raises(Exception) as exc2:
            _sql("INSERT INTO identity_embeddings (identity_id, pipeline_id, created_at) "
                 "VALUES (CAST(:i AS uuid), :p, now())", {"i": ident, "p": f"no-such-camera-{ident[:8]}"}, commit=True)
        assert "fk_identity_embeddings_pipeline" in str(exc2.value)
    finally:
        _sql("DELETE FROM identities WHERE id = CAST(:i AS uuid)", {"i": ident}, commit=True)


def test_a_camera_with_evidence_cannot_be_hard_deleted_and_one_without_can():
    pid = f"qa-integrity-{uuid.uuid4().hex[:8]}"
    ident = str(uuid.uuid4())
    _sql("INSERT INTO pipelines (pipeline_id, created_at, updated_at, total_detections, is_active) VALUES (:p, now(), now(), 0, 1)",
         {"p": pid}, commit=True)
    _new_identity(ident)
    _sql("INSERT INTO identity_appearances (identity_id, pipeline_id, start_time, created_at) VALUES (CAST(:i AS uuid), :p, now(), now())",
         {"i": ident, "p": pid}, commit=True)
    try:
        with pytest.raises(Exception) as exc:
            _sql("DELETE FROM pipelines WHERE pipeline_id = :p", {"p": pid}, commit=True)
        assert "fk_identity_appearances_pipeline" in str(exc.value), "RESTRICT must block the delete"
        # deactivation is the operational path
        _sql("UPDATE pipelines SET is_active = 0 WHERE pipeline_id = :p", {"p": pid}, commit=True)
        assert _sql("SELECT is_active FROM pipelines WHERE pipeline_id = :p", {"p": pid}, fetch="scalar") == 0
    finally:
        _sql("DELETE FROM identity_appearances WHERE pipeline_id = :p", {"p": pid}, commit=True)
        _sql("DELETE FROM identities WHERE id = CAST(:i AS uuid)", {"i": ident}, commit=True)
        # a camera with zero evidence may go
        _sql("DELETE FROM pipelines WHERE pipeline_id = :p", {"p": pid}, commit=True)
    assert _sql("SELECT count(*) FROM pipelines WHERE pipeline_id = :p", {"p": pid}, fetch="scalar") == 0


def test_rename_moves_every_child_before_the_old_row_goes():
    """The rename flow (routes/users.py) inserts the new row, moves the six
    child tables, deletes the old row and writes an alias — under RESTRICT the
    delete only succeeds because the children were moved first."""
    src = open(f"{REPO}/backend/routes/users.py", encoding="utf-8").read()
    i = src.index("async def rename_pipeline") if "async def rename_pipeline" in src else src.index("/rename")
    body = src[i:i + 6000]
    for table in ("detections", "user_pipeline_access", "identity_embeddings",
                  "identity_appearances", "watchlist_alerts", "live_alert_triggers"):
        assert f'"{table}"' in body, f"rename must move {table}"
    assert body.index("UPDATE {table} SET pipeline_id") < body.index("DELETE FROM pipelines"), \
        "children must be moved before the old pipelines row is deleted"


def test_no_sentinel_pipeline_strings_remain_in_writers():
    """'uploaded' / 'preloaded' were never cameras. Enrollment and preload
    write NULL; the only file allowed to mention them is the repair script."""
    offenders = []
    for root, _dirs, files in os.walk(f"{REPO}/backend"):
        for name in files:
            if not name.endswith(".py"):
                continue
            path = os.path.join(root, name)
            with open(path, encoding="utf-8", errors="replace") as fh:
                for n, line in enumerate(fh, 1):
                    if re.search(r"pipeline_id\s*=\s*['\"](uploaded|preloaded)['\"]", line):
                        offenders.append(f"{path}:{n}")
    assert not offenders, offenders


def test_no_dangling_pipeline_ids_anywhere():
    for table in ("identity_appearances", "identity_embeddings", "detections",
                  "watchlist_alerts", "live_alert_triggers"):
        n = _sql(f"SELECT count(*) FROM {table} t WHERE t.pipeline_id IS NOT NULL AND NOT EXISTS "
                 f"(SELECT 1 FROM pipelines p WHERE p.pipeline_id = t.pipeline_id)", fetch="scalar")
        assert n == 0, f"{table}: {n} dangling pipeline ids"


def test_enrollment_embeddings_carry_no_camera():
    """Every embedding that came from an enrolled photo (image_id set) has NULL
    pipeline_id — provenance is the image, not a camera."""
    n = _sql("SELECT count(*) FROM identity_embeddings WHERE image_id IS NOT NULL AND pipeline_id IS NOT NULL",
             fetch="scalar")
    assert n == 0, f"{n} enrollment embeddings claim a camera"

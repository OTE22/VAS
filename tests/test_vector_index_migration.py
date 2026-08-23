"""
Migration safety for the PostgreSQL-authoritative vector store.
===============================================================
Run inside the api container:

    docker exec face_recognition_api python -m pytest tests/test_vector_index_migration.py -v

The one rule that matters: `identity_embeddings.embedding` is the authoritative
copy of every face vector. A migration — in EITHER direction — that dropped,
truncated or nulled it would destroy the source of truth and leave the system
with nothing to rebuild the search index from. These tests pin that, plus the
shape of the state column and the removal of the duplicated image-level state.
"""

import uuid as uuid_module

import pytest

from conftest import run_on_shared_loop as run_async

EXPECTED_HEAD = "a3c8e5f1b7d2"
LEGAL_STATES = {"pending", "synced", "failed"}


async def _ensure_db():
    from db_connection import db_manager
    if not getattr(db_manager, "_initialized", False):
        await db_manager.init_db()


def _scalar(sql, params=None):
    async def _run():
        from sqlalchemy import text
        from db_connection import db_manager
        await _ensure_db()
        async with db_manager.get_session() as db:
            return (await db.execute(text(sql), params or {})).scalar()
    return run_async(_run())


# ---------------------------------------------------------------------------
# Schema shape
# ---------------------------------------------------------------------------

def test_migration_head_is_applied():
    assert _scalar("SELECT version_num FROM alembic_version") == EXPECTED_HEAD


def test_authoritative_vector_column_exists_and_is_the_right_type():
    """If this column ever disappears, the index becomes the only copy again."""
    udt = _scalar("SELECT udt_name FROM information_schema.columns "
                  "WHERE table_name='identity_embeddings' AND column_name='embedding'")
    assert udt == "vector", f"embedding column is {udt!r}, expected pgvector 'vector'"


def test_sync_state_column_shape():
    row = _scalar("SELECT column_default FROM information_schema.columns "
                  "WHERE table_name='identity_embeddings' "
                  "AND column_name='vector_index_sync_state'")
    assert row is not None and "pending" in row, (
        "new embeddings must default to 'pending' — claiming 'synced' before "
        "anything verified it would be a fabricated fact")
    nullable = _scalar("SELECT is_nullable FROM information_schema.columns "
                       "WHERE table_name='identity_embeddings' "
                       "AND column_name='vector_index_sync_state'")
    assert nullable == "NO"


def test_model_version_column_is_nullable():
    """NULL means 'provenance unknown' for historical rows — never fabricated."""
    nullable = _scalar("SELECT is_nullable FROM information_schema.columns "
                       "WHERE table_name='identity_embeddings' "
                       "AND column_name='embedding_model_version'")
    assert nullable == "YES"


def test_sync_state_is_indexed_for_reconciliation_scans():
    assert _scalar("SELECT count(*) FROM pg_indexes "
                   "WHERE tablename='identity_embeddings' "
                   "AND indexname='idx_embedding_sync_state'") == 1


def test_duplicated_image_level_state_is_gone():
    """It was written and never read; per-vector state belongs on the vector."""
    assert _scalar("SELECT count(*) FROM information_schema.columns "
                   "WHERE table_name='identity_images' "
                   "AND column_name='faiss_sync_state'") == 0


def test_orm_matches_the_database():
    """ORM and schema cannot silently diverge on the columns this work added."""
    async def _run():
        from sqlalchemy import text
        from db_connection import db_manager
        import db_models
        await _ensure_db()
        table = db_models.Base.metadata.tables["identity_embeddings"]
        orm_cols = {c.name for c in table.columns}
        async with db_manager.get_session() as db:
            db_cols = {r[0] for r in (await db.execute(text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name='identity_embeddings'"))).all()}
        return orm_cols, db_cols
    orm_cols, db_cols = run_async(_run())
    assert "vector_index_sync_state" in orm_cols and "vector_index_sync_state" in db_cols
    assert "embedding_model_version" in orm_cols and "embedding_model_version" in db_cols
    assert orm_cols == db_cols, (
        f"ORM/db divergence: orm-only={orm_cols - db_cols}, db-only={db_cols - orm_cols}")

    async def _images():
        from sqlalchemy import text
        from db_connection import db_manager
        import db_models
        table = db_models.Base.metadata.tables["identity_images"]
        async with db_manager.get_session() as db:
            db_cols = {r[0] for r in (await db.execute(text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name='identity_images'"))).all()}
        return {c.name for c in table.columns}, db_cols
    orm_img, db_img = run_async(_images())
    assert "faiss_sync_state" not in orm_img and "faiss_sync_state" not in db_img
    assert orm_img == db_img


# ---------------------------------------------------------------------------
# The rule: a vector survives a downgrade
# ---------------------------------------------------------------------------

def test_downgrade_does_not_touch_the_vector_column():
    """Source-level pin.

    A real upgrade→downgrade→upgrade round-trip cannot run here: downgrading
    live would drop columns out from under the running application. So this
    asserts the property directly on the migration text — the downgrade must
    never name the authoritative column in a destructive statement.
    """
    with open("/app/alembic/versions/f2a3b4c5d6e7_vector_index_sync_state.py",
              encoding="utf-8") as handle:
        source = handle.read()
    downgrade = source.split("def downgrade()", 1)[1]

    for forbidden in ("drop_column('identity_embeddings', 'embedding')",
                      'drop_column("identity_embeddings", "embedding")',
                      "drop_table('identity_embeddings')",
                      "DELETE FROM identity_embeddings",
                      "UPDATE identity_embeddings SET embedding"):
        assert forbidden not in downgrade, (
            f"downgrade would destroy authoritative vectors: {forbidden}")

    # It drops exactly the two columns this migration added, and nothing else.
    assert "drop_column('identity_embeddings', 'vector_index_sync_state')" in downgrade
    assert "drop_column('identity_embeddings', 'embedding_model_version')" in downgrade
    assert downgrade.count("drop_column('identity_embeddings'") == 2


def test_a_stored_vector_survives_a_simulated_column_drop():
    """Behavioural proof on a scratch table shaped like the real one: dropping
    the state columns leaves the vector bytes intact."""
    async def _run():
        from sqlalchemy import text
        from db_connection import db_manager
        await _ensure_db()
        table = f"qa_downgrade_probe_{uuid_module.uuid4().hex[:8]}"
        vector = "[" + ",".join(["0.125"] * 512) + "]"
        async with db_manager.get_session() as db:
            await db.execute(text(
                f"CREATE TABLE {table} (id serial PRIMARY KEY, embedding vector(512), "
                f"vector_index_sync_state varchar(16) NOT NULL DEFAULT 'pending', "
                f"embedding_model_version varchar(64))"))
            await db.execute(text(
                f"INSERT INTO {table} (embedding) VALUES (:v)"), {"v": vector})
            await db.commit()
            before = (await db.execute(text(
                f"SELECT embedding::text FROM {table} LIMIT 1"))).scalar()
            # the downgrade's exact operations
            await db.execute(text(f"ALTER TABLE {table} DROP COLUMN embedding_model_version"))
            await db.execute(text(f"ALTER TABLE {table} DROP COLUMN vector_index_sync_state"))
            await db.commit()
            after = (await db.execute(text(
                f"SELECT embedding::text FROM {table} LIMIT 1"))).scalar()
            rows = (await db.execute(text(f"SELECT count(*) FROM {table}"))).scalar()
            await db.execute(text(f"DROP TABLE {table}"))
            await db.commit()
            return before, after, rows
    before, after, rows = run_async(_run())
    assert rows == 1, "the downgrade lost a row"
    assert before == after, "the downgrade altered the stored vector"


# ---------------------------------------------------------------------------
# State machine
# ---------------------------------------------------------------------------

def test_only_the_documented_states_are_used_in_code():
    """pending | synced | failed — and deletion is NOT a state."""
    import re

    with open("/app/backend/core/identity_service.py", encoding="utf-8") as handle:
        source = handle.read()
    assigned = set(re.findall(r"vector_index_sync_state\s*=\s*['\"](\w+)['\"]", source))
    assert assigned, "no sync-state assignments found — did the write path change?"
    assert assigned <= LEGAL_STATES, f"illegal sync state(s) in use: {assigned - LEGAL_STATES}"
    assert "deleted" not in assigned


def test_new_rows_default_to_pending():
    """A row that nothing has indexed yet must not claim to be synced."""
    async def _run():
        from sqlalchemy import text
        from db_connection import db_manager
        await _ensure_db()
        async with db_manager.get_session() as db:
            ident = (await db.execute(text(
                "INSERT INTO identities (id, type, status, display_name, first_seen_at, "
                " last_seen_at, created_at, updated_at, appearances_count) "
                "VALUES (gen_random_uuid(), 'KNOWN', 'ACTIVE', 'qa-migr-probe', now(), "
                " now(), now(), now(), 0) RETURNING id"))).scalar()
            await db.execute(text(
                "INSERT INTO pipelines (pipeline_id, created_at, updated_at, total_detections, is_active) "
                "VALUES ('qa-probe', now(), now(), 0, 1) ON CONFLICT (pipeline_id) DO NOTHING"))
            emb_id = (await db.execute(text(
                "INSERT INTO identity_embeddings (identity_id, pipeline_id, created_at) "
                "VALUES (:i, 'qa-probe', now()) RETURNING id"), {"i": ident})).scalar()
            await db.commit()
            state = (await db.execute(text(
                "SELECT vector_index_sync_state FROM identity_embeddings WHERE id=:e"),
                {"e": emb_id})).scalar()
            await db.execute(text("DELETE FROM identity_embeddings WHERE id=:e"), {"e": emb_id})
            await db.execute(text("DELETE FROM identities WHERE id=:i"), {"i": ident})
            await db.commit()
            return state
    assert run_async(_run()) == "pending"

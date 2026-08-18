"""
ONE-TIME generator for alembic/versions/000_baseline_schema.py (kept for the
record; the generated file is a frozen literal and never regenerated
automatically).

Why a baseline exists: the first 28 revisions were written against databases
whose base tables had been created by `Base.metadata.create_all()` at boot —
revision 001 ALTERs `identity_embeddings`, a table no migration creates. With
`create_all()` removed from the application (Alembic is the only schema
initializer), an empty database needs a root revision that creates the 24
create_all-era tables in the shape those revisions expect.

Shape rule: the CURRENT ORM definition of each of the 24 tables, minus the
artifacts of the four historical revisions that add to them without an
existence guard (they must still find their work to do on a fresh database):

    002  pipelines.latitude / longitude / location_name + idx_pipeline_coordinates
    a7b8 pipelines.timezone
    001  identity_embeddings.embedding (vector) + idx_embedding_vector_hnsw
         (569b then drops/re-creates that index; b7c8 re-creates it)
    d0e1 identity_embeddings.image_id + fk + ix
    f2a3 identity_embeddings.vector_index_sync_state / embedding_model_version
         + idx_embedding_sync_state
    d1e2 background_task_history job/lifecycle columns (guarded, but with server
         defaults the ORM lacks) + their indexes
    f4b5 watchlists.version / deleted_at / deleted_by_user_id / deletion_reason (same)

Every other revision guards its additions (IF NOT EXISTS / _has_column), so a
current-shape baseline is correct for them. Verified by
tests/test_migration_schema_parity.py: a fresh `alembic upgrade head` database
must equal the development database schema (columns, types, nullability,
defaults, PK/FK/unique/check constraints, indexes, enums).

Run inside the api container:  python scripts/dev/generate_baseline_migration.py
"""
import os
import re
import sys

sys.path.insert(0, "/app")

from sqlalchemy import MetaData
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateTable, CreateIndex

import db_models  # noqa: F401
from db_models import Base

ERA_TABLES = [
    'background_task_history', 'chatbot_audit_log', 'detections', 'faces', 'identities',
    'identity_appearances', 'identity_audit_log', 'identity_embeddings', 'identity_merges',
    'identity_relationships', 'live_alert_triggers', 'live_search_alerts', 'merge_suggestions',
    'pipelines', 'search_history', 'settings', 'settings_audit_log', 'similarity_training_data',
    'system_metrics', 'user_pipeline_access', 'users', 'watchlist_alerts', 'watchlist_entries',
    'watchlists',
]
EXCLUDE_COLUMNS = {
    'pipelines': {'latitude', 'longitude', 'location_name', 'timezone'},   # 002 + a7b8 add these unguarded
    'identity_embeddings': {'embedding', 'image_id', 'vector_index_sync_state', 'embedding_model_version'},
    # d1e2 / f4b5 add these guarded (IF NOT EXISTS) but WITH server defaults the ORM
    # does not declare; leaving them to those revisions keeps fresh == migrated.
    'background_task_history': {'job_id', 'progress_percent', 'result', 'retry_count', 'max_retries', 'error_code',
                                'error_message', 'created_by_user_id', 'request_id', 'correlation_id', 'worker_name',
                                'hostname', 'updated_at'},
    'watchlists': {'version', 'deleted_at', 'deleted_by_user_id', 'deletion_reason'},
}
EXCLUDE_INDEXES = {'idx_pipeline_coordinates', 'idx_embedding_vector_hnsw', 'ix_identity_embeddings_image_id',
                   'idx_embedding_sync_state', 'idx_task_history_job_id', 'idx_task_history_correlation',
                   'uq_task_history_job_id', 'idx_watchlists_deleted'}

dialect = postgresql.dialect()
md = Base.metadata
tables = [t for t in md.sorted_tables if t.name in ERA_TABLES]
assert len(tables) == len(ERA_TABLES), (len(tables), [t.name for t in tables])

# enums used by these tables
enum_types = {}
for t in tables:
    for c in t.columns:
        if isinstance(c.type, postgresql.ENUM) or getattr(c.type, "enums", None):
            name = c.type.name
            if name and name not in enum_types:
                enum_types[name] = list(c.type.enums)

statements = []
for name, labels in enum_types.items():
    quoted = ", ".join("'" + l.replace("'", "''") + "'" for l in labels)
    statements.append(f"CREATE TYPE {name} AS ENUM ({quoted})")

for t in tables:
    ddl = str(CreateTable(t).compile(dialect=dialect)).strip()
    excl = EXCLUDE_COLUMNS.get(t.name, set())
    if excl:
        kept = []
        for line in ddl.splitlines():
            stripped = line.strip()
            drop = False
            for col in excl:
                if re.match(rf"^{col}\s", stripped) or re.search(rf"\b(FOREIGN KEY|UNIQUE|PRIMARY KEY|CHECK)\s*\(.*\b{col}\b", stripped):
                    drop = True
            if drop:
                continue
            kept.append(line)
        ddl = "\n".join(kept)
        # remove a trailing comma before the closing paren
        ddl = re.sub(r",\s*\n\)", "\n)", ddl)
    ddl = re.sub(r"CREATE TABLE ", "CREATE TABLE IF NOT EXISTS ", ddl, count=1)
    statements.append(ddl)
    for idx in sorted(t.indexes, key=lambda i: i.name or ""):
        if idx.name in EXCLUDE_INDEXES:
            continue
        cols = {c.name for c in idx.columns}
        if cols & excl:
            continue
        s = str(CreateIndex(idx).compile(dialect=dialect)).strip()
        s = re.sub(r"CREATE (UNIQUE )?INDEX ", lambda m: f"CREATE {m.group(1) or ''}INDEX IF NOT EXISTS ", s, count=1)
        statements.append(s)

body = ",\n".join("    " + repr(s) for s in statements)
out = f'''"""Baseline schema — the 24 tables that predate Alembic in this repository.

Revision ID: 000_baseline
Revises: (root)

Historically these tables were created by `Base.metadata.create_all()` at boot
and the migration chain started from revision 001 by ALTERing them. The
application no longer calls create_all() anywhere (Alembic is the ONLY schema
initializer, verified at boot), so an empty database needs this root revision.

The DDL below is a FROZEN literal generated once from the ORM
(scripts/dev/generate_baseline_migration.py — see there for the shape rule:
current ORM shape minus the artifacts of the four historical revisions that add
to these tables without an existence guard: 002, a7b8, 001/569b, d0e1, f2a3). It is
never regenerated automatically; a fresh `alembic upgrade head` database is
proven equal to the development schema by tests/test_migration_schema_parity.py.

Idempotent: every statement is IF NOT EXISTS, and it is skipped when the
tables already exist (databases created before this revision are stamped
past it: they were at head when it was introduced).
"""
from alembic import op
import sqlalchemy as sa

revision = '000_baseline'
down_revision = None
branch_labels = None
depends_on = None

STATEMENTS = [
{body}
]


def _enum_exists(bind, name: str) -> bool:
    return bool(bind.execute(sa.text("SELECT 1 FROM pg_type WHERE typname = :n"), {{"n": name}}).scalar())


def upgrade() -> None:
    bind = op.get_bind()
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    for stmt in STATEMENTS:
        if stmt.startswith("CREATE TYPE "):
            name = stmt.split()[2]
            if _enum_exists(bind, name):
                continue
        op.execute(sa.text(stmt))


def downgrade() -> None:
    """The root revision is not reversible: dropping the base tables destroys
    every row the system has. Downgrade to nothing is refused."""
    raise RuntimeError("000_baseline cannot be downgraded: it would drop the base schema")
'''
path = "/app/alembic/versions/000_baseline_schema.py"
with open(path, "w", encoding="utf-8", newline="\n") as fh:
    fh.write(out)
print(f"wrote {path}: {len(statements)} statements ({len(enum_types)} enums, {len(tables)} tables)")

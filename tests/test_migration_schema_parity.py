"""
Alembic is the ONLY schema initializer (plan §11).

    * a fresh `alembic upgrade head` on an EMPTY database yields exactly the
      development schema (columns, types, nullability, defaults, constraints,
      indexes, enums) — 0 diffs;
    * every constraint this pass added exists there by name; the ml_model_
      thresholds column set is the final one; the migration-frozen feature
      definitions (24), retraining policies (4) and the `system` principal are
      seeded; the application's head check (init_db) passes against it;
    * the migrations' preconditions REFUSE (never delete) incompatible rows —
      proven only on a scratch database that is destroyed afterwards;
    * no `create_all` remains under backend/, db_connection.py, scripts/.

Every scratch database has a UNIQUE name (pid + uuid) and is dropped in a
`finally` — parallel runs cannot collide and a killed run leaves a harmless,
identifiable database behind.

    docker exec face_recognition_api python -m pytest tests/test_migration_schema_parity.py -q
"""
import json
import os
import re
import subprocess
import sys
import uuid

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ALEMBIC_INI = os.path.join(REPO, "alembic", "alembic.ini")
PREVIOUS_HEAD = "b0c1d2e3f4a5"      # last revision before this corrective pass
NEW_CONSTRAINTS = [
    # c2d3e4f5a6b7 relationship integrity
    "fk_identity_embeddings_pipeline", "fk_identity_appearances_pipeline",
    "fk_watchlist_alerts_pipeline", "fk_live_alert_triggers_pipeline", "fk_watchlist_alerts_search",
    "uq_watchlist_alert_entry_detection", "fk_watchlists_deleted_by", "fk_similarity_model_registry_activated_by",
    "fk_ml_models_previous_production", "ck_ml_models_prev_not_self",
    "fk_user_query_history_session", "fk_user_conversation_memory_session", "fk_conversations_session",
    # d4e5f6a7b8c9 ml lineage
    "uq_ml_threshold_scope_version", "uq_ml_threshold_one_active", "ck_ml_threshold_scope_canonical",
    "ck_ml_threshold_status", "ck_ml_threshold_cutpoints", "ck_ml_threshold_source",
    "idx_ml_pred_assessment", "idx_ml_pred_subject_event", "idx_ml_pred_outcome_label",
    # f6a7b8c9d0e1 prediction lineage RESTRICT (names kept; rule asserted below)
    "ml_predictions_model_id_fkey", "ml_predictions_threshold_id_fkey", "ml_shadow_comparisons_model_id_fkey",
    # migration-only constraints that predate this pass (never in create_all)
    "uq_alert_trigger_alert_detection", "uq_watchlists_name_live", "uq_model_registry_one_active",
]
THRESHOLD_COLUMNS = {"id", "model_id", "scope_type", "scope_id", "version", "status", "cutpoints", "quantiles",
                     "source", "expected_metrics", "sample_count", "created_at", "activated_at", "activated_by",
                     "retired_at", "retired_by", "notes"}


# ---------------------------------------------------------------- scratch DB plumbing

def _sync_url(async_url: str, database: str) -> str:
    url = make_url(async_url.replace("+asyncpg", "+psycopg2"))
    return url.set(database=database).render_as_string(hide_password=False)


def _async_url(async_url: str, database: str) -> str:
    return make_url(async_url).set(database=database).render_as_string(hide_password=False)


def _dev_url() -> str:
    from config import settings
    return settings.DATABASE_URL


class Scratch:
    """CREATE DATABASE <unique> … DROP DATABASE in __exit__ (also on failure)."""

    def __init__(self, tag: str):
        self.name = f"face_recognition_{tag}_{os.getpid()}_{uuid.uuid4().hex[:12]}"
        self.admin = create_engine(_sync_url(_dev_url(), "postgres"), isolation_level="AUTOCOMMIT")

    def __enter__(self):
        with self.admin.connect() as c:
            assert c.execute(text("SELECT rolsuper FROM pg_roles WHERE rolname = current_user")).scalar(), \
                "scratch databases need a superuser connection"
            c.execute(text(f'CREATE DATABASE "{self.name}"'))
        self.sync_url = _sync_url(_dev_url(), self.name)
        self.async_url = _async_url(_dev_url(), self.name)
        return self

    def __exit__(self, *exc):
        with self.admin.connect() as c:
            c.execute(text("SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                           "WHERE datname = :n AND pid <> pg_backend_pid()"), {"n": self.name})
            c.execute(text(f'DROP DATABASE IF EXISTS "{self.name}"'))
        self.admin.dispose()
        return False

    def alembic(self, *args, check=True):
        env = dict(os.environ, DATABASE_URL=self.async_url, PYTHONPATH=REPO)
        proc = subprocess.run([sys.executable, "-m", "alembic", "-c", ALEMBIC_INI, *args],
                              cwd=os.path.join(REPO, "alembic"), env=env, capture_output=True, text=True, timeout=600)
        if check and proc.returncode != 0:
            raise AssertionError(f"alembic {' '.join(args)} failed:\n{proc.stdout[-2000:]}\n{proc.stderr[-4000:]}")
        return proc

    def sql(self, statement, params=None, scalar=False):
        eng = create_engine(self.sync_url)
        try:
            with eng.begin() as c:
                res = c.execute(text(statement), params or {})
                if not res.returns_rows:
                    return res.rowcount
                return res.scalar() if scalar else res.all()
        finally:
            eng.dispose()


def _dump(sync_url: str) -> dict:
    """Normalized schema (same shape as scripts/dev/dump_schema.py)."""
    eng = create_engine(sync_url)
    out = {"tables": {}, "enums": {}}
    try:
        with eng.connect() as c:
            tables = [r[0] for r in c.execute(text("SELECT tablename FROM pg_tables WHERE schemaname='public' ORDER BY 1"))]
            for t in tables:
                cols = c.execute(text(
                    "SELECT column_name, data_type, udt_name, is_nullable, column_default, character_maximum_length "
                    "FROM information_schema.columns WHERE table_schema='public' AND table_name=:t ORDER BY column_name"),
                    {"t": t}).all()
                cons = c.execute(text("SELECT contype, pg_get_constraintdef(oid) FROM pg_constraint "
                                      "WHERE conrelid = CAST(:t AS regclass) ORDER BY 2"), {"t": t}).all()
                idx = c.execute(text("SELECT indexdef FROM pg_indexes WHERE schemaname='public' AND tablename=:t ORDER BY 1"),
                                {"t": t}).all()
                out["tables"][t] = {
                    "columns": {r[0]: [r[1], r[2], r[3], (r[4] or "").replace("::text", "").replace("'", ""), r[5]]
                                for r in cols},
                    "constraints": sorted(f"{r[0]}:{r[1]}" for r in cons),
                    "indexes": sorted(r[0].split(" USING ", 1)[1] if " USING " in r[0] else r[0] for r in idx),
                }
            enums = c.execute(text("SELECT t.typname, string_agg(e.enumlabel, ',' ORDER BY e.enumsortorder) "
                                   "FROM pg_type t JOIN pg_enum e ON e.enumtypid=t.oid GROUP BY 1 ORDER BY 1")).all()
            out["enums"] = {r[0]: r[1] for r in enums}
    finally:
        eng.dispose()
    return out


def _diff(a: dict, b: dict) -> list:
    diffs = []
    for t in sorted(set(a["tables"]) | set(b["tables"])):
        if t not in a["tables"] or t not in b["tables"]:
            diffs.append(f"table {t}: only in {'fresh' if t in a['tables'] else 'dev'}")
            continue
        for part in ("columns", "constraints", "indexes"):
            if a["tables"][t][part] != b["tables"][t][part]:
                diffs.append(f"{t}.{part}: fresh={a['tables'][t][part]} dev={b['tables'][t][part]}")
    if a["enums"] != b["enums"]:
        diffs.append(f"enums: fresh={a['enums']} dev={b['enums']}")
    return diffs


# ---------------------------------------------------------------- the fresh database

@pytest.fixture(scope="module")
def fresh():
    with Scratch("migration_test") as s:
        s.alembic("upgrade", "head")
        yield s


def test_fresh_upgrade_head_equals_the_development_schema(fresh):
    diffs = _diff(_dump(fresh.sync_url), _dump(_sync_url(_dev_url(), make_url(_dev_url()).database)))
    assert diffs == [], "\n".join(diffs)


def test_fresh_database_is_at_the_scripts_head_and_the_app_check_passes(fresh):
    from backend.utils.migrations import expected_head_from_scripts, verify_database_head
    from sqlalchemy.ext.asyncio import create_async_engine
    from conftest import run_on_shared_loop
    head = expected_head_from_scripts()
    assert fresh.sql("SELECT version_num FROM alembic_version") == [(head,)]

    async def boot():
        eng = create_async_engine(fresh.async_url)
        try:
            return await verify_database_head(eng)     # exactly what DatabaseManager.init_db runs
        finally:
            await eng.dispose()
    assert run_on_shared_loop(boot()) == head
    # and the full DatabaseManager boot in a subprocess with the scratch DSN
    code = ("import asyncio, os\n"
            "from db_connection import DatabaseManager\n"
            "async def m():\n"
            "    dm = DatabaseManager(); await dm.init_db(); await dm.close_db()\n"
            "asyncio.run(m()); print('BOOT_OK')\n")
    env = dict(os.environ, DATABASE_URL=fresh.async_url, PYTHONPATH=REPO)
    proc = subprocess.run([sys.executable, "-c", code], cwd=REPO, env=env, capture_output=True, text=True, timeout=300)
    assert proc.returncode == 0 and "BOOT_OK" in proc.stdout, proc.stderr[-3000:]


def test_every_new_constraint_exists_by_name_on_the_fresh_database(fresh):
    names = {r[0] for r in fresh.sql("SELECT conname FROM pg_constraint")}
    names |= {r[0] for r in fresh.sql("SELECT indexname FROM pg_indexes WHERE schemaname = 'public'")}
    missing = [n for n in NEW_CONSTRAINTS if n not in names]
    assert missing == [], missing
    rules = dict(fresh.sql("SELECT conname, confdeltype::text FROM pg_constraint WHERE conname IN "
                           "('ml_predictions_model_id_fkey', 'ml_predictions_threshold_id_fkey', "
                           " 'ml_shadow_comparisons_model_id_fkey', 'ml_drift_reports_model_id_fkey')"))
    assert rules == {"ml_predictions_model_id_fkey": "r", "ml_predictions_threshold_id_fkey": "r",
                     "ml_shadow_comparisons_model_id_fkey": "r", "ml_drift_reports_model_id_fkey": "c"}, rules


def test_fresh_threshold_columns_seeds_and_principal(fresh):
    cols = {r[0] for r in fresh.sql("SELECT column_name FROM information_schema.columns "
                                    "WHERE table_name = 'ml_model_thresholds'")}
    assert cols == THRESHOLD_COLUMNS, cols ^ THRESHOLD_COLUMNS
    assert fresh.sql("SELECT count(*) FROM ml_feature_definitions", scalar=True) == 26
    assert fresh.sql("SELECT count(*) FROM ml_retraining_policies", scalar=True) == 4
    assert fresh.sql("SELECT count(*) FROM users WHERE username = 'system'", scalar=True) == 1
    for col in ("image_path",):
        assert fresh.sql("SELECT count(*) FROM information_schema.columns WHERE table_name = 'detections' "
                         "AND column_name = :c", {"c": col}, scalar=True) == 0
    assert fresh.sql("SELECT count(*) FROM information_schema.columns WHERE table_name = 'pending_enrollments' "
                     "AND column_name = 'checksum_match_identity_id'", scalar=True) == 0
    assert fresh.sql("SELECT is_nullable FROM information_schema.columns WHERE table_name = 'ml_drift_reports' "
                     "AND column_name = 'model_id'", scalar=True) == "NO"


def test_feature_seed_is_idempotent_and_matches_the_runtime_inventory(fresh):
    """Re-running the frozen seed adds nothing; FEATURE_INVENTORY ≡ the union of
    migration-frozen rows (name/version/spec) so drift fails CI, not production."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "d4e5", os.path.join(REPO, "alembic", "versions", "d4e5f6a7b8c9_ml_lineage.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    frozen = {(r["name"], r["version"]): dict(r) for r in mod.FROZEN_FEATURE_DEFINITIONS}
    # secintel-features-v2 (b7d2f4a9c6e1) extends the frozen set: two v1 rows
    # are deactivated and two v2 rows added. The union of BOTH migrations is
    # the frozen literal the runtime inventory must match.
    spec2 = importlib.util.spec_from_file_location(
        "b7d2", os.path.join(REPO, "alembic", "versions", "b7d2f4a9c6e1_feature_set_v2.py"))
    mod2 = importlib.util.module_from_spec(spec2)
    spec2.loader.exec_module(mod2)
    for name in mod2.SUPERSEDED_V1:
        frozen[(name, 1)]["is_active"] = False
    for r in mod2.V2_DEFINITIONS:
        frozen[(r["name"], r["version"])] = dict(r)
    before = fresh.sql("SELECT count(*) FROM ml_feature_definitions", scalar=True)
    for r in frozen.values():
        fresh.sql("INSERT INTO ml_feature_definitions (id, name, version, entity_type, value_type, source, computation, "
                  " params, leakage_class, is_active, created_at) VALUES (gen_random_uuid(), :name, :version, "
                  " :entity_type, :value_type, :source, :computation, CAST(:params AS jsonb), :leakage_class, "
                  " :is_active, now()) ON CONFLICT (name, version) DO NOTHING",
                  {**{k: r[k] for k in ("name", "version", "entity_type", "value_type", "source", "computation",
                                        "leakage_class", "is_active")}, "params": json.dumps(r["params"])})
    assert fresh.sql("SELECT count(*) FROM ml_feature_definitions", scalar=True) == before == len(frozen)
    from backend.ml.feature_store import FEATURE_INVENTORY
    inv = {(i["name"], int(i.get("version", 1))): i for i in FEATURE_INVENTORY}
    assert set(inv) == set(frozen), set(inv) ^ set(frozen)
    for key, item in inv.items():
        f = frozen[key]
        assert (item["entity_type"], item.get("window"), item["source"], item["computation"],
                dict(item.get("params", {})), item.get("leakage_class", "safe"), bool(item.get("is_active", True))) == \
               (f["entity_type"], f["window"], f["source"], f["computation"], dict(f["params"]),
                f["leakage_class"], bool(f["is_active"])), key


def test_orm_tables_are_a_subset_of_the_fresh_database(fresh):
    import db_models  # noqa: F401
    from db_models import Base
    live = {r[0] for r in fresh.sql("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")}
    missing = sorted(set(Base.metadata.tables) - live)
    assert missing == [], missing


# ---------------------------------------------------------------- preconditions (scratch only)

def test_preconditions_refuse_without_deleting():
    """Scratch DB at the previous head + representative incompatible rows →
    `alembic upgrade head` fails naming the count and the repair command; the
    rows still exist and the version did not move. Never run on the dev DB."""
    with Scratch("precondition_test") as s:
        s.alembic("upgrade", PREVIOUS_HEAD)
        assert s.sql("SELECT version_num FROM alembic_version", scalar=True) == PREVIOUS_HEAD
        # 000_baseline builds the era tables from the CURRENT ORM shape, so a
        # scratch chain already carries the auto-named FKs that c2d3e4f5a6b7
        # later replaces by name; a create_all-era database (the one the
        # preconditions exist for) had none — reproduce that shape exactly.
        for table, column in (("identity_appearances", "pipeline_id"), ("identity_embeddings", "pipeline_id"),
                              ("watchlist_alerts", "search_id")):
            for (con,) in s.sql("SELECT conname FROM pg_constraint WHERE contype = 'f' AND conrelid = CAST(:t AS regclass) "
                                "AND (SELECT array_agg(attname) FROM pg_attribute WHERE attrelid = conrelid "
                                "     AND attnum = ANY(conkey)) = ARRAY[CAST(:c AS name)]", {"t": table, "c": column}):
                s.sql(f"ALTER TABLE {table} DROP CONSTRAINT {con}")
        ident = str(uuid.uuid4())
        s.sql("INSERT INTO identities (id, type, status, first_seen_at, last_seen_at, appearances_count, created_at, updated_at) "
              "VALUES (CAST(:i AS uuid), 'UNKNOWN', 'ACTIVE', now(), now(), 0, now(), now())", {"i": ident})
        s.sql("INSERT INTO identity_appearances (identity_id, pipeline_id, start_time, created_at) "
              "VALUES (CAST(:i AS uuid), 'ghost-cam-a', now(), now())", {"i": ident})
        s.sql("INSERT INTO identity_embeddings (identity_id, pipeline_id, created_at) "
              "VALUES (CAST(:i AS uuid), 'ghost-cam-b', now())", {"i": ident})
        wl = s.sql("INSERT INTO watchlists (id, name, alert_level, notify_dashboard, notify_email, notify_sms, "
                   " notify_webhook, is_active, created_at, version) VALUES (gen_random_uuid(), 'qa-precondition', "
                   " 'WARNING', true, false, false, false, true, now(), 1) RETURNING id", scalar=True)
        entry = s.sql("INSERT INTO watchlist_entries (id, watchlist_id, identity_id, priority, added_at, is_active) "
                      "VALUES (gen_random_uuid(), :w, CAST(:i AS uuid), 'NORMAL', now(), true) RETURNING id",
                      {"w": wl, "i": ident}, scalar=True)
        s.sql("INSERT INTO watchlist_alerts (id, watchlist_entry_id, triggered_by, search_id, acknowledged, created_at) "
              "VALUES (gen_random_uuid(), :e, 'search', gen_random_uuid(), false, now())", {"e": entry})
        s.sql("INSERT INTO ml_drift_reports (id, report_kind, model_id, scope_type, scope_id, window_start, window_end, "
              " sample_count, insufficient_data, metrics, severity, created_at) VALUES (gen_random_uuid(), 'data', NULL, "
              " 'global', '', now(), now(), 0, true, '{}', 'none', now())")

        proc = s.alembic("upgrade", "head", check=False)
        assert proc.returncode != 0, "upgrade must be refused"
        err = proc.stderr + proc.stdout
        assert "refuses" in err and "row(s)" in err and "repair_relationship_integrity.py" in err, err[-2500:]
        assert re.search(r"refuses: [1-9]\d* row\(s\)", err), "the error names the count"
        # nothing was deleted, nothing moved
        assert s.sql("SELECT count(*) FROM identity_appearances WHERE pipeline_id = 'ghost-cam-a'", scalar=True) == 1
        assert s.sql("SELECT count(*) FROM identity_embeddings WHERE pipeline_id = 'ghost-cam-b'", scalar=True) == 1
        assert s.sql("SELECT count(*) FROM watchlist_alerts WHERE search_id IS NOT NULL", scalar=True) == 1
        assert s.sql("SELECT count(*) FROM ml_drift_reports WHERE model_id IS NULL", scalar=True) == 1
        assert s.sql("SELECT version_num FROM alembic_version", scalar=True) == PREVIOUS_HEAD
        assert s.sql("SELECT count(*) FROM pg_constraint WHERE conname = 'fk_identity_appearances_pipeline'",
                     scalar=True) == 0


# ---------------------------------------------------------------- source contract

def test_no_create_all_remains_in_application_code():
    """AST scan (not substrings — docstrings legitimately say the words):
    no CALL of `<anything>.create_all(...)` under backend/, scripts/, db_connection.py."""
    import ast
    offenders = []
    roots = [os.path.join(REPO, "backend"), os.path.join(REPO, "scripts"), os.path.join(REPO, "db_connection.py")]
    for root in roots:
        paths = [root] if os.path.isfile(root) else [
            os.path.join(d, f) for d, _dirs, files in os.walk(root) for f in files if f.endswith(".py")]
        for path in paths:
            src = open(path, encoding="utf-8", errors="replace").read()
            try:
                tree = ast.parse(src)
            except SyntaxError:
                continue
            for node in ast.walk(tree):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "create_all":
                    offenders.append(f"{os.path.relpath(path, REPO)}:{node.lineno}")
    assert offenders == [], offenders

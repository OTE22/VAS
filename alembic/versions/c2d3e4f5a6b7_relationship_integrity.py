"""Relationship integrity: real FKs where a real relationship exists, dead
columns removed, chat-session parents formalised, merge-suggestion lifecycle.

Revision ID: c2d3e4f5a6b7
Revises: b0c1d2e3f4a5

This migration is DDL plus NON-DESTRUCTIVE data migrations only. It never
deletes application rows. Before every constraint it is about to add it checks
the precondition with `_require_zero(...)` and REFUSES (RuntimeError naming the
count and the operator command) if any row would violate it. The destructive /
uncertain-provenance repairs live in `scripts/repair_relationship_integrity.py`,
which is operator-run, dry-run by default and refuses production.

Order matters and is documented inline:

  identity_embeddings.pipeline_id
      DROP NOT NULL
      → sentinel 'uploaded'/'preloaded' → NULL   (deterministic semantic remap:
                                                  enrollment/preload rows were
                                                  never camera sightings)
      → precondition: no other non-existent pipeline id
      → FK → pipelines(pipeline_id) ON DELETE RESTRICT

  chat sessions
      user_conversation_sessions.user_id DROP NOT NULL, CASCADE → SET NULL
      → backfill missing session parents from history rows (user_id may be NULL
        for history whose user was deleted)
      → precondition
      → FKs (user_query_history.session_id, user_conversation_memory
        .source_session_id, conversations.legacy_session_id) SET NULL

Delete policy (one policy, documented in Docs/87): a camera with evidence is
never hard-deleted — `pipelines.is_active` is the operational switch — so every
evidence FK to pipelines is RESTRICT (detections changes CASCADE → RESTRICT);
denormalised where-it-fired columns on alert history are SET NULL.
"""
from alembic import op
import sqlalchemy as sa

revision = 'c2d3e4f5a6b7'
down_revision = 'b0c1d2e3f4a5'
branch_labels = None
depends_on = None

REPAIR = "python scripts/repair_relationship_integrity.py --apply --yes-i-understand (dry-run first; dev/demo only)"


def _require_zero(bind, sql: str, what: str) -> None:
    """Refuse — never delete — when rows would violate the constraint about to be added."""
    n = bind.execute(sa.text(sql)).scalar() or 0
    if n:
        raise RuntimeError(
            f"migration c2d3e4f5a6b7 refuses: {n} row(s) {what}. "
            f"This migration never deletes data; repair first: {REPAIR}")


def _fk_name(bind, table: str, column: str, ref_table: str):
    return bind.execute(sa.text("""
        SELECT conname FROM pg_constraint
        WHERE contype = 'f'
          AND confrelid = CAST(:r AS regclass)
          AND conrelid = CAST(:t AS regclass)
          AND (SELECT array_agg(attname) FROM pg_attribute
               WHERE attrelid = conrelid AND attnum = ANY(conkey)) = ARRAY[CAST(:c AS name)]
    """), {"t": table, "c": column, "r": ref_table}).scalar()


def _add_fk(table, column, ref_table, ref_column, rule, name):
    """Add the named FK, REPLACING any FK already on (table.column → ref_table)
    — on a database created from the 000_baseline the ORM shape already carries
    an auto-named FK there; two constraints on one column would be wrong."""
    bind = op.get_bind()
    existing = bind.execute(sa.text("""
        SELECT conname FROM pg_constraint
        WHERE contype = 'f' AND conrelid = CAST(:t AS regclass) AND confrelid = CAST(:r AS regclass)
          AND (SELECT array_agg(attname) FROM pg_attribute
               WHERE attrelid = conrelid AND attnum = ANY(conkey)) = ARRAY[CAST(:c AS name)]
    """), {"t": table, "r": ref_table, "c": column}).scalars().all()
    for con in set(list(existing) + [name]):
        op.execute(f'ALTER TABLE {table} DROP CONSTRAINT IF EXISTS {con}')
    op.execute(f'ALTER TABLE {table} ADD CONSTRAINT {name} FOREIGN KEY ({column}) '
               f'REFERENCES {ref_table} ({ref_column}) ON DELETE {rule}')


def upgrade() -> None:
    bind = op.get_bind()

    # ---------------------------------------------------------------- pipelines
    # identity_embeddings: nullable first, then the deterministic remap, then the
    # precondition, then the FK — in exactly this order.
    op.execute('ALTER TABLE identity_embeddings ALTER COLUMN pipeline_id DROP NOT NULL')
    op.execute("UPDATE identity_embeddings SET pipeline_id = NULL "
               "WHERE pipeline_id IN ('uploaded', 'preloaded')")
    _require_zero(bind,
        "SELECT count(*) FROM identity_embeddings e WHERE e.pipeline_id IS NOT NULL "
        "AND NOT EXISTS (SELECT 1 FROM pipelines p WHERE p.pipeline_id = e.pipeline_id)",
        "in identity_embeddings reference a pipeline_id that does not exist")
    _add_fk('identity_embeddings', 'pipeline_id', 'pipelines', 'pipeline_id', 'RESTRICT',
            'fk_identity_embeddings_pipeline')

    _require_zero(bind,
        "SELECT count(*) FROM identity_appearances a WHERE NOT EXISTS "
        "(SELECT 1 FROM pipelines p WHERE p.pipeline_id = a.pipeline_id)",
        "in identity_appearances reference a pipeline_id that does not exist")
    _add_fk('identity_appearances', 'pipeline_id', 'pipelines', 'pipeline_id', 'RESTRICT',
            'fk_identity_appearances_pipeline')

    # detections: CASCADE → RESTRICT (one delete policy for camera evidence)
    name = _fk_name(bind, 'detections', 'pipeline_id', 'pipelines') or 'detections_pipeline_id_fkey'
    op.execute(f'ALTER TABLE detections DROP CONSTRAINT IF EXISTS {name}')
    op.execute(f'ALTER TABLE detections ADD CONSTRAINT {name} FOREIGN KEY (pipeline_id) '
               f'REFERENCES pipelines (pipeline_id) ON DELETE RESTRICT')

    # alert history: where-it-fired, SET NULL
    for table in ('watchlist_alerts', 'live_alert_triggers'):
        _require_zero(bind,
            f"SELECT count(*) FROM {table} t WHERE t.pipeline_id IS NOT NULL "
            f"AND NOT EXISTS (SELECT 1 FROM pipelines p WHERE p.pipeline_id = t.pipeline_id)",
            f"in {table} reference a pipeline_id that does not exist")
        _add_fk(table, 'pipeline_id', 'pipelines', 'pipeline_id', 'SET NULL', f'fk_{table}_pipeline')

    # ------------------------------------------------------ watchlist alerts
    _require_zero(bind,
        "SELECT count(*) FROM watchlist_alerts a WHERE a.search_id IS NOT NULL "
        "AND NOT EXISTS (SELECT 1 FROM search_history s WHERE s.id = a.search_id)",
        "in watchlist_alerts reference a search_history row that does not exist")
    _add_fk('watchlist_alerts', 'search_id', 'search_history', 'id', 'SET NULL',
            'fk_watchlist_alerts_search')
    # detection-triggered alerts are idempotent per (entry, detection)
    op.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS uq_watchlist_alert_entry_detection
        ON watchlist_alerts (watchlist_entry_id, detection_id)
        WHERE detection_id IS NOT NULL
    """)
    op.execute("CREATE INDEX IF NOT EXISTS idx_watchlist_alert_detection ON watchlist_alerts (detection_id)")

    # ------------------------------------------------- dead columns removed
    op.execute("ALTER TABLE pending_enrollments DROP COLUMN IF EXISTS checksum_match_identity_id")
    op.execute("ALTER TABLE detections DROP COLUMN IF EXISTS image_path")

    # ------------------------------------------ merge suggestion lifecycle
    op.execute("ALTER TYPE mergesuggestionstatus ADD VALUE IF NOT EXISTS 'INVALIDATED'")
    op.execute("ALTER TABLE merge_suggestions ADD COLUMN IF NOT EXISTS invalidated_reason VARCHAR(255)")
    op.execute("ALTER TABLE merge_suggestions ADD COLUMN IF NOT EXISTS invalidated_at TIMESTAMP")

    # ----------------------------------------------- unconstrained user refs
    _require_zero(bind,
        "SELECT count(*) FROM watchlists w WHERE w.deleted_by_user_id IS NOT NULL "
        "AND NOT EXISTS (SELECT 1 FROM users u WHERE u.id = w.deleted_by_user_id)",
        "in watchlists.deleted_by_user_id reference a user that does not exist")
    _add_fk('watchlists', 'deleted_by_user_id', 'users', 'id', 'SET NULL', 'fk_watchlists_deleted_by')
    _require_zero(bind,
        "SELECT count(*) FROM similarity_model_registry r WHERE r.activated_by IS NOT NULL "
        "AND NOT EXISTS (SELECT 1 FROM users u WHERE u.id = r.activated_by)",
        "in similarity_model_registry.activated_by reference a user that does not exist")
    _add_fk('similarity_model_registry', 'activated_by', 'users', 'id', 'SET NULL',
            'fk_similarity_model_registry_activated_by')

    # ------------------------------------------ ml_models.previous_production_id
    _require_zero(bind,
        "SELECT count(*) FROM ml_models m WHERE m.previous_production_id IS NOT NULL "
        "AND NOT EXISTS (SELECT 1 FROM ml_models p WHERE p.id = m.previous_production_id)",
        "in ml_models.previous_production_id reference a model that does not exist")
    _add_fk('ml_models', 'previous_production_id', 'ml_models', 'id', 'SET NULL',
            'fk_ml_models_previous_production')
    op.execute("ALTER TABLE ml_models DROP CONSTRAINT IF EXISTS ck_ml_models_prev_not_self")
    op.execute("ALTER TABLE ml_models ADD CONSTRAINT ck_ml_models_prev_not_self "
               "CHECK (previous_production_id IS NULL OR previous_production_id <> id)")

    # ------------------------------------------------------- chat sessions
    # (a) sessions are history containers: they outlive the account like the
    #     rows they group. Nullable first, then the rule.
    op.execute('ALTER TABLE user_conversation_sessions ALTER COLUMN user_id DROP NOT NULL')
    name = _fk_name(bind, 'user_conversation_sessions', 'user_id', 'users') \
        or 'user_conversation_sessions_user_id_fkey'
    op.execute(f'ALTER TABLE user_conversation_sessions DROP CONSTRAINT IF EXISTS {name}')
    op.execute(f'ALTER TABLE user_conversation_sessions ADD CONSTRAINT {name} '
               f'FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE SET NULL')
    # (b) backfill missing parents (non-destructive INSERT) from every child table
    op.execute("""
        INSERT INTO user_conversation_sessions
            (user_id, session_id, session_name, started_at, last_activity_at, is_active, query_count)
        SELECT s.user_id, s.session_id, NULL, s.first_at, s.last_at, false, s.n
        FROM (
            SELECT session_id,
                   (array_agg(user_id ORDER BY query_timestamp))[1] AS user_id,
                   MIN(query_timestamp) AS first_at, MAX(query_timestamp) AS last_at, COUNT(*) AS n
            FROM user_query_history WHERE session_id IS NOT NULL GROUP BY session_id
        ) s
        WHERE NOT EXISTS (SELECT 1 FROM user_conversation_sessions x WHERE x.session_id = s.session_id)
    """)
    op.execute("""
        INSERT INTO user_conversation_sessions
            (user_id, session_id, session_name, started_at, last_activity_at, is_active, query_count)
        SELECT m.user_id, m.source_session_id, NULL, MIN(m.created_at), MAX(m.created_at), false, 0
        FROM user_conversation_memory m
        WHERE m.source_session_id IS NOT NULL
          AND NOT EXISTS (SELECT 1 FROM user_conversation_sessions x WHERE x.session_id = m.source_session_id)
        GROUP BY m.user_id, m.source_session_id
    """)
    op.execute("""
        INSERT INTO user_conversation_sessions
            (user_id, session_id, session_name, started_at, last_activity_at, is_active, query_count)
        SELECT c.user_id, c.legacy_session_id, NULL, MIN(c.created_at), MAX(c.created_at), false, 0
        FROM conversations c
        WHERE c.legacy_session_id IS NOT NULL
          AND NOT EXISTS (SELECT 1 FROM user_conversation_sessions x WHERE x.session_id = c.legacy_session_id)
        GROUP BY c.user_id, c.legacy_session_id
    """)
    # (c) preconditions, (d) FKs
    for table, column in (('user_query_history', 'session_id'),
                          ('user_conversation_memory', 'source_session_id'),
                          ('conversations', 'legacy_session_id')):
        _require_zero(bind,
            f"SELECT count(*) FROM {table} t WHERE t.{column} IS NOT NULL AND NOT EXISTS "
            f"(SELECT 1 FROM user_conversation_sessions s WHERE s.session_id = t.{column})",
            f"in {table}.{column} reference a session that does not exist")
        _add_fk(table, column, 'user_conversation_sessions', 'session_id', 'SET NULL',
                f'fk_{table}_session')


def downgrade() -> None:
    """Undo the DDL. Refuses to restore NOT NULL where NULLs now legitimately
    exist (enrollment embeddings, sessions of deleted users) — a downgrade may
    undo a schema change, never destroy or falsify rows."""
    bind = op.get_bind()
    for table, column in (('identity_embeddings', 'pipeline_id'),
                          ('user_conversation_sessions', 'user_id')):
        nulls = bind.execute(sa.text(f'SELECT COUNT(*) FROM {table} WHERE {column} IS NULL')).scalar()
        if nulls:
            raise RuntimeError(f"cannot restore NOT NULL on {table}.{column}: {nulls} row(s) are "
                               f"legitimately NULL. Resolve them deliberately first.")

    for table, column, name in (('user_query_history', 'session_id', 'fk_user_query_history_session'),
                                ('user_conversation_memory', 'source_session_id', 'fk_user_conversation_memory_session'),
                                ('conversations', 'legacy_session_id', 'fk_conversations_session')):
        op.execute(f'ALTER TABLE {table} DROP CONSTRAINT IF EXISTS {name}')
    name = _fk_name(bind, 'user_conversation_sessions', 'user_id', 'users') \
        or 'user_conversation_sessions_user_id_fkey'
    op.execute(f'ALTER TABLE user_conversation_sessions DROP CONSTRAINT IF EXISTS {name}')
    op.execute(f'ALTER TABLE user_conversation_sessions ADD CONSTRAINT {name} '
               f'FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE CASCADE')
    op.execute('ALTER TABLE user_conversation_sessions ALTER COLUMN user_id SET NOT NULL')

    op.execute("ALTER TABLE ml_models DROP CONSTRAINT IF EXISTS ck_ml_models_prev_not_self")
    op.execute("ALTER TABLE ml_models DROP CONSTRAINT IF EXISTS fk_ml_models_previous_production")
    op.execute("ALTER TABLE similarity_model_registry DROP CONSTRAINT IF EXISTS fk_similarity_model_registry_activated_by")
    op.execute("ALTER TABLE watchlists DROP CONSTRAINT IF EXISTS fk_watchlists_deleted_by")

    op.execute("ALTER TABLE merge_suggestions DROP COLUMN IF EXISTS invalidated_at")
    op.execute("ALTER TABLE merge_suggestions DROP COLUMN IF EXISTS invalidated_reason")
    # PostgreSQL cannot drop an enum value; INVALIDATED stays as an unused label.

    op.execute("ALTER TABLE detections ADD COLUMN IF NOT EXISTS image_path VARCHAR(512)")
    op.execute("ALTER TABLE pending_enrollments ADD COLUMN IF NOT EXISTS checksum_match_identity_id UUID")

    op.execute("DROP INDEX IF EXISTS idx_watchlist_alert_detection")
    op.execute("DROP INDEX IF EXISTS uq_watchlist_alert_entry_detection")
    op.execute("ALTER TABLE watchlist_alerts DROP CONSTRAINT IF EXISTS fk_watchlist_alerts_search")
    for table in ('watchlist_alerts', 'live_alert_triggers'):
        op.execute(f"ALTER TABLE {table} DROP CONSTRAINT IF EXISTS fk_{table}_pipeline")

    name = _fk_name(bind, 'detections', 'pipeline_id', 'pipelines') or 'detections_pipeline_id_fkey'
    op.execute(f'ALTER TABLE detections DROP CONSTRAINT IF EXISTS {name}')
    op.execute(f'ALTER TABLE detections ADD CONSTRAINT {name} FOREIGN KEY (pipeline_id) '
               f'REFERENCES pipelines (pipeline_id) ON DELETE CASCADE')
    op.execute("ALTER TABLE identity_appearances DROP CONSTRAINT IF EXISTS fk_identity_appearances_pipeline")
    op.execute("ALTER TABLE identity_embeddings DROP CONSTRAINT IF EXISTS fk_identity_embeddings_pipeline")
    op.execute('ALTER TABLE identity_embeddings ALTER COLUMN pipeline_id SET NOT NULL')

"""Schema optimization: HNSW vector index, drop redundant indexes, consistent FK delete rules

Findings from the 2026-07 schema audit:
1. identity_embeddings.embedding (vector 512) had NO ANN index in the live DB —
   every similarity search was a sequential scan. ensure_indexes_exist() existed
   but was never called; tables were created via create_all which deliberately
   omits the HNSW index.
2. ~60 redundant indexes: SQLAlchemy index=True created ix_* duplicates of
   manual idx_* indexes, ix_<table>_id duplicated primary keys, and several
   single-column indexes were fully covered by composites.
3. FK delete rules were NO ACTION on the core chain while the ORM declared
   cascades — raw SQL deletes always errored. This migration makes the DB
   enforce the documented behavior:
     pipelines -> detections -> faces : ON DELETE CASCADE
     faces.identity_id               : ON DELETE SET NULL (history survives)
     identity_embeddings.detection_id: ON DELETE SET NULL (provenance only)
     identity -> embeddings/appearances : ON DELETE CASCADE
     alert tables' detection_id      : ON DELETE SET NULL

Revision ID: b7c8d9e0f1a2
Revises: a1b2c3d4e5f6
Create Date: 2026-07-24
"""
from alembic import op

revision = 'b7c8d9e0f1a2'
down_revision = 'a1b2c3d4e5f6'
branch_labels = None
depends_on = None


# Exact duplicates: same table, same columns, same uniqueness as a kept index
EXACT_DUPLICATE_INDEXES = [
    'ix_background_task_history_completed_at',   # = idx_task_history_completed
    'ix_background_task_history_scheduled_time', # = idx_task_history_scheduled
    'ix_chatbot_audit_log_created_at',           # = idx_audit_created
    'ix_detections_timestamp',                   # = idx_detection_timestamp
    'ix_faces_name',                             # = idx_face_name
    'ix_identities_last_seen_at',                # = idx_identity_last_seen
    'ix_identity_audit_log_created_at',          # = idx_identity_audit_created
    'ix_identity_embeddings_faiss_id',           # = idx_embedding_faiss_id
    'ix_identity_merges_merged_at',              # = idx_merge_merged_at
    'ix_identity_relationships_identity_id_1',   # = idx_relationship_identity1
    'ix_identity_relationships_identity_id_2',   # = idx_relationship_identity2
    'ix_live_search_alerts_created_by',          # = idx_live_alert_creator
    'ix_live_search_alerts_identity_id',         # = idx_live_alert_identity
    'ix_live_search_alerts_status',              # = idx_live_alert_status
    'ix_saved_searches_user_id',                 # = idx_saved_search_user
    'ix_search_history_created_at',              # = idx_search_history_date
    'ix_settings_category',                      # = idx_setting_category
    'ix_settings_audit_log_created_at',          # = idx_settings_audit_created
    'ix_similarity_training_data_created_at',    # = idx_similarity_training_created
    'ix_system_metrics_timestamp',               # = idx_metrics_timestamp
    'ix_user_conversation_memory_expires_at',    # = idx_memory_expires
    'ix_users_role',                             # = idx_user_role
    'ix_watchlist_entries_identity_id',          # = idx_watchlist_entry_identity
]

# btree(id) indexes that duplicate the table's PRIMARY KEY index
PK_DUPLICATE_INDEXES = [
    'ix_background_task_history_id', 'ix_chatbot_audit_log_id', 'ix_detections_id',
    'ix_faces_id', 'ix_identities_id', 'ix_identity_appearances_id',
    'ix_identity_audit_log_id', 'ix_identity_embeddings_id', 'ix_identity_merges_id',
    'ix_identity_relationships_id', 'ix_live_alert_triggers_id', 'ix_live_search_alerts_id',
    'ix_merge_suggestions_id', 'ix_pipelines_id', 'ix_saved_searches_id',
    'ix_search_history_id', 'ix_settings_audit_log_id', 'ix_settings_id',
    'ix_similarity_training_data_id', 'ix_system_metrics_id', 'ix_user_conversation_memory_id',
    'ix_user_conversation_sessions_id', 'ix_user_pipeline_access_id',
    'ix_user_query_embeddings_id', 'ix_user_query_history_id', 'ix_users_id',
    'ix_watchlist_alerts_id', 'ix_watchlist_entries_id', 'ix_watchlists_id',
]

# Single-column indexes whose column is the LEADING column of a kept composite
COVERED_BY_COMPOSITE_INDEXES = [
    'ix_detections_pipeline_id',            # ⊂ idx_detection_pipeline_timestamp
    'ix_faces_detection_id',                # ⊂ idx_face_detection
    'ix_identity_embeddings_identity_id',   # ⊂ idx_embedding_identity_created
    'ix_identity_appearances_identity_id',  # ⊂ idx_appearance_identity_start
    'ix_identity_appearances_pipeline_id',  # ⊂ idx_appearance_pipeline
]

# (table, constraint, column, referred_table, referred_column, ondelete)
FK_RULES = [
    ('detections', 'detections_pipeline_id_fkey', 'pipeline_id', 'pipelines', 'pipeline_id', 'CASCADE'),
    ('faces', 'faces_detection_id_fkey', 'detection_id', 'detections', 'id', 'CASCADE'),
    ('faces', 'faces_identity_id_fkey', 'identity_id', 'identities', 'id', 'SET NULL'),
    ('identity_embeddings', 'identity_embeddings_detection_id_fkey', 'detection_id', 'detections', 'id', 'SET NULL'),
    ('identity_embeddings', 'identity_embeddings_identity_id_fkey', 'identity_id', 'identities', 'id', 'CASCADE'),
    ('identity_appearances', 'identity_appearances_identity_id_fkey', 'identity_id', 'identities', 'id', 'CASCADE'),
    ('live_alert_triggers', 'live_alert_triggers_detection_id_fkey', 'detection_id', 'detections', 'id', 'SET NULL'),
    ('watchlist_alerts', 'watchlist_alerts_detection_id_fkey', 'detection_id', 'detections', 'id', 'SET NULL'),
]


def upgrade():
    # --- 1. ANN index for face-recognition similarity search -----------------
    # Params match docker-compose (PGVECTOR_HNSW_M=32, EF_CONSTRUCTION=128);
    # name matches identity_index_pgvector.ensure_indexes_exist so the startup
    # self-heal recognizes it.
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_embedding_vector_hnsw
        ON identity_embeddings
        USING hnsw (embedding vector_cosine_ops)
        WITH (m = 32, ef_construction = 128)
    """)

    # --- 2. Drop redundant indexes -------------------------------------------
    for index_name in EXACT_DUPLICATE_INDEXES + PK_DUPLICATE_INDEXES + COVERED_BY_COMPOSITE_INDEXES:
        op.execute(f'DROP INDEX IF EXISTS {index_name}')

    # --- 3. Consistent FK delete rules ---------------------------------------
    for table, constraint, column, ref_table, ref_column, rule in FK_RULES:
        op.execute(f'ALTER TABLE {table} DROP CONSTRAINT IF EXISTS {constraint}')
        op.execute(
            f'ALTER TABLE {table} ADD CONSTRAINT {constraint} '
            f'FOREIGN KEY ({column}) REFERENCES {ref_table} ({ref_column}) '
            f'ON DELETE {rule}'
        )

    # --- 4. pipeline_aliases.new_pipeline_id becomes a real FK ---------------
    # If the canonical pipeline is deleted, its redirect rows go with it.
    op.execute('ALTER TABLE pipeline_aliases DROP CONSTRAINT IF EXISTS pipeline_aliases_new_pipeline_id_fkey')
    op.execute("""
        ALTER TABLE pipeline_aliases ADD CONSTRAINT pipeline_aliases_new_pipeline_id_fkey
        FOREIGN KEY (new_pipeline_id) REFERENCES pipelines (pipeline_id) ON DELETE CASCADE
    """)


def downgrade():
    op.execute('ALTER TABLE pipeline_aliases DROP CONSTRAINT IF EXISTS pipeline_aliases_new_pipeline_id_fkey')

    # Restore plain NO ACTION FKs
    for table, constraint, column, ref_table, ref_column, _rule in FK_RULES:
        op.execute(f'ALTER TABLE {table} DROP CONSTRAINT IF EXISTS {constraint}')
        op.execute(
            f'ALTER TABLE {table} ADD CONSTRAINT {constraint} '
            f'FOREIGN KEY ({column}) REFERENCES {ref_table} ({ref_column})'
        )

    # The dropped duplicate indexes are intentionally NOT recreated on downgrade
    # (they duplicated surviving indexes; recreating them restores no behavior).

    op.execute('DROP INDEX IF EXISTS idx_embedding_vector_hnsw')

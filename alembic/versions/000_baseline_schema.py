"""Baseline schema — the 24 tables that predate Alembic in this repository.

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
    "CREATE TYPE identitytype AS ENUM ('UNKNOWN', 'KNOWN')",
    "CREATE TYPE identitystatus AS ENUM ('ACTIVE', 'MERGED', 'PROMOTED', 'INACTIVE')",
    "CREATE TYPE relationshipstrength AS ENUM ('WEAK', 'MODERATE', 'STRONG')",
    "CREATE TYPE livealertexpirationtype AS ENUM ('NEVER', 'DATE', 'DETECTIONS')",
    "CREATE TYPE livealertstatus AS ENUM ('ACTIVE', 'PAUSED', 'EXPIRED', 'TRIGGERED')",
    "CREATE TYPE mergesuggestionstatus AS ENUM ('PENDING', 'APPROVED', 'REJECTED', 'INVALIDATED')",
    "CREATE TYPE searchtype AS ENUM ('SINGLE', 'MULTI', 'BATCH')",
    "CREATE TYPE watchlistalertlevel AS ENUM ('INFO', 'WARNING', 'CRITICAL')",
    "CREATE TYPE labelstate AS ENUM ('AUTO_UNKNOWN', 'AUTO_KNOWN', 'MANUAL_LABELED')",
    "CREATE TYPE watchlistentrypriority AS ENUM ('LOW', 'NORMAL', 'HIGH', 'CRITICAL')",
    'CREATE TABLE IF NOT EXISTS background_task_history (\n\tid SERIAL NOT NULL, \n\ttask_type VARCHAR(50) NOT NULL, \n\ttask_name VARCHAR(200) NOT NULL, \n\tstatus VARCHAR(20) NOT NULL, \n\tdescription TEXT, \n\tscheduled_time TIMESTAMP WITHOUT TIME ZONE, \n\tstarted_at TIMESTAMP WITHOUT TIME ZONE, \n\tcompleted_at TIMESTAMP WITHOUT TIME ZONE, \n\tduration_seconds FLOAT, \n\tsuccess BOOLEAN, \n\tdetails JSONB, \n\tnotify_all_users BOOLEAN, \n\tcreated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, \n\tPRIMARY KEY (id)\n)',
    'CREATE INDEX IF NOT EXISTS idx_task_history_completed ON background_task_history (completed_at)',
    'CREATE INDEX IF NOT EXISTS idx_task_history_scheduled ON background_task_history (scheduled_time)',
    'CREATE INDEX IF NOT EXISTS idx_task_history_type_status ON background_task_history (task_type, status)',
    'CREATE INDEX IF NOT EXISTS ix_background_task_history_completed_at ON background_task_history (completed_at)',
    'CREATE INDEX IF NOT EXISTS ix_background_task_history_created_at ON background_task_history (created_at)',
    'CREATE INDEX IF NOT EXISTS ix_background_task_history_id ON background_task_history (id)',
    'CREATE INDEX IF NOT EXISTS ix_background_task_history_scheduled_time ON background_task_history (scheduled_time)',
    'CREATE INDEX IF NOT EXISTS ix_background_task_history_status ON background_task_history (status)',
    'CREATE INDEX IF NOT EXISTS ix_background_task_history_task_type ON background_task_history (task_type)',
    'CREATE TABLE IF NOT EXISTS identities (\n\tid UUID NOT NULL, \n\ttype identitytype NOT NULL, \n\tdisplay_name VARCHAR(255), \n\tstatus identitystatus NOT NULL, \n\tperson_code VARCHAR(100), \n\tperson_code_key VARCHAR(100), \n\tfirst_seen_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, \n\tlast_seen_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, \n\tcreated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, \n\tupdated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, \n\tbest_snapshot_path VARCHAR(512), \n\tappearances_count INTEGER NOT NULL, \n\tmerged_into_id UUID, \n\tPRIMARY KEY (id), \n\tFOREIGN KEY(merged_into_id) REFERENCES identities (id)\n)',
    'CREATE INDEX IF NOT EXISTS idx_identity_last_seen ON identities (last_seen_at)',
    'CREATE INDEX IF NOT EXISTS idx_identity_type_status ON identities (type, status)',
    'CREATE INDEX IF NOT EXISTS idx_identity_type_status_last_seen ON identities (type, status, last_seen_at)',
    'CREATE INDEX IF NOT EXISTS ix_identities_display_name ON identities (display_name)',
    'CREATE INDEX IF NOT EXISTS ix_identities_first_seen_at ON identities (first_seen_at)',
    'CREATE INDEX IF NOT EXISTS ix_identities_merged_into_id ON identities (merged_into_id)',
    'CREATE INDEX IF NOT EXISTS ix_identities_status ON identities (status)',
    'CREATE INDEX IF NOT EXISTS ix_identities_type ON identities (type)',
    'CREATE UNIQUE INDEX IF NOT EXISTS uq_identity_person_code_key ON identities (person_code_key) WHERE person_code_key IS NOT NULL',
    'CREATE TABLE IF NOT EXISTS pipelines (\n\tid SERIAL NOT NULL, \n\tpipeline_id VARCHAR(255) NOT NULL, \n\tcreated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, \n\tupdated_at TIMESTAMP WITHOUT TIME ZONE, \n\ttotal_detections INTEGER, \n\tis_active INTEGER, \n\tPRIMARY KEY (id)\n)',
    'CREATE INDEX IF NOT EXISTS idx_pipeline_id_active ON pipelines (pipeline_id, is_active)',
    'CREATE UNIQUE INDEX IF NOT EXISTS ix_pipelines_pipeline_id ON pipelines (pipeline_id)',
    'CREATE TABLE IF NOT EXISTS settings (\n\tid SERIAL NOT NULL, \n\tkey VARCHAR(255) NOT NULL, \n\tvalue TEXT, \n\tvalue_type VARCHAR(50) NOT NULL, \n\tcategory VARCHAR(100) NOT NULL, \n\tdescription TEXT, \n\tis_sensitive BOOLEAN NOT NULL, \n\tis_readonly BOOLEAN NOT NULL, \n\tcreated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, \n\tupdated_at TIMESTAMP WITHOUT TIME ZONE, \n\tPRIMARY KEY (id)\n)',
    'CREATE INDEX IF NOT EXISTS idx_setting_category ON settings (category)',
    'CREATE INDEX IF NOT EXISTS idx_setting_key ON settings (key)',
    'CREATE INDEX IF NOT EXISTS ix_settings_category ON settings (category)',
    'CREATE INDEX IF NOT EXISTS ix_settings_id ON settings (id)',
    'CREATE UNIQUE INDEX IF NOT EXISTS ix_settings_key ON settings (key)',
    'CREATE TABLE IF NOT EXISTS system_metrics (\n\tid SERIAL NOT NULL, \n\ttimestamp TIMESTAMP WITHOUT TIME ZONE NOT NULL, \n\tqueue_size INTEGER, \n\tprocessing_count INTEGER, \n\ttotal_received INTEGER, \n\ttotal_processed INTEGER, \n\ttotal_skipped INTEGER, \n\tavg_processing_time_ms FLOAT, \n\tactive_pipelines INTEGER, \n\ttotal_faces_detected INTEGER, \n\tcpu_percent FLOAT, \n\tmemory_percent FLOAT, \n\tdisk_usage_gb FLOAT, \n\tPRIMARY KEY (id)\n)',
    'CREATE INDEX IF NOT EXISTS idx_metrics_timestamp ON system_metrics (timestamp)',
    'CREATE INDEX IF NOT EXISTS ix_system_metrics_id ON system_metrics (id)',
    'CREATE INDEX IF NOT EXISTS ix_system_metrics_timestamp ON system_metrics (timestamp)',
    'CREATE TABLE IF NOT EXISTS users (\n\tid SERIAL NOT NULL, \n\tusername VARCHAR(100) NOT NULL, \n\temail VARCHAR(255) NOT NULL, \n\tpassword_hash VARCHAR(255) NOT NULL, \n\tfull_name VARCHAR(255), \n\trole VARCHAR(50) NOT NULL, \n\tis_active BOOLEAN NOT NULL, \n\tcan_use_chatbot BOOLEAN NOT NULL, \n\tblocked_reason TEXT, \n\tblocked_at TIMESTAMP WITHOUT TIME ZONE, \n\tcreated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, \n\tupdated_at TIMESTAMP WITHOUT TIME ZONE, \n\tlast_login TIMESTAMP WITHOUT TIME ZONE, \n\tmust_change_password BOOLEAN DEFAULT false NOT NULL, \n\tpassword_changed_at TIMESTAMP WITHOUT TIME ZONE, \n\tpermissions_version INTEGER DEFAULT 1 NOT NULL, \n\tPRIMARY KEY (id)\n)',
    'CREATE INDEX IF NOT EXISTS idx_user_role ON users (role)',
    'CREATE INDEX IF NOT EXISTS idx_user_username_active ON users (username, is_active)',
    'CREATE UNIQUE INDEX IF NOT EXISTS ix_users_email ON users (email)',
    'CREATE INDEX IF NOT EXISTS ix_users_id ON users (id)',
    'CREATE INDEX IF NOT EXISTS ix_users_role ON users (role)',
    'CREATE UNIQUE INDEX IF NOT EXISTS ix_users_username ON users (username)',
    'CREATE TABLE IF NOT EXISTS chatbot_audit_log (\n\tid SERIAL NOT NULL, \n\tuser_id INTEGER, \n\thistorical_user_id INTEGER, \n\tusername VARCHAR(100) NOT NULL, \n\tquery TEXT NOT NULL, \n\tresponse TEXT, \n\tsuccess BOOLEAN NOT NULL, \n\terror_message TEXT, \n\tprocessing_time_ms FLOAT, \n\tsession_id VARCHAR(255), \n\tcreated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, \n\tPRIMARY KEY (id), \n\tFOREIGN KEY(user_id) REFERENCES users (id) ON DELETE SET NULL\n)',
    'CREATE INDEX IF NOT EXISTS idx_audit_created ON chatbot_audit_log (created_at)',
    'CREATE INDEX IF NOT EXISTS idx_audit_user_created ON chatbot_audit_log (user_id, created_at)',
    'CREATE INDEX IF NOT EXISTS ix_chatbot_audit_log_created_at ON chatbot_audit_log (created_at)',
    'CREATE INDEX IF NOT EXISTS ix_chatbot_audit_log_id ON chatbot_audit_log (id)',
    'CREATE INDEX IF NOT EXISTS ix_chatbot_audit_log_session_id ON chatbot_audit_log (session_id)',
    'CREATE INDEX IF NOT EXISTS ix_chatbot_audit_log_user_id ON chatbot_audit_log (user_id)',
    'CREATE INDEX IF NOT EXISTS ix_chatbot_audit_log_username ON chatbot_audit_log (username)',
    'CREATE TABLE IF NOT EXISTS detections (\n\tid SERIAL NOT NULL, \n\tuuid VARCHAR(36), \n\tpipeline_id VARCHAR(255) NOT NULL, \n\ttimestamp TIMESTAMP WITHOUT TIME ZONE NOT NULL, \n\timage_size_bytes INTEGER, \n\tprocessing_time_ms FLOAT, \n\tworker_id INTEGER, \n\tPRIMARY KEY (id), \n\tFOREIGN KEY(pipeline_id) REFERENCES pipelines (pipeline_id) ON DELETE RESTRICT\n)',
    'CREATE INDEX IF NOT EXISTS idx_detection_pipeline_timestamp ON detections (pipeline_id, timestamp)',
    'CREATE INDEX IF NOT EXISTS idx_detection_timestamp ON detections (timestamp)',
    'CREATE UNIQUE INDEX IF NOT EXISTS ix_detections_uuid ON detections (uuid)',
    'CREATE TABLE IF NOT EXISTS identity_appearances (\n\tid SERIAL NOT NULL, \n\tidentity_id UUID NOT NULL, \n\tpipeline_id VARCHAR(255) NOT NULL, \n\ttrack_id VARCHAR(255), \n\tstart_time TIMESTAMP WITHOUT TIME ZONE NOT NULL, \n\tend_time TIMESTAMP WITHOUT TIME ZONE, \n\tbest_snapshot_path VARCHAR(512), \n\tcreated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, \n\tPRIMARY KEY (id), \n\tFOREIGN KEY(identity_id) REFERENCES identities (id) ON DELETE CASCADE, \n\tFOREIGN KEY(pipeline_id) REFERENCES pipelines (pipeline_id) ON DELETE RESTRICT\n)',
    'CREATE INDEX IF NOT EXISTS idx_appearance_identity_pipeline ON identity_appearances (identity_id, pipeline_id)',
    'CREATE INDEX IF NOT EXISTS idx_appearance_identity_start ON identity_appearances (identity_id, start_time)',
    'CREATE INDEX IF NOT EXISTS idx_appearance_pipeline ON identity_appearances (pipeline_id, start_time)',
    'CREATE INDEX IF NOT EXISTS ix_identity_appearances_start_time ON identity_appearances (start_time)',
    'CREATE TABLE IF NOT EXISTS identity_audit_log (\n\tid SERIAL NOT NULL, \n\tuser_id INTEGER, \n\thistorical_user_id INTEGER, \n\tusername VARCHAR(100) NOT NULL, \n\taction_type VARCHAR(50) NOT NULL, \n\tidentity_id UUID, \n\trelated_identity_id UUID, \n\taction_details JSONB, \n\tbefore_state JSONB, \n\tafter_state JSONB, \n\tip_address VARCHAR(45), \n\tuser_agent VARCHAR(500), \n\tsuccess BOOLEAN NOT NULL, \n\terror_message TEXT, \n\tnotes TEXT, \n\tcreated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, \n\tPRIMARY KEY (id), \n\tFOREIGN KEY(user_id) REFERENCES users (id) ON DELETE SET NULL, \n\tFOREIGN KEY(identity_id) REFERENCES identities (id), \n\tFOREIGN KEY(related_identity_id) REFERENCES identities (id)\n)',
    'CREATE INDEX IF NOT EXISTS idx_identity_audit_action ON identity_audit_log (action_type, created_at)',
    'CREATE INDEX IF NOT EXISTS idx_identity_audit_created ON identity_audit_log (created_at)',
    'CREATE INDEX IF NOT EXISTS idx_identity_audit_identity ON identity_audit_log (identity_id, created_at)',
    'CREATE INDEX IF NOT EXISTS idx_identity_audit_user_action ON identity_audit_log (user_id, action_type, created_at)',
    'CREATE INDEX IF NOT EXISTS ix_identity_audit_log_action_type ON identity_audit_log (action_type)',
    'CREATE INDEX IF NOT EXISTS ix_identity_audit_log_created_at ON identity_audit_log (created_at)',
    'CREATE INDEX IF NOT EXISTS ix_identity_audit_log_id ON identity_audit_log (id)',
    'CREATE INDEX IF NOT EXISTS ix_identity_audit_log_identity_id ON identity_audit_log (identity_id)',
    'CREATE INDEX IF NOT EXISTS ix_identity_audit_log_success ON identity_audit_log (success)',
    'CREATE INDEX IF NOT EXISTS ix_identity_audit_log_user_id ON identity_audit_log (user_id)',
    'CREATE INDEX IF NOT EXISTS ix_identity_audit_log_username ON identity_audit_log (username)',
    'CREATE TABLE IF NOT EXISTS identity_merges (\n\tid SERIAL NOT NULL, \n\tfrom_identity_id UUID NOT NULL, \n\tto_identity_id UUID NOT NULL, \n\tmerged_by INTEGER, \n\thistorical_merged_by INTEGER, \n\tmerged_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, \n\tnotes TEXT, \n\tprovenance JSONB, \n\tPRIMARY KEY (id), \n\tFOREIGN KEY(from_identity_id) REFERENCES identities (id), \n\tFOREIGN KEY(to_identity_id) REFERENCES identities (id), \n\tFOREIGN KEY(merged_by) REFERENCES users (id) ON DELETE SET NULL\n)',
    'CREATE INDEX IF NOT EXISTS idx_merge_from_to ON identity_merges (from_identity_id, to_identity_id)',
    'CREATE INDEX IF NOT EXISTS idx_merge_merged_at ON identity_merges (merged_at)',
    'CREATE INDEX IF NOT EXISTS ix_identity_merges_from_identity_id ON identity_merges (from_identity_id)',
    'CREATE INDEX IF NOT EXISTS ix_identity_merges_id ON identity_merges (id)',
    'CREATE INDEX IF NOT EXISTS ix_identity_merges_merged_at ON identity_merges (merged_at)',
    'CREATE INDEX IF NOT EXISTS ix_identity_merges_merged_by ON identity_merges (merged_by)',
    'CREATE INDEX IF NOT EXISTS ix_identity_merges_to_identity_id ON identity_merges (to_identity_id)',
    'CREATE TABLE IF NOT EXISTS identity_relationships (\n\tid UUID NOT NULL, \n\tidentity_id_1 UUID NOT NULL, \n\tidentity_id_2 UUID NOT NULL, \n\tco_appearance_count INTEGER NOT NULL, \n\tco_appearance_percentage FLOAT, \n\trelationship_strength relationshipstrength, \n\tcommon_pipelines JSONB, \n\tcommon_time_patterns JSONB, \n\tfirst_co_appearance TIMESTAMP WITHOUT TIME ZONE, \n\tlast_co_appearance TIMESTAMP WITHOUT TIME ZONE, \n\tcalculated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, \n\tPRIMARY KEY (id), \n\tFOREIGN KEY(identity_id_1) REFERENCES identities (id) ON DELETE CASCADE, \n\tFOREIGN KEY(identity_id_2) REFERENCES identities (id) ON DELETE CASCADE\n)',
    'CREATE INDEX IF NOT EXISTS idx_relationship_identity1 ON identity_relationships (identity_id_1)',
    'CREATE INDEX IF NOT EXISTS idx_relationship_identity2 ON identity_relationships (identity_id_2)',
    'CREATE UNIQUE INDEX IF NOT EXISTS idx_relationship_pair ON identity_relationships (identity_id_1, identity_id_2)',
    'CREATE INDEX IF NOT EXISTS ix_identity_relationships_id ON identity_relationships (id)',
    'CREATE INDEX IF NOT EXISTS ix_identity_relationships_identity_id_1 ON identity_relationships (identity_id_1)',
    'CREATE INDEX IF NOT EXISTS ix_identity_relationships_identity_id_2 ON identity_relationships (identity_id_2)',
    'CREATE TABLE IF NOT EXISTS live_search_alerts (\n\tid UUID NOT NULL, \n\tname VARCHAR(200) NOT NULL, \n\tidentity_id UUID NOT NULL, \n\tcreated_by INTEGER, \n\thistorical_created_by INTEGER, \n\tmin_similarity FLOAT NOT NULL, \n\tpipeline_ids JSONB, \n\ttime_window_enabled BOOLEAN NOT NULL, \n\ttime_window_start VARCHAR(5), \n\ttime_window_end VARCHAR(5), \n\tactive_days JSONB, \n\tcooldown_minutes INTEGER NOT NULL, \n\tnotify_dashboard BOOLEAN NOT NULL, \n\tnotify_email BOOLEAN NOT NULL, \n\tnotify_sms BOOLEAN NOT NULL, \n\tnotify_webhook BOOLEAN NOT NULL, \n\temail_recipients JSONB, \n\tsms_recipients JSONB, \n\twebhook_url TEXT, \n\tsound_alert BOOLEAN NOT NULL, \n\tauto_capture_snapshot BOOLEAN NOT NULL, \n\tauto_record_clip BOOLEAN NOT NULL, \n\tclip_duration_seconds INTEGER NOT NULL, \n\texpiration_type livealertexpirationtype NOT NULL, \n\texpiration_date TIMESTAMP WITHOUT TIME ZONE, \n\texpiration_detections INTEGER, \n\tstatus livealertstatus NOT NULL, \n\ttriggers_count INTEGER NOT NULL, \n\tlast_triggered_at TIMESTAMP WITHOUT TIME ZONE, \n\tcreated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, \n\tupdated_at TIMESTAMP WITHOUT TIME ZONE, \n\tPRIMARY KEY (id), \n\tFOREIGN KEY(identity_id) REFERENCES identities (id) ON DELETE CASCADE, \n\tFOREIGN KEY(created_by) REFERENCES users (id) ON DELETE SET NULL\n)',
    'CREATE INDEX IF NOT EXISTS idx_live_alert_creator ON live_search_alerts (created_by)',
    'CREATE INDEX IF NOT EXISTS idx_live_alert_identity ON live_search_alerts (identity_id)',
    'CREATE INDEX IF NOT EXISTS idx_live_alert_status ON live_search_alerts (status)',
    'CREATE INDEX IF NOT EXISTS ix_live_search_alerts_created_by ON live_search_alerts (created_by)',
    'CREATE INDEX IF NOT EXISTS ix_live_search_alerts_id ON live_search_alerts (id)',
    'CREATE INDEX IF NOT EXISTS ix_live_search_alerts_identity_id ON live_search_alerts (identity_id)',
    'CREATE INDEX IF NOT EXISTS ix_live_search_alerts_status ON live_search_alerts (status)',
    'CREATE TABLE IF NOT EXISTS merge_suggestions (\n\tid SERIAL NOT NULL, \n\tcluster_id VARCHAR(255), \n\tidentity_ids JSONB NOT NULL, \n\tconfidence FLOAT NOT NULL, \n\tstatus mergesuggestionstatus NOT NULL, \n\trepresentative_snapshots JSONB, \n\tcreated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, \n\treviewed_at TIMESTAMP WITHOUT TIME ZONE, \n\treviewed_by INTEGER, \n\tinvalidated_reason VARCHAR(255), \n\tinvalidated_at TIMESTAMP WITHOUT TIME ZONE, \n\tPRIMARY KEY (id), \n\tFOREIGN KEY(reviewed_by) REFERENCES users (id) ON DELETE SET NULL\n)',
    'CREATE INDEX IF NOT EXISTS idx_merge_suggestion_status ON merge_suggestions (status, created_at)',
    'CREATE INDEX IF NOT EXISTS ix_merge_suggestions_cluster_id ON merge_suggestions (cluster_id)',
    'CREATE INDEX IF NOT EXISTS ix_merge_suggestions_created_at ON merge_suggestions (created_at)',
    'CREATE INDEX IF NOT EXISTS ix_merge_suggestions_id ON merge_suggestions (id)',
    'CREATE INDEX IF NOT EXISTS ix_merge_suggestions_status ON merge_suggestions (status)',
    'CREATE TABLE IF NOT EXISTS search_history (\n\tid UUID NOT NULL, \n\tuser_id INTEGER, \n\thistorical_user_id INTEGER, \n\tsearch_type searchtype NOT NULL, \n\tscope VARCHAR(20), \n\ttop_k INTEGER, \n\tfilters JSONB, \n\texclude_identity_ids JSONB, \n\texclude_watchlist_ids JSONB, \n\tinput_image_hash VARCHAR(64), \n\tinput_faces_count INTEGER, \n\tinput_quality_scores JSONB, \n\tresults_count INTEGER, \n\tresults_summary JSONB, \n\twatchlist_alerts_count INTEGER NOT NULL, \n\tunique_identities_count INTEGER, \n\tprocessing_time_ms INTEGER, \n\tip_address VARCHAR(45), \n\tuser_agent TEXT, \n\tcreated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, \n\tPRIMARY KEY (id), \n\tFOREIGN KEY(user_id) REFERENCES users (id) ON DELETE SET NULL\n)',
    'CREATE INDEX IF NOT EXISTS idx_search_history_date ON search_history (created_at)',
    'CREATE INDEX IF NOT EXISTS idx_search_history_user ON search_history (user_id, created_at)',
    'CREATE INDEX IF NOT EXISTS ix_search_history_created_at ON search_history (created_at)',
    'CREATE INDEX IF NOT EXISTS ix_search_history_id ON search_history (id)',
    'CREATE INDEX IF NOT EXISTS ix_search_history_user_id ON search_history (user_id)',
    'CREATE TABLE IF NOT EXISTS settings_audit_log (\n\tid SERIAL NOT NULL, \n\tsetting_key VARCHAR(255) NOT NULL, \n\told_value TEXT, \n\tnew_value TEXT, \n\tvalue_type VARCHAR(50) NOT NULL, \n\tchanged_by_user_id INTEGER, \n\tchanged_by_username VARCHAR(100), \n\tchange_reason TEXT, \n\taction VARCHAR(50), \n\tip_address VARCHAR(45), \n\tuser_agent TEXT, \n\tcreated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, \n\tPRIMARY KEY (id), \n\tFOREIGN KEY(changed_by_user_id) REFERENCES users (id) ON DELETE SET NULL\n)',
    'CREATE INDEX IF NOT EXISTS idx_settings_audit_created ON settings_audit_log (created_at)',
    'CREATE INDEX IF NOT EXISTS idx_settings_audit_setting ON settings_audit_log (setting_key, created_at)',
    'CREATE INDEX IF NOT EXISTS idx_settings_audit_user ON settings_audit_log (changed_by_user_id, created_at)',
    'CREATE INDEX IF NOT EXISTS ix_settings_audit_log_changed_by_user_id ON settings_audit_log (changed_by_user_id)',
    'CREATE INDEX IF NOT EXISTS ix_settings_audit_log_created_at ON settings_audit_log (created_at)',
    'CREATE INDEX IF NOT EXISTS ix_settings_audit_log_id ON settings_audit_log (id)',
    'CREATE INDEX IF NOT EXISTS ix_settings_audit_log_setting_key ON settings_audit_log (setting_key)',
    'CREATE TABLE IF NOT EXISTS similarity_training_data (\n\tid SERIAL NOT NULL, \n\tidentity_id_1 UUID, \n\tidentity_id_2 UUID, \n\tembedding_similarity FLOAT NOT NULL, \n\tpipeline_overlap FLOAT NOT NULL, \n\tquality_score_1 FLOAT NOT NULL, \n\tquality_score_2 FLOAT NOT NULL, \n\tappearances_diff FLOAT NOT NULL, \n\tis_cross_pipeline BOOLEAN NOT NULL, \n\tlabel FLOAT NOT NULL, \n\tcreated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, \n\tcreated_by_user_id INTEGER, \n\tPRIMARY KEY (id), \n\tFOREIGN KEY(identity_id_1) REFERENCES identities (id), \n\tFOREIGN KEY(identity_id_2) REFERENCES identities (id), \n\tFOREIGN KEY(created_by_user_id) REFERENCES users (id) ON DELETE SET NULL\n)',
    'CREATE INDEX IF NOT EXISTS idx_similarity_training_created ON similarity_training_data (created_at)',
    'CREATE INDEX IF NOT EXISTS idx_similarity_training_label ON similarity_training_data (label, created_at)',
    'CREATE INDEX IF NOT EXISTS idx_similarity_training_user ON similarity_training_data (created_by_user_id, created_at)',
    'CREATE INDEX IF NOT EXISTS ix_similarity_training_data_created_at ON similarity_training_data (created_at)',
    'CREATE INDEX IF NOT EXISTS ix_similarity_training_data_created_by_user_id ON similarity_training_data (created_by_user_id)',
    'CREATE INDEX IF NOT EXISTS ix_similarity_training_data_id ON similarity_training_data (id)',
    'CREATE INDEX IF NOT EXISTS ix_similarity_training_data_identity_id_1 ON similarity_training_data (identity_id_1)',
    'CREATE INDEX IF NOT EXISTS ix_similarity_training_data_identity_id_2 ON similarity_training_data (identity_id_2)',
    'CREATE TABLE IF NOT EXISTS user_pipeline_access (\n\tid SERIAL NOT NULL, \n\tuser_id INTEGER NOT NULL, \n\tpipeline_id VARCHAR(255) NOT NULL, \n\tgranted_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, \n\tPRIMARY KEY (id), \n\tFOREIGN KEY(user_id) REFERENCES users (id) ON DELETE CASCADE, \n\tFOREIGN KEY(pipeline_id) REFERENCES pipelines (pipeline_id)\n)',
    'CREATE UNIQUE INDEX IF NOT EXISTS idx_user_pipeline ON user_pipeline_access (user_id, pipeline_id)',
    'CREATE INDEX IF NOT EXISTS ix_user_pipeline_access_id ON user_pipeline_access (id)',
    'CREATE INDEX IF NOT EXISTS ix_user_pipeline_access_pipeline_id ON user_pipeline_access (pipeline_id)',
    'CREATE INDEX IF NOT EXISTS ix_user_pipeline_access_user_id ON user_pipeline_access (user_id)',
    'CREATE TABLE IF NOT EXISTS watchlists (\n\tid UUID NOT NULL, \n\tname VARCHAR(100) NOT NULL, \n\tdescription TEXT, \n\tcolor VARCHAR(7), \n\ticon VARCHAR(50), \n\talert_level watchlistalertlevel NOT NULL, \n\tnotify_dashboard BOOLEAN NOT NULL, \n\tnotify_email BOOLEAN NOT NULL, \n\tnotify_sms BOOLEAN NOT NULL, \n\tnotify_webhook BOOLEAN NOT NULL, \n\temail_recipients JSONB, \n\tsms_recipients JSONB, \n\twebhook_url TEXT, \n\tis_active BOOLEAN NOT NULL, \n\tcreated_by INTEGER, \n\tcreated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, \n\tupdated_at TIMESTAMP WITHOUT TIME ZONE, \n\tPRIMARY KEY (id), \n\tFOREIGN KEY(created_by) REFERENCES users (id) ON DELETE SET NULL\n)',
    'CREATE INDEX IF NOT EXISTS idx_watchlist_name_active ON watchlists (name, is_active)',
    'CREATE INDEX IF NOT EXISTS ix_watchlists_id ON watchlists (id)',
    'CREATE INDEX IF NOT EXISTS ix_watchlists_is_active ON watchlists (is_active)',
    'CREATE INDEX IF NOT EXISTS ix_watchlists_name ON watchlists (name)',
    'CREATE TABLE IF NOT EXISTS faces (\n\tid SERIAL NOT NULL, \n\tdetection_id INTEGER NOT NULL, \n\tname VARCHAR(255) NOT NULL, \n\tsimilarity FLOAT NOT NULL, \n\tidentity_id UUID, \n\tlabel_state labelstate, \n\tface_image_path VARCHAR(512), \n\tbbox_x1 FLOAT, \n\tbbox_y1 FLOAT, \n\tbbox_x2 FLOAT, \n\tbbox_y2 FLOAT, \n\tPRIMARY KEY (id), \n\tFOREIGN KEY(detection_id) REFERENCES detections (id) ON DELETE CASCADE, \n\tFOREIGN KEY(identity_id) REFERENCES identities (id) ON DELETE SET NULL\n)',
    'CREATE INDEX IF NOT EXISTS idx_face_detection ON faces (detection_id, name)',
    'CREATE INDEX IF NOT EXISTS idx_face_name ON faces (name)',
    'CREATE INDEX IF NOT EXISTS ix_faces_identity_id ON faces (identity_id)',
    'CREATE INDEX IF NOT EXISTS ix_faces_label_state ON faces (label_state)',
    'CREATE TABLE IF NOT EXISTS identity_embeddings (\n\tid SERIAL NOT NULL, \n\tidentity_id UUID NOT NULL, \n\tdetection_id INTEGER, \n\tpipeline_id VARCHAR(255), \n\tfaiss_index_type VARCHAR(50), \n\tquality FLOAT, \n\tquality_scorer_version VARCHAR(32), \n\tcreated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, \n\tPRIMARY KEY (id), \n\tFOREIGN KEY(identity_id) REFERENCES identities (id) ON DELETE CASCADE, \n\tFOREIGN KEY(detection_id) REFERENCES detections (id) ON DELETE SET NULL, \n\tFOREIGN KEY(pipeline_id) REFERENCES pipelines (pipeline_id) ON DELETE RESTRICT\n)',
    'CREATE INDEX IF NOT EXISTS idx_embedding_identity_created ON identity_embeddings (identity_id, created_at)',
    'CREATE INDEX IF NOT EXISTS ix_identity_embeddings_created_at ON identity_embeddings (created_at)',
    'CREATE INDEX IF NOT EXISTS ix_identity_embeddings_detection_id ON identity_embeddings (detection_id)',
    'CREATE INDEX IF NOT EXISTS ix_identity_embeddings_pipeline_id ON identity_embeddings (pipeline_id)',
    'CREATE TABLE IF NOT EXISTS live_alert_triggers (\n\tid UUID NOT NULL, \n\talert_id UUID NOT NULL, \n\tdetection_id INTEGER, \n\tpipeline_id VARCHAR(255), \n\tsimilarity_score FLOAT, \n\tsnapshot_path VARCHAR(512), \n\tclip_path VARCHAR(512), \n\tacknowledged BOOLEAN NOT NULL, \n\tacknowledged_by INTEGER, \n\tacknowledged_at TIMESTAMP WITHOUT TIME ZONE, \n\tcreated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, \n\tPRIMARY KEY (id), \n\tFOREIGN KEY(alert_id) REFERENCES live_search_alerts (id) ON DELETE CASCADE, \n\tFOREIGN KEY(detection_id) REFERENCES detections (id) ON DELETE SET NULL, \n\tFOREIGN KEY(pipeline_id) REFERENCES pipelines (pipeline_id) ON DELETE SET NULL, \n\tFOREIGN KEY(acknowledged_by) REFERENCES users (id) ON DELETE SET NULL\n)',
    'CREATE INDEX IF NOT EXISTS idx_alert_trigger_alert ON live_alert_triggers (alert_id, created_at)',
    'CREATE INDEX IF NOT EXISTS idx_alert_trigger_alert_ack ON live_alert_triggers (alert_id, acknowledged, created_at)',
    'CREATE INDEX IF NOT EXISTS ix_live_alert_triggers_alert_id ON live_alert_triggers (alert_id)',
    'CREATE INDEX IF NOT EXISTS ix_live_alert_triggers_created_at ON live_alert_triggers (created_at)',
    'CREATE INDEX IF NOT EXISTS ix_live_alert_triggers_id ON live_alert_triggers (id)',
    'CREATE TABLE IF NOT EXISTS watchlist_entries (\n\tid UUID NOT NULL, \n\twatchlist_id UUID NOT NULL, \n\tidentity_id UUID NOT NULL, \n\tpriority watchlistentrypriority NOT NULL, \n\tnotes TEXT, \n\taction_instructions TEXT, \n\tadded_by INTEGER, \n\tadded_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, \n\texpires_at TIMESTAMP WITHOUT TIME ZONE, \n\tis_active BOOLEAN NOT NULL, \n\tPRIMARY KEY (id), \n\tFOREIGN KEY(watchlist_id) REFERENCES watchlists (id) ON DELETE CASCADE, \n\tFOREIGN KEY(identity_id) REFERENCES identities (id) ON DELETE CASCADE, \n\tFOREIGN KEY(added_by) REFERENCES users (id) ON DELETE SET NULL\n)',
    'CREATE INDEX IF NOT EXISTS idx_watchlist_entry_active ON watchlist_entries (is_active, expires_at)',
    'CREATE INDEX IF NOT EXISTS idx_watchlist_entry_identity ON watchlist_entries (identity_id)',
    'CREATE UNIQUE INDEX IF NOT EXISTS idx_watchlist_entry_unique ON watchlist_entries (watchlist_id, identity_id)',
    'CREATE INDEX IF NOT EXISTS ix_watchlist_entries_id ON watchlist_entries (id)',
    'CREATE INDEX IF NOT EXISTS ix_watchlist_entries_identity_id ON watchlist_entries (identity_id)',
    'CREATE INDEX IF NOT EXISTS ix_watchlist_entries_is_active ON watchlist_entries (is_active)',
    'CREATE INDEX IF NOT EXISTS ix_watchlist_entries_watchlist_id ON watchlist_entries (watchlist_id)',
    'CREATE TABLE IF NOT EXISTS watchlist_alerts (\n\tid UUID NOT NULL, \n\twatchlist_entry_id UUID NOT NULL, \n\ttriggered_by VARCHAR(50) NOT NULL, \n\tsearch_id UUID, \n\tdetection_id INTEGER, \n\tsimilarity_score FLOAT, \n\tpipeline_id VARCHAR(255), \n\tsnapshot_path VARCHAR(512), \n\tacknowledged BOOLEAN NOT NULL, \n\tacknowledged_by INTEGER, \n\tacknowledged_at TIMESTAMP WITHOUT TIME ZONE, \n\tnotes TEXT, \n\tcreated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, \n\tPRIMARY KEY (id), \n\tFOREIGN KEY(watchlist_entry_id) REFERENCES watchlist_entries (id) ON DELETE CASCADE, \n\tFOREIGN KEY(search_id) REFERENCES search_history (id) ON DELETE SET NULL, \n\tFOREIGN KEY(detection_id) REFERENCES detections (id) ON DELETE SET NULL, \n\tFOREIGN KEY(pipeline_id) REFERENCES pipelines (pipeline_id) ON DELETE SET NULL, \n\tFOREIGN KEY(acknowledged_by) REFERENCES users (id) ON DELETE SET NULL\n)',
    'CREATE INDEX IF NOT EXISTS idx_watchlist_alert_acknowledged ON watchlist_alerts (acknowledged, created_at)',
    'CREATE INDEX IF NOT EXISTS idx_watchlist_alert_detection ON watchlist_alerts (detection_id)',
    'CREATE INDEX IF NOT EXISTS idx_watchlist_alert_entry ON watchlist_alerts (watchlist_entry_id, created_at)',
    'CREATE INDEX IF NOT EXISTS ix_watchlist_alerts_created_at ON watchlist_alerts (created_at)',
    'CREATE INDEX IF NOT EXISTS ix_watchlist_alerts_id ON watchlist_alerts (id)',
    'CREATE INDEX IF NOT EXISTS ix_watchlist_alerts_watchlist_entry_id ON watchlist_alerts (watchlist_entry_id)',
    'CREATE UNIQUE INDEX IF NOT EXISTS uq_watchlist_alert_entry_detection ON watchlist_alerts (watchlist_entry_id, detection_id) WHERE detection_id IS NOT NULL'
]


def _enum_exists(bind, name: str) -> bool:
    return bool(bind.execute(sa.text("SELECT 1 FROM pg_type WHERE typname = :n"), {"n": name}).scalar())


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

-- ============================================================================
-- LAF-AI assistant: dedicated READ-ONLY database role
-- ============================================================================
-- Creates `laf_ai_readonly`, the ONLY credential the LAF-AI Data Agent plugin
-- may use. It is a whitelist: SELECT on the analytics tables listed below and
-- nothing else. No DEFAULT PRIVILEGES are granted, so tables created by future
-- migrations stay invisible to it until this file is extended on purpose.
--
-- Deliberately NOT built on fr_readonly: that role has SELECT on every table
-- (users.password_hash, token hashes, settings, audit logs) and inherits every
-- future table through ALTER DEFAULT PRIVILEGES in db/roles.sql.
--
-- Deliberately NOT a schema of views: a view pins the columns it references,
-- so a later migration that drops or renames one would fail until the view is
-- removed. Plain grants add no objects to the schema and are reversed
-- completely by db/laf_ai_readonly_drop.sql.
--
-- deploy.sh does not run this file. Apply it by hand (see CHATBOT-VAS.md in
-- ~/vas-assistant) and re-apply after a fresh production installation:
--
--   docker exec -i face_detector_prod-postgres-1 \
--       psql -U postgres -d face_recognition -v ON_ERROR_STOP=1 \
--       -v laf_ai_readonly_password="$(cat ~/vas-assistant/secrets/laf-ai-db.password)" \
--       < db/laf_ai_readonly.sql
--
-- If the password variable is not supplied, psql leaves the reference
-- unexpanded, the ALTER ROLE line is a syntax error, and ON_ERROR_STOP aborts
-- the whole script before any grant is made.
-- ============================================================================

-- ---------------------------------------------------------------------------
-- 1. Role (idempotent, house style from db/roles.sql)
-- ---------------------------------------------------------------------------
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'laf_ai_readonly') THEN
        CREATE ROLE laf_ai_readonly LOGIN;
    END IF;
END
$$;

ALTER ROLE laf_ai_readonly WITH PASSWORD :'laf_ai_readonly_password';
ALTER ROLE laf_ai_readonly
    NOSUPERUSER NOCREATEROLE NOCREATEDB NOREPLICATION NOBYPASSRLS NOINHERIT
    CONNECTION LIMIT 5;

-- Defense in depth on top of the grants: a session starts read-only, a runaway
-- query is cut at 30 s, and an idle open transaction cannot hold locks.
ALTER ROLE laf_ai_readonly SET default_transaction_read_only = on;
ALTER ROLE laf_ai_readonly SET statement_timeout = '30s';
ALTER ROLE laf_ai_readonly SET idle_in_transaction_session_timeout = '15s';
ALTER ROLE laf_ai_readonly SET search_path = public;

-- ---------------------------------------------------------------------------
-- 2. Reach: the database and the schema, nothing in it yet
-- ---------------------------------------------------------------------------
GRANT CONNECT ON DATABASE face_recognition TO laf_ai_readonly;
GRANT USAGE   ON SCHEMA public            TO laf_ai_readonly;
-- Start from zero so re-running this file after removing a table below also
-- removes the access (the whitelist is the file, not the accumulated state).
REVOKE ALL PRIVILEGES ON ALL TABLES    IN SCHEMA public FROM laf_ai_readonly;
REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public FROM laf_ai_readonly;
REVOKE ALL PRIVILEGES ON ALL FUNCTIONS IN SCHEMA public FROM laf_ai_readonly;

-- ---------------------------------------------------------------------------
-- 3. Whitelist: whole tables that hold analytics and no secrets or PII
-- ---------------------------------------------------------------------------
GRANT SELECT ON
    pipelines,
    pipeline_aliases,
    detections,
    faces,
    identities,
    identity_appearances,
    identity_relationships,
    identity_merges,
    merge_suggestions,
    identity_images,
    system_metrics,
    watchlist_entries,
    watchlist_alerts,
    live_alert_triggers,
    threat_assessments,
    risk_signal_results
TO laf_ai_readonly;

-- ---------------------------------------------------------------------------
-- 4. Whitelist: tables granted per column, withholding notification targets
--    (recipient lists and webhook URLs can embed tokens)
-- ---------------------------------------------------------------------------
GRANT SELECT (
    id, name, description, color, icon, alert_level,
    notify_dashboard, notify_email, notify_sms, notify_webhook,
    is_active, created_by, created_at, updated_at, version,
    deleted_at, deleted_by_user_id, deletion_reason
) ON watchlists TO laf_ai_readonly;

GRANT SELECT (
    id, name, identity_id, created_by, historical_created_by, min_similarity,
    pipeline_ids, time_window_enabled, time_window_start, time_window_end,
    active_days, cooldown_minutes,
    notify_dashboard, notify_email, notify_sms, notify_webhook,
    sound_alert, auto_capture_snapshot, auto_record_clip, clip_duration_seconds,
    expiration_type, expiration_date, expiration_detections,
    status, triggers_count, last_triggered_at, created_at, updated_at
) ON live_search_alerts TO laf_ai_readonly;

-- ---------------------------------------------------------------------------
-- 5. What is NOT granted, and why (kept here so the boundary is reviewable)
-- ---------------------------------------------------------------------------
--   users, deleted_users, user_pipeline_access ........ accounts, password_hash, authz
--   webhook_credentials, pending_enrollments .......... token hashes
--   settings, settings_audit_log ...................... values flagged is_sensitive
--   *_audit_log, search_history, live_alert_audit_log . IP addresses, user agents
--   chatbot_audit_log, user_query_*, user_conversation_*,
--   conversations, conversation_branches, messages,
--   agent_artifacts, message_feedback ................. other users' chat content
--   identity_embeddings, user_query_embeddings ........ 512-d vectors; no analytic value
--   background_task_history, ml_worker_heartbeats,
--   ml_*, similarity_*, risk_model_versions,
--   learned_thresholds ................................ ML-ops internals, payloads
--   organizations, workspaces, workspace_members ...... workspace settings JSON
--   alembic_version ................................... migration state

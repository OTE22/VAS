-- ============================================================================
-- Remove the LAF-AI assistant's read-only database role.
-- ============================================================================
-- Reverses db/laf_ai_readonly.sql completely. Safe to run when the role does
-- not exist. Nothing else in the database is touched: the role owns no
-- objects (it was granted SELECT only), so no CASCADE is needed.
--
--   docker exec -i face_detector_prod-postgres-1 \
--       psql -U postgres -d face_recognition -v ON_ERROR_STOP=1 \
--       < db/laf_ai_readonly_drop.sql
-- ============================================================================

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'laf_ai_readonly') THEN
        -- Terminate any open session first; DROP ROLE fails while one is connected.
        PERFORM pg_terminate_backend(pid)
            FROM pg_stat_activity
            WHERE usename = 'laf_ai_readonly' AND pid <> pg_backend_pid();
        REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA public FROM laf_ai_readonly;
        REVOKE ALL PRIVILEGES ON SCHEMA public FROM laf_ai_readonly;
        REVOKE CONNECT ON DATABASE face_recognition FROM laf_ai_readonly;
        DROP ROLE laf_ai_readonly;
        RAISE NOTICE 'laf_ai_readonly removed';
    ELSE
        RAISE NOTICE 'laf_ai_readonly does not exist; nothing to do';
    END IF;
END
$$;

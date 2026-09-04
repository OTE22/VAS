-- =====================================================================
-- Least-privilege database roles
-- =====================================================================
-- The application, Alembic and the SQL agent all connected as the `postgres`
-- superuser. Postgres knows nothing about the application's authorization
-- checks, so anyone who reached the port with that password could read or
-- modify every embedding, identity and user row while bypassing all of them —
-- and leave no audit trail, because no API endpoint was involved.
--
-- Idempotent, and safe to run against a populated database. init-db.sql only
-- executes on a fresh volume, so run this by hand once on existing
-- deployments:
--
--   PGPASSWORD=<superuser-pw> psql -h localhost -U postgres \
--     -d face_recognition -v ON_ERROR_STOP=1 \
--     -v app_password="'...'" -v migrator_password="'...'" \
--     -v readonly_password="'...'" -v backup_password="'...'" \
--     -f db/roles.sql
--
-- Or inside the running stack:
--   docker compose exec -T postgres psql -U postgres -d face_recognition \
--     -v ON_ERROR_STOP=1 -v app_password="'...'" ... < db/roles.sql
--
-- Roles created:
--   fr_app       application runtime. Owns the schema, so it can still create
--                the pgvector HNSW indexes the code builds at startup, but has
--                no SUPERUSER / CREATEROLE / CREATEDB / REPLICATION and cannot
--                install extensions.
--   fr_migrator  Alembic only. DDL rights, used by the one-shot migrate job.
--   fr_readonly  reporting and diagnostics. SELECT only.
--   fr_backup    pg_dump. SELECT plus the ability to read all tables.
-- =====================================================================

\set ON_ERROR_STOP on

-- ---------------------------------------------------------------------
-- Roles
-- ---------------------------------------------------------------------
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fr_app') THEN
        CREATE ROLE fr_app LOGIN;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fr_migrator') THEN
        CREATE ROLE fr_migrator LOGIN;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fr_readonly') THEN
        CREATE ROLE fr_readonly LOGIN;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fr_backup') THEN
        CREATE ROLE fr_backup LOGIN;
    END IF;
END
$$;

-- Passwords come from psql variables so they are never written into this file.
ALTER ROLE fr_app       WITH PASSWORD :'app_password';
ALTER ROLE fr_migrator  WITH PASSWORD :'migrator_password';
ALTER ROLE fr_readonly  WITH PASSWORD :'readonly_password';
ALTER ROLE fr_backup    WITH PASSWORD :'backup_password';

-- Explicitly strip privileges that must never be held, in case a role already
-- existed with more than it should.
ALTER ROLE fr_app       NOSUPERUSER NOCREATEROLE NOCREATEDB NOREPLICATION NOBYPASSRLS;
ALTER ROLE fr_migrator  NOSUPERUSER NOCREATEROLE NOCREATEDB NOREPLICATION NOBYPASSRLS;
ALTER ROLE fr_readonly  NOSUPERUSER NOCREATEROLE NOCREATEDB NOREPLICATION NOBYPASSRLS;
ALTER ROLE fr_backup    NOSUPERUSER NOCREATEROLE NOCREATEDB NOREPLICATION NOBYPASSRLS;

-- ---------------------------------------------------------------------
-- Database and schema
-- ---------------------------------------------------------------------
GRANT CONNECT ON DATABASE face_recognition TO fr_app, fr_migrator, fr_readonly, fr_backup;

-- Nobody but the owner may create objects in public by default.
REVOKE ALL ON SCHEMA public FROM PUBLIC;
GRANT USAGE ON SCHEMA public TO fr_app, fr_migrator, fr_readonly, fr_backup;

-- fr_app owns the schema: the application creates pgvector HNSW indexes and a
-- few tables at runtime. Revoking that would move those failures to startup.
GRANT CREATE ON SCHEMA public TO fr_app, fr_migrator;

-- ---------------------------------------------------------------------
-- Existing objects
-- ---------------------------------------------------------------------
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES    IN SCHEMA public TO fr_app;
GRANT USAGE, SELECT                  ON ALL SEQUENCES IN SCHEMA public TO fr_app;
GRANT EXECUTE                        ON ALL FUNCTIONS IN SCHEMA public TO fr_app;

GRANT ALL    ON ALL TABLES    IN SCHEMA public TO fr_migrator;
GRANT ALL    ON ALL SEQUENCES IN SCHEMA public TO fr_migrator;

GRANT SELECT ON ALL TABLES    IN SCHEMA public TO fr_readonly, fr_backup;
GRANT SELECT ON ALL SEQUENCES IN SCHEMA public TO fr_backup;

-- ---------------------------------------------------------------------
-- Future objects
-- ---------------------------------------------------------------------
-- Without these, every new migration would create tables the application role
-- cannot read, and the failure would appear only after the next deploy.
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO fr_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT USAGE, SELECT ON SEQUENCES TO fr_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT SELECT ON TABLES TO fr_readonly, fr_backup;
-- SELECT only (not USAGE): pg_dump reads each sequence's last_value; it never
-- calls nextval. Without this, a migration-created sequence blocks the backup.
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT SELECT ON SEQUENCES TO fr_backup;

-- Objects created BY the migrator must also be reachable by the application.
ALTER DEFAULT PRIVILEGES FOR ROLE fr_migrator IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO fr_app;
ALTER DEFAULT PRIVILEGES FOR ROLE fr_migrator IN SCHEMA public
    GRANT USAGE, SELECT ON SEQUENCES TO fr_app;
ALTER DEFAULT PRIVILEGES FOR ROLE fr_migrator IN SCHEMA public
    GRANT SELECT ON TABLES TO fr_readonly, fr_backup;
-- The migrator is what actually CREATES these sequences, so this is the grant
-- that matters for every future migration.
ALTER DEFAULT PRIVILEGES FOR ROLE fr_migrator IN SCHEMA public
    GRANT SELECT ON SEQUENCES TO fr_backup;

-- ---------------------------------------------------------------------
-- Transfer ownership of existing objects to fr_app
-- ---------------------------------------------------------------------
-- Required for ALTER TABLE / CREATE INDEX on tables that postgres created
-- before this script existed.
DO $$
DECLARE
    obj record;
BEGIN
    FOR obj IN
        SELECT tablename AS name FROM pg_tables
        WHERE schemaname = 'public' AND tableowner <> 'fr_app'
    LOOP
        EXECUTE format('ALTER TABLE public.%I OWNER TO fr_app', obj.name);
    END LOOP;

    FOR obj IN
        SELECT sequencename AS name FROM pg_sequences
        WHERE schemaname = 'public' AND sequenceowner <> 'fr_app'
    LOOP
        EXECUTE format('ALTER SEQUENCE public.%I OWNER TO fr_app', obj.name);
    END LOOP;

    FOR obj IN
        SELECT viewname AS name FROM pg_views
        WHERE schemaname = 'public' AND viewowner <> 'fr_app'
    LOOP
        EXECUTE format('ALTER VIEW public.%I OWNER TO fr_app', obj.name);
    END LOOP;
END
$$;

-- fr_migrator needs ownership rights to run DDL; membership grants them
-- without making it a second owner.
GRANT fr_app TO fr_migrator;

-- ---------------------------------------------------------------------
-- Verification
-- ---------------------------------------------------------------------
-- Expect four rows, every boolean column false:
SELECT rolname, rolsuper, rolcreaterole, rolcreatedb, rolreplication, rolbypassrls
FROM pg_roles
WHERE rolname IN ('fr_app', 'fr_migrator', 'fr_readonly', 'fr_backup')
ORDER BY rolname;

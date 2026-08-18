-- Least-privilege roles for the ISOLATED REGRESSION postgres only.
--
-- The regression stack runs its own PostgreSQL, so it starts with an empty
-- role catalogue. Roles are cluster-wide, which is why this was invisible
-- before: the regression used to create a scratch database on the DEVELOPER's
-- server, where db/roles.sql had already been applied by hand, and simply
-- inherited fr_readonly from it. On a fresh server the SQL agent cannot
-- authenticate and `test_sql_execution_is_actually_read_only` fails with
-- "password authentication failed for user fr_readonly" — a missing role, not
-- a broken guard.
--
-- Deliberately NOT db/roles.sql: that file is the production procedure, takes
-- psql variables for four passwords, and is applied by an operator with real
-- secrets. This creates only what the suite needs, with the throwaway password
-- the regression compose passes to the API, and it runs exclusively inside a
-- tmpfs database that is destroyed with the stack.
--
-- Runs after init-db.sql (docker-entrypoint-initdb.d, alphabetical), so
-- face_recognition already exists.

\c face_recognition

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fr_readonly') THEN
        CREATE ROLE fr_readonly LOGIN;
    END IF;
END
$$;

-- Matches SQL_AGENT_DB_PASSWORD in docker-compose.cpu.yml, which the
-- regression service inherits via `extends`. A test credential for a database
-- that exists for minutes; the production password comes from a Docker secret.
ALTER ROLE fr_readonly WITH PASSWORD 'TestRo-8jT5nKd3Yc';
ALTER ROLE fr_readonly NOSUPERUSER NOCREATEROLE NOCREATEDB NOREPLICATION NOBYPASSRLS;

GRANT CONNECT ON DATABASE face_recognition TO fr_readonly;
GRANT USAGE ON SCHEMA public TO fr_readonly;

-- SELECT on everything that exists now and on everything Alembic creates
-- afterwards. The point of the role is that it can read and cannot write, so
-- the suite can prove the SQL agent's execution path is genuinely read-only
-- rather than merely intending to be.
GRANT SELECT ON ALL TABLES IN SCHEMA public TO fr_readonly;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO fr_readonly;

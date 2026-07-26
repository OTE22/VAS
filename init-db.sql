-- Initialize databases for Face Recognition Service
-- Runs once on first container startup

-- Create Grafana database
CREATE DATABASE grafana;
GRANT ALL PRIVILEGES ON DATABASE grafana TO postgres;

-- Connect to face_recognition DB
\c face_recognition

-- Extensions
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE EXTENSION IF NOT EXISTS "pg_stat_statements";
CREATE EXTENSION IF NOT EXISTS "vector";  -- pgvector for embedding similarity search

-- Safe per-database performance tuning
ALTER DATABASE face_recognition SET work_mem = '16MB';
ALTER DATABASE face_recognition SET maintenance_work_mem = '64MB';

-- Optional planner hint (safe but informational only)
ALTER DATABASE face_recognition SET effective_cache_size = '1GB';

-- Confirmation
\echo 'Database initialization complete!'
\echo 'Databases created: face_recognition, grafana'
\echo 'Extensions enabled: uuid-ossp, pg_stat_statements, vector (pgvector)'

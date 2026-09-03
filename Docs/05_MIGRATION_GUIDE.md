# Database Migration Guide

This guide explains how to create the new User and UserPipelineAccess tables for the RBAC system.

## Option 1: Simple Migration Script (Recommended for Quick Setup)

This is the easiest method if you just want to create the tables quickly.

### Steps:

1. **Make sure your database is running** (PostgreSQL)

2. **Run the migration script:**
   ```bash
   docker exec -w /app/alembic face_recognition_api python -m alembic upgrade head   # Alembic is the ONLY schema tool (run_migration.py was retired)
   ```

   This script will:
   - Connect to your database using the DATABASE_URL from config
   - Check if the tables already exist
   - Create the `users` and `user_pipeline_access` tables if they don't exist

3. **Verify the tables were created:**
   ```bash
   # Connect to PostgreSQL and check
   psql -U postgres -d face_recognition -c "\dt"
   ```

   You should see:
   - `users`
   - `user_pipeline_access`

## Option 2: Using Alembic (Recommended for Production)

Alembic provides better version control and migration history. Use this if you want to track schema changes over time.

### Prerequisites:

1. **Install psycopg2-binary** (required for Alembic):
   ```bash
   pip install psycopg2-binary
   ```

2. **Make sure your database is running**

### Steps:

1. **Alembic is already initialized** (alembic/ directory exists)

2. **Create a migration:**
   ```bash
   alembic revision --autogenerate -m "Add User and UserPipelineAccess tables for RBAC"
   ```

   This will create a migration file in `alembic/versions/`

3. **Review the migration file** (optional but recommended):
   - Open the generated file in `alembic/versions/`
   - Check that it includes `create_table('users')` and `create_table('user_pipeline_access')`
   - Make any necessary adjustments

4. **Run the migration:**
   ```bash
   alembic upgrade head
   ```

5. **Verify:**
   ```bash
   # Check migration status
   alembic current
   
   # Or check in PostgreSQL
   psql -U postgres -d face_recognition -c "\dt"
   ```

### Future Migrations:

When you need to make schema changes:

1. Modify `db_models.py`
2. Create migration: `alembic revision --autogenerate -m "Description"`
3. Review the migration file
4. Apply: `alembic upgrade head`

### When you add a migration, move the head pins

The Alembic head is pinned in five places so a drifted schema fails closed
instead of serving traffic. A new revision that leaves them behind makes a
fresh production deploy refuse to migrate (`REVISION_MISMATCH`) and fails
these tests in the full regression:

| Where | What to change |
|---|---|
| `docker/docker-compose.prod.yml` | both `MIGRATIONS_EXPECTED_HEAD:-<head>` defaults |
| `tests/test_vector_index_migration.py` | `EXPECTED_HEAD` |
| `tests/test_identity_merge_smoke.py` | `test_schema_head_is_current` |
| `tests/test_ml_foundation.py` | `test_migration_head_and_seeds` |
| `tests/test_risk_platform.py` | `test_migration_applied_and_indexed` |

`tests/test_compose_and_deployment.py::test_prod_migrations_head_default_matches_the_repo_head`
compares the compose default against the repository head, so it is the one
that tells you the pins are stale. Find every pin with:

```bash
grep -rn "<old head>" tests/ docker/ config.py
```

Leave the `down_revision` inside the new migration file alone; that one is
the chain itself.

### Rollback (if needed):

```bash
# Rollback one migration
alembic downgrade -1

# Rollback to specific revision
alembic downgrade <revision_id>
```

## Option 3: Automatic Creation (Current Behavior)

**Alembic is the only schema initializer.** `Base.metadata.create_all()` no longer exists in the application (AST-asserted by `tests/test_migration_schema_parity.py`); an EMPTY database is built by `alembic upgrade head` from the root revision `000_baseline` (the 24 create_all-era tables as a frozen literal) through the whole chain, and `DatabaseManager.init_db()` refuses to boot unless `alembic_version` equals the scripts' exact head — fail-closed in every environment (`MIGRATIONS_FAIL_CLOSED` was removed). Historically the application used `create_all()` when the app starts.

**However**, this method:
- ✅ Works automatically
- ❌ Doesn't track migration history
- ❌ Can't rollback changes
- ❌ May fail if tables already exist with different schemas

**For production, use Option 1 or Option 2 instead.**

## Troubleshooting

### Error: "ModuleNotFoundError: No module named 'psycopg2'"

**Solution:**
```bash
pip install psycopg2-binary
```

### Error: "Connection refused" or "Could not connect"

**Solutions:**
1. Make sure PostgreSQL is running:
   ```bash
   # Docker — there is no compose file at the repository root; they live in docker/
   docker compose -f docker/docker-compose.cpu.yml up -d postgres
   
   # Or check if running
   docker ps | grep postgres
   ```

2. Check DATABASE_URL in `.env` file:
   ```
   DATABASE_URL=postgresql+asyncpg://postgres:admin@postgres:5432/face_recognition
   ```

3. Test connection:
   ```bash
   psql -U postgres -h localhost -d face_recognition
   ```

### Error: "Table already exists"

If tables already exist but you want to recreate them:

**Option A: Drop and recreate (⚠️ WARNING: This deletes data!)**
```sql
DROP TABLE IF EXISTS user_pipeline_access CASCADE;
DROP TABLE IF EXISTS users CASCADE;
```
Then run the migration again.

**Option B: Use Alembic to detect differences**
```bash
alembic revision --autogenerate -m "Update existing tables"
alembic upgrade head
```

## After Migration

Once the tables are created:

1. **Restart your application** - one administrator is bootstrapped on startup
   if no active admin exists
2. **Sign in** with the username and the password from
   `secrets/bootstrap_admin_password`. On the development CPU stack the
   convenience credential is `admin` / `admin123`
3. **Change the password** — you are redirected to `/change-password` and the
   account cannot do anything else until you do. This is **not** done through
   the admin panel: `/admin/users` is gated and will redirect you back. See
   [`61_DEPLOYMENT_RUNBOOK.md`](61_DEPLOYMENT_RUNBOOK.md) §7
4. **Create additional users** as needed — each must likewise change the
   password you assign at their first sign-in

## Verification

After migration, verify the tables exist:

```sql
-- Connect to database
psql -U postgres -d face_recognition

-- List tables
\dt

-- Check users table structure
\d users

-- Check user_pipeline_access table structure
\d user_pipeline_access

-- Exit
\q
```

You should see both tables with all the expected columns.


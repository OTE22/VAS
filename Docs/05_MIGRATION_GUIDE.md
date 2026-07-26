# Database Migration Guide

This guide explains how to create the new User and UserPipelineAccess tables for the RBAC system.

## Option 1: Simple Migration Script (Recommended for Quick Setup)

This is the easiest method if you just want to create the tables quickly.

### Steps:

1. **Make sure your database is running** (PostgreSQL)

2. **Run the migration script:**
   ```bash
   python scripts/migrations/run_migration.py
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

### Rollback (if needed):

```bash
# Rollback one migration
alembic downgrade -1

# Rollback to specific revision
alembic downgrade <revision_id>
```

## Option 3: Automatic Creation (Current Behavior)

The application already uses `Base.metadata.create_all()` in `db_connection.py`, which automatically creates all tables defined in `db_models.py` when the app starts.

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
   # Docker
   docker-compose up -d postgres
   
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

1. **Restart your application** - The default admin user will be created automatically on first startup
2. **Sign in** with:
   - Username: `admin`
   - Password: `admin123`
3. **Change the admin password** immediately in the admin panel
4. **Create additional users** as needed

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


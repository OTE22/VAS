# Alembic Migrations in Docker - Complete Guide

This guide explains how to run Alembic database migrations inside Docker containers.

## 📋 Prerequisites

- Docker and Docker Compose installed
- Container `face_recognition_api` is running
- Alembic is configured with `alembic.ini` in the `alembic/` folder

## 🚀 Quick Start

### Using the Helper Scripts

#### Linux/Mac (Bash)
```bash
# Make script executable
chmod +x docker/run_alembic_migration.sh

# Run migrations
./docker/run_alembic_migration.sh upgrade
```

#### Windows (PowerShell)
```powershell
# Run migrations
.\docker\run_alembic_migration.ps1 upgrade
```

## 📝 Common Commands

### 1. Check Current Migration Status
```bash
# Bash
./docker/run_alembic_migration.sh current

# PowerShell
.\docker\run_alembic_migration.ps1 current
```

### 2. View Migration History
```bash
# Bash
./docker/run_alembic_migration.sh history

# PowerShell
.\docker\run_alembic_migration.ps1 history
```

### 3. Upgrade Database to Latest
```bash
# Bash
./docker/run_alembic_migration.sh upgrade

# PowerShell
.\docker\run_alembic_migration.ps1 upgrade
```

### 4. Create New Migration (Auto-generate)
```bash
# Bash
./docker/run_alembic_migration.sh revision --autogenerate -m "Add user query history tables"

# PowerShell
.\docker\run_alembic_migration.ps1 revision --autogenerate -m "Add user query history tables"
```

### 5. Create Manual Migration
```bash
# Bash
./docker/run_alembic_migration.sh revision -m "Add new column to users table"

# PowerShell
.\docker\run_alembic_migration.ps1 revision -m "Add new column to users table"
```

### 6. Downgrade Database
```bash
# Bash (downgrade one step)
./docker/run_alembic_migration.sh downgrade -1

# PowerShell
.\docker\run_alembic_migration.ps1 downgrade -1
```

## 🔧 Direct Docker Commands

If you prefer to run Alembic commands directly:

### Basic Syntax
```bash
docker exec -w /app/alembic face_recognition_api python -m alembic <command>
```

### Examples

#### Check Current Revision
```bash
docker exec -w /app/alembic face_recognition_api python -m alembic current
```

#### View History
```bash
docker exec -w /app/alembic face_recognition_api python -m alembic history --verbose
```

#### Upgrade to Head
```bash
docker exec -w /app/alembic face_recognition_api python -m alembic upgrade head
```

#### Create Auto-generated Migration
```bash
docker exec -w /app/alembic face_recognition_api python -m alembic revision --autogenerate -m "Add user query history tables"
```

#### Create Manual Migration
```bash
docker exec -w /app/alembic face_recognition_api python -m alembic revision -m "Add new feature"
```

#### Show Migration Details
```bash
docker exec -w /app/alembic face_recognition_api python -m alembic show head
```

#### Downgrade One Step
```bash
docker exec -w /app/alembic face_recognition_api python -m alembic downgrade -1
```

#### Downgrade to Base
```bash
docker exec -w /app/alembic face_recognition_api python -m alembic downgrade base
```

#### Stamp Database (mark as migrated without running)
```bash
docker exec -w /app/alembic face_recognition_api python -m alembic stamp head
```

## 📂 File Structure

```
project/
├── alembic/
│   ├── alembic.ini          # Alembic configuration
│   ├── env.py               # Migration environment
│   └── versions/            # Migration files
│       ├── 001_xxx.py
│       ├── 002_xxx.py
│       └── ...
├── docker/
│   ├── run_alembic_migration.sh    # Bash helper script
│   └── run_alembic_migration.ps1   # PowerShell helper script
└── db_models.py             # SQLAlchemy models
```

## 🔍 Important Notes

### Working Directory
- Alembic commands must be run from `/app/alembic` directory inside the container
- The `-w /app/alembic` flag sets the working directory

### Configuration
- `alembic.ini` is located at `/app/alembic/alembic.ini` inside the container
- Database URL is automatically configured from `config.py` settings
- The `env.py` file handles database connection setup

### Migration Files
- New migration files are created in `alembic/versions/` directory
- Files are automatically synced if you have volume mounts configured
- Check `docker-compose.cpu.yml` for volume mappings

## 🛠️ Troubleshooting

### Container Not Running
```bash
# Start containers
docker-compose -f docker/docker-compose.cpu.yml up -d

# Check container status
docker ps | grep face_recognition_api
```

### Permission Issues
```bash
# Check container user
docker exec face_recognition_api whoami

# Run as root if needed (not recommended for production)
docker exec -u root -w /app/alembic face_recognition_api python -m alembic upgrade head
```

### Database Connection Issues
- Verify database is running: `docker ps | grep postgres`
- Check database URL in `config.py` or environment variables
- Ensure database credentials are correct

### Migration File Not Found
- Check if migration file exists: `docker exec face_recognition_api ls -la /app/alembic/versions/`
- Copy migration file if needed: `docker cp alembic/versions/XXX.py face_recognition_api:/app/alembic/versions/`

### View Migration Logs
```bash
# Check container logs
docker logs face_recognition_api | grep -i alembic

# Follow logs in real-time
docker logs -f face_recognition_api
```

## 📋 Step-by-Step: Creating a New Migration

### 1. Update Models
Edit `db_models.py` to add/modify your models.

### 2. Create Migration
```bash
# Auto-generate migration from model changes
./docker/run_alembic_migration.sh revision --autogenerate -m "Add user query history tables"
```

### 3. Review Migration File
Check the generated file in `alembic/versions/` and verify the changes are correct.

### 4. Apply Migration
```bash
# Apply the migration
./docker/run_alembic_migration.sh upgrade
```

### 5. Verify
```bash
# Check current revision
./docker/run_alembic_migration.sh current
```

## 🎯 Best Practices

1. **Always Review Auto-generated Migrations**
   - Auto-generate creates migrations based on model differences
   - Review and edit if needed before applying

2. **Test Migrations**
   - Test on development database first
   - Backup production database before migrating

3. **Use Descriptive Messages**
   - Use clear, descriptive migration messages
   - Example: "Add user query history tables" not "update"

4. **Version Control**
   - Commit migration files to version control
   - Keep migrations in sync with code changes

5. **Rollback Plan**
   - Know how to rollback if migration fails
   - Test downgrade procedures

## 🔄 Migration Workflow

```
1. Update db_models.py
   ↓
2. Create migration: revision --autogenerate
   ↓
3. Review migration file
   ↓
4. Apply migration: upgrade head
   ↓
5. Verify: current
```

## 📚 Additional Resources

- [Alembic Documentation](https://alembic.sqlalchemy.org/)
- [SQLAlchemy Documentation](https://docs.sqlalchemy.org/)
- Project-specific migration examples in `alembic/versions/`



---

## Corrective pass 2026-08-16 — what changed for operators

| Item | Now |
|---|---|
| Schema initializer | Alembic ONLY. `Base.metadata.create_all()` is gone; root revision `000_baseline` creates the 24 create_all-era tables (frozen literal, `scripts/dev/generate_baseline_migration.py` documents the shape rule). |
| Boot check | `DatabaseManager.init_db()` → `verify_database_head()`: `alembic_version` must equal `ScriptDirectory.get_current_head()` — one head, exact match, fail-closed everywhere. `MIGRATIONS_MODE: run` (dev) migrates in phase 1.0 first; `verify` (prod) only checks; `MIGRATIONS_EXPECTED_HEAD` remains the operator-visible pin (compose default asserted equal to the scripts' head by `tests/test_compose_and_deployment.py`). |
| `MIGRATIONS_FAIL_CLOSED` | REMOVED (settings, `.env.example`, compose, docs, tests). Its only remaining effect would have been permitting a schema-mismatched boot — forbidden. |
| Head | `f6a7b8c9d0e1` (chain: … `b0c1d2e3f4a5` → `c2d3e4f5a6b7` relationship integrity → `d4e5f6a7b8c9` ML lineage → `e5f6a7b8c9d0` create_all residue alignment → `f6a7b8c9d0e1` prediction lineage RESTRICT). |
| Preconditions | Migrations never delete. `_require_zero()` refuses with the row count and the repair command. Order for a legacy dev/demo database: `python scripts/repair_relationship_integrity.py` (dry-run) → `--apply --yes-i-understand` → `alembic upgrade head`. Never on production. |
| Parity proof | `tests/test_migration_schema_parity.py`: unique scratch DB → `upgrade head` → schema dump == dev DB (0 diffs), every new constraint by name, feature definitions 24 / policies 4 / `system` principal, app boot smoke, precondition refusal on a scratch DB, `finally: DROP DATABASE`. |
| Full regression | `scripts/run_regression_isolated.sh` — scratch DB + dedicated `redis_regression` + throwaway `face_recognition_regression` container (its own service name so the dev nginx upstream `face_recognition` never resolves to it — asserted), ephemeral volumes, isolation assertions before pytest, teardown on every exit path. Never against the dev DB / dev Redis. |

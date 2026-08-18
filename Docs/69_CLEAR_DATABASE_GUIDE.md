# Clearing Data and Stored Images

> ## ⚠️ DEVELOPMENT DATABASE ONLY
>
> Every procedure in this guide **destroys data**. It exists for resetting the
> development database after testing. **Never run any command from this guide
> against production.** Production data removal goes through the retention
> system and [`60_BACKUP_AND_RESTORE.md`](60_BACKUP_AND_RESTORE.md) — with a
> verified backup taken first.

Two maintained scripts handle this, and they remove **stored images as well as
rows** — which the raw SQL further down does not. Clearing rows alone leaves
every face still on disk.

- `scripts/wipe_operational_data.py` — the routine reset. Keeps accounts,
  workspaces, migration state and seeded ML config; the app keeps working.
- `scripts/wipe_all_except_users.py` — keeps **only `users`**, or add
  `--keep-boot-rows` to keep the app bootable.

Both default to a **dry run**. Jump to [Running a wipe](#running-a-wipe).

## Database Configuration
- **Container Name**: `face_recognition_db`
- **Database Name**: `face_recognition`
- **User**: `postgres`
- **Password**: `admin`

---

# Recommended: use the maintained scripts

Two scripts do this properly. Prefer them over the raw SQL further down — the
manual commands clear *rows* and leave every stored image on disk, which is how
you end up with a database that says a person has no photos and a `storage/`
directory still full of their face.

| | `wipe_operational_data.py` | `wipe_all_except_users.py` |
|---|---|---|
| Keeps | accounts, workspaces, migration state, seeded ML config | **only `users`** (or `+ --keep-boot-rows`) |
| Tables cleared | 52 of 59 | 58 of 59 (52 with `--keep-boot-rows`) |
| App still starts after | ✅ yes | ⚠️ only with `--keep-boot-rows` |
| Use when | after a regression run; routine demo reset | you want a bare database |

**If you are unsure, use the first one.** It is the standing post-regression
cleanup and leaves a working system.

## Choosing what survives

Both scripts derive the table list **from the live schema**, so a table added by
a future migration is included automatically rather than silently skipped. They
issue **one `TRUNCATE`, without `CASCADE`** — `CASCADE` would follow foreign keys
out of the list and could delete something the script promised to keep, so
instead PostgreSQL refuses and a pre-flight check names the offending reference:

```
❌ TRUNCATE would fail: a preserved table references a truncated one.
     identity_embeddings -> detections
     identity_embeddings -> identities
   Add the referenced table to --keep, or widen the preserve set.
```

### The four tables that are not "data"

`--keep-boot-rows` protects these. Without it the wipe succeeds and the
application will not fully start:

| Table | What breaks without it | Recovery |
|---|---|---|
| `alembic_version` | The startup preflight cannot confirm the schema matches the code. Production compose pins `MIGRATIONS_EXPECTED_HEAD` and exits **78** | `alembic stamp head` |
| `workspaces`, `organizations`, `workspace_members` | `get_default_workspace_id()` raises; every `POST /api/v1/conversations` answers **500**. Nothing re-seeds it — the row came from migration `d3e4f5a6b7c8`, which will not run again | restore the row by hand or from a dump |

## Running a wipe

Always dry-run first — it is the default, and it changes nothing:

```bash
docker exec -w /app face_recognition_api \
    python scripts/wipe_all_except_users.py --keep-boot-rows --verbose
```

```
database           : face_recognition
schema tables      : 59
preserved          : 7 -> alembic_version, ml_feature_definitions, ml_retraining_policies,
                          organizations, users, workspace_members, workspaces
to truncate        : 54 tables, 1124 rows
files to delete    : 65 (59 images) under 9 directories
fixtures preserved : storage/qa-quality-regression (13 files)

✅ FK check           : OK — no preserved table references a truncated one

DRY RUN — nothing was changed. Re-run with --apply --yes-i-understand.
```

Then, with the API stopped so nothing writes mid-wipe:

```bash
docker stop face_recognition_api
docker compose -f docker/docker-compose.cpu.yml run --rm --no-deps \
    --entrypoint python face_recognition \
    scripts/wipe_all_except_users.py --keep-boot-rows --apply --yes-i-understand
docker start face_recognition_api
```

> Use `docker compose run`, not a bare `docker run`. A plain container lacks the
> compose environment and `config_guard` refuses to start it (**exit 78**).

`--apply` also requires `--yes-i-understand`, and prompts you to type the
database name unless you pass `--no-prompt`.

### If you use `docker exec` instead

`docker exec` gives the process no terminal, so the confirmation prompt cannot
be answered. Add `--no-prompt`:

```bash
docker exec -w /app face_recognition_api \
    python scripts/wipe_all_except_users.py \
    --keep-boot-rows --apply --yes-i-understand --no-prompt
```

or attach one with `docker exec -it`. Without either, the script stops before
the `TRUNCATE` and says so — nothing is changed.

## What is removed from disk

Rows alone are not enough: nothing in the codebase reconciles orphaned files
against missing rows, so the scripts are the only thing that removes them.

```
storage/<pipeline-uuid>/        every camera's snapshots — per-person crops
                                and unknown/ frames. Usually the BULK of the
                                images: 53 of 59 in a recent run
storage/faces/<identity-uuid>/  the enrolled gallery
storage/pending/                parked two-phase uploads
storage/debug/                  cropped + webhook debug frames
models/ml/candidates|datasets/  trained candidates and training sets
sql_agent/chromadb_data/        the chatbot / SQL-agent knowledge base
```

The pipeline folders are named after pipeline IDs, so the target list is
**derived by walking `storage/`** rather than hard-coded — a hard-coded list is
wrong the moment a camera is added.

### Deliberately preserved

| Path | Why |
|---|---|
| `storage/qa-quality-regression/` | Regression fixtures, not data. Deleting it breaks the quality suite |
| `~/.cache/chroma/onnx_models` | **167 MB of downloaded model.** Not data; removing it forces a slow refetch |
| `logs/` | The diagnostic record of what happened. Opt in with `--include-logs` |

### Chatbot data

Conversations, branches, messages and feedback live in the database and are
truncated with everything else. `sql_agent/chromadb_data` is the SQL-agent
knowledge base; clearing it is safe because `SQLKnowledgeBase` calls
`_auto_initialize_seed_examples()` in its constructor, so it re-seeds on the
next startup. Examples *learned* beyond the built-in seeds are lost — which is
the point of a wipe.

## Verifying afterwards

```bash
curl -s -o /dev/null -w '%{http_code}\n' http://localhost/health/detailed   # 200

# the default workspace must still resolve — a 500 here means the row is gone
curl -s -o /dev/null -w '%{http_code}\n' -X POST http://localhost/api/v1/conversations \
     -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
     -d '{"title":"post-wipe check"}'                                        # 200

docker exec face_recognition_db psql -U postgres -d face_recognition -c \
  "SELECT (SELECT count(*) FROM identities) identities,
          (SELECT count(*) FROM users) users,
          (SELECT count(*) FROM workspaces) workspaces,
          (SELECT version_num FROM alembic_version) head;"
```

## Useful flags

| Flag | Effect |
|---|---|
| *(none)* | Dry run. Reports what would change and exits |
| `--apply --yes-i-understand` | Actually truncate. Both are required |
| `--keep-boot-rows` | Also preserve the six startup tables (workspace rows, migration state, seeded ML config) |
| `--keep TABLE` | Preserve one more table (repeatable) |
| `--include-logs` | Also empty `logs/` |
| `--no-prompt` | Skip the typed database-name confirmation |
| `--verbose` | List every table with its row count, and every directory |

---

# Manual alternatives

Everything below works, but is an escape hatch rather than the normal path. None
of it deletes stored images, and the `CASCADE` variants can reach further than
you intend. Prefer the scripts above.

---

## Option 1: Drop All Tables (Keeps Database, Removes All Data & Schema)

This will remove all tables and their data, but keep the database itself:

```bash
# Connect to the container and drop all tables
docker exec -it face_recognition_db psql -U postgres -d face_recognition -c "
DO \$\$ 
DECLARE 
    r RECORD;
BEGIN
    FOR r IN (SELECT tablename FROM pg_tables WHERE schemaname = 'public') 
    LOOP
        EXECUTE 'DROP TABLE IF EXISTS ' || quote_ident(r.tablename) || ' CASCADE';
    END LOOP;
END \$\$;
"
```

---

## Option 2: Truncate All Tables (Keeps Schema, Removes Data Only)

This keeps all tables and structure but removes all data:

```bash
# Connect to the container and truncate all tables
docker exec -it face_recognition_db psql -U postgres -d face_recognition -c "
DO \$\$ 
DECLARE 
    r RECORD;
BEGIN
    FOR r IN (SELECT tablename FROM pg_tables WHERE schemaname = 'public') 
    LOOP
        EXECUTE 'TRUNCATE TABLE ' || quote_ident(r.tablename) || ' CASCADE';
    END LOOP;
END \$\$;
"
```

---

## Option 3: Drop and Recreate Database (Complete Reset)

This completely removes and recreates the database:

```bash
# Stop the container first (optional, but safer)
docker stop face_recognition_db

# Drop and recreate the database
docker exec -it face_recognition_db psql -U postgres -c "
DROP DATABASE IF EXISTS face_recognition;
CREATE DATABASE face_recognition;
"

# Restart the container
docker start face_recognition_db
```

---

## Option 4: Remove Docker Volume (Complete Reset Including Schema)

This removes the entire PostgreSQL data volume (most thorough reset):

```bash
# Stop the container
docker stop face_recognition_db

# Remove the container (optional, if you want to recreate it)
docker rm face_recognition_db

# Find the real volume name first — it is prefixed by the compose project
docker volume ls | grep postgres

# Remove the volume (this deletes ALL data)
docker volume rm <the-name-you-just-found>

# Recreate the volume and re-initialize the database.
# There is no compose file at the repository root; they live in docker/.
docker compose -f docker/docker-compose.cpu.yml up -d postgres
```

> **Never do this on production.** This is the development reset path. For a
> routine post-regression reset that preserves users and settings, use
> `scripts/wipe_operational_data.py` (Option 1) instead — it is the supported
> path and does not destroy the schema.

---

## Option 5: Interactive psql Session (Manual Control)

Connect interactively to the database:

```bash
# Connect to PostgreSQL inside the container
docker exec -it face_recognition_db psql -U postgres -d face_recognition

# Then run SQL commands:
# \dt                    # List all tables
# TRUNCATE TABLE faces CASCADE;  # Clear specific table
# DROP TABLE faces CASCADE;      # Drop specific table
# \q                    # Exit
```

---

## Quick Commands Reference

### Check what tables exist:
```bash
docker exec -it face_recognition_db psql -U postgres -d face_recognition -c "\dt"
```

### Check database size:
```bash
docker exec -it face_recognition_db psql -U postgres -d face_recognition -c "
SELECT pg_size_pretty(pg_database_size('face_recognition'));
"
```

### List all databases:
```bash
docker exec -it face_recognition_db psql -U postgres -c "\l"
```

### Check volume name:
```bash
docker volume ls | grep postgres
```

---

## Recommended Approach

For a **quick data reset** (keeping schema): Use **Option 2** (Truncate)
For a **complete reset** (including schema): Use **Option 3** (Drop & Recreate)
For a **nuclear reset** (including volume): Use **Option 4** (Remove Volume)


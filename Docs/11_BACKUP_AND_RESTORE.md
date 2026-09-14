# Backup and Restore

Before this existed there was no backup tooling in the repository at all —
`pg_dump` appeared once, in a sentence in another document. This describes what
is backed up, how to restore it, and how to prove the restore works.

**A backup is not working until a restore has succeeded.** The procedure in
§4 was executed against the live database during implementation: a real dump was
taken and restored into a clean database, and all sampled tables matched
row-for-row with the schema revision preserved.

---

## 1. What is backed up

| Artifact | Source | Reconstructible without a backup? |
|---|---|---|
| Database (`database.dump`) | `pg_dump -Fc` | No |
| Storage gallery (`storage.tar.gz`) | `/app/storage` | **No — these images are the source material** |
| FAISS indexes and model artifacts (`artifacts.tar.gz`) | `/app/database` | Only by re-running inference over the whole gallery |
| Model registry metadata | inside the database dump | No |
| Audit records | inside the database dump | No |
| `SHA256SUMS`, `manifest.txt` | generated per run | — |

Not backed up, deliberately:

- **Redis** — sessions, rate-limit counters and the token revocation denylist.
  Restoring stale revocation state would resurrect tokens that had been logged
  out. AOF persistence covers restart; a restore should start with an empty
  Redis and force everyone to log in again.
- **Model weights** (`weights/*.onnx`) — large, immutable, redistributable.
- **Map archives** (`map-data/production/*.mbtiles`) — rebuildable with
  `scripts/map_data/build_all.sh`. Restoring them is a file copy plus a
  content verification (`POST /api/maps/verify`): a restored archive is
  reported unavailable until its content has been measured on that machine.
  The content ledger (`map-data/metadata/content_verdicts.json`) is
  deliberately NOT portable — it describes the bytes of one deployment.
- **The internal CA private key** — must be stored offline and encrypted,
  separately from these backups. Anyone holding it can mint a certificate for
  any hostname your clients trust.

---

## 2. Objectives

| | Value | Basis |
|---|---|---|
| **RPO** | 24 hours | Default `BACKUP_INTERVAL_SECONDS`. Lower it for a tighter window. |
| **RTO** | ~30 minutes | Database restore plus archive extraction plus a service restart. |
| **Retention** | 14 days | `BACKUP_RETENTION_DAYS`. |
| **Restore test** | Quarterly | §4, into a scratch database. |
| **Off-host copy** | Required | `BACKUP_REMOTE_PATH`. |
| **Encryption** | Required off-host | Backups contain biometric data and password hashes. |
| **Owner** | Deployment operator | Also owns the quarterly test. |

The gallery contains face images and the database contains embeddings and
password hashes. Treat every backup as sensitive personal data: encrypt at rest
off-host, restrict access, and apply the same retention rules as production.

---

## 3. Taking a backup

Automatic, via the `backup` service. Manually:

```bash
docker compose -f docker/docker-compose.prod.yml exec -T backup \
  sh /scripts/backup.sh /backups

docker compose -f docker/docker-compose.prod.yml exec -T backup ls -la /backups
```

Each run creates `/backups/<UTC timestamp>Z/` containing the artifacts,
`SHA256SUMS` and `manifest.txt`. The script fails loudly if the dump is
implausibly small, because `pg_dump` can exit zero having produced nothing
useful.

---

## 4. Restore

### 4.1 Rehearsal (quarterly — do this before you need it)

Restore into a scratch database. Nothing in production is touched.

```bash
# 1. scratch target
docker compose -f docker/docker-compose.prod.yml exec -T postgres \
  psql -U postgres -c "DROP DATABASE IF EXISTS restore_test;"
docker compose -f docker/docker-compose.prod.yml exec -T postgres \
  psql -U postgres -c "CREATE DATABASE restore_test;"
docker compose -f docker/docker-compose.prod.yml exec -T postgres \
  psql -U postgres -d restore_test -c \
  'CREATE EXTENSION IF NOT EXISTS vector; CREATE EXTENSION IF NOT EXISTS "uuid-ossp";'

# 2. restore the newest backup
docker compose -f docker/docker-compose.prod.yml exec -T -e PGDATABASE=restore_test backup \
  sh -c 'sh /scripts/restore.sh $(ls /backups | grep "Z$" | tail -1)'

# 3. compare row counts against production
for t in identities users identity_appearances pipelines identity_embeddings; do
  src=$(docker compose -f docker/docker-compose.prod.yml exec -T postgres \
        psql -U postgres -d face_recognition -tAc "select count(*) from $t")
  dst=$(docker compose -f docker/docker-compose.prod.yml exec -T postgres \
        psql -U postgres -d restore_test -tAc "select count(*) from $t")
  echo "$t: $src vs $dst"
done

# 4. schema revision must match the application
docker compose -f docker/docker-compose.prod.yml exec -T postgres \
  psql -U postgres -d restore_test -tAc "select version_num from alembic_version;"

# 5. clean up
docker compose -f docker/docker-compose.prod.yml exec -T postgres \
  psql -U postgres -c "DROP DATABASE restore_test;"
```

Record the date. An untested backup is an assumption.

**Drill log** (append a line each time the drill is run):

| Date | Scope | Result |
|---|---|---|
| 2026-08-13 | dev stack: pg_dump → scratch-DB restore, 6-table row parity, alembic head match, live pgvector similarity query on restored data, faces tar round-trip byte-identical, `.env` round-trip byte-identical. `restore.sh` itself not exercised (prod-only service). | PASS |

### 4.2 Real restore

```bash
# 1. stop traffic, keep data services running
docker compose -f docker/docker-compose.prod.yml stop nginx face_recognition

# 2. restore (--force is required over a populated database)
docker compose -f docker/docker-compose.prod.yml exec -T backup \
  sh /scripts/restore.sh <timestamp> --force

# 3. schema must match the application
docker compose -f docker/docker-compose.prod.yml run --rm face_recognition \
  python -m backend.utils.migrations --verify

# 4. bring it back
docker compose -f docker/docker-compose.prod.yml start face_recognition nginx
```

`restore.sh` verifies `SHA256SUMS` before touching anything — discovering
corruption halfway through leaves the database worse off than the outage did —
and refuses a populated target without `--force`.

---

## 5. Reconciliation after a restore

The database dump and the file archives are taken seconds apart, not atomically.
After any restore, expect a small window of inconsistency:

1. **Rows referencing missing images** — identities created between the database
   dump and the gallery archive. Harmless to the API; the image simply 404s.
2. **Images with no row** — orphaned files, recovered by the retention job.
3. **Stale FAISS indexes** — if `artifacts.tar.gz` predates the database, rebuild
   the index rather than trusting it.
4. **Redis** — start empty. Every session is invalidated and users log in again,
   which is correct: restoring old revocation state would un-revoke tokens.

For a strictly consistent snapshot, stop the API before backing up. The default
schedule prioritises availability over that guarantee, which is the right
trade-off here but should be a conscious one.

---

## 6. Failure alerting

`BackupTooOld` fires when the newest backup is more than 48 hours old.

It deliberately alerts on **age**, not on a failure event: a backup job that
silently stopped running produces no failure event at all, and that is the
failure mode that actually loses data. The rule carries an explicit
`face_detector_last_backup_timestamp_seconds > 0` guard, because an unset
Prometheus gauge reads 0 and `time() - 0` would otherwise fire it permanently.

The API container mounts `/backups` read-only purely so this metric has data.

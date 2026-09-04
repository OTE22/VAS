#!/bin/sh
#
# Take one backup: PostgreSQL dump + storage gallery + FAISS/model artifacts.
#
#   sh scripts/backup/backup.sh [destination]
#
# Runs inside the `backup` service (postgres:15-alpine). Every artifact gets a
# SHA-256 checksum written alongside it, because a backup nobody has verified
# is a guess, not a backup.
#
# Restore: scripts/backup/restore.sh — and see Docs/60_BACKUP_AND_RESTORE.md.
# Backups are NOT considered working until a restore has actually succeeded
# into a clean environment.

set -eu

DEST="${1:-/backups}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
RUN_DIR="${DEST}/${STAMP}"
RETENTION_DAYS="${BACKUP_RETENTION_DAYS:-14}"

log() { echo "[backup] $(date -u +%H:%M:%S) $*"; }

mkdir -p "$RUN_DIR"

# ---------------------------------------------------------------------------
# PostgreSQL
# ---------------------------------------------------------------------------
# Custom format (-Fc): compressed, and restorable table-by-table with pg_restore.
log "dumping database ${PGDATABASE:-face_recognition}..."
if ! pg_dump --format=custom --compress=6 --no-owner --no-privileges \
        --file="${RUN_DIR}/database.dump"; then
    log "ERROR: pg_dump failed"
    rm -rf "$RUN_DIR"
    exit 1
fi

# ---------------------------------------------------------------------------
# File artifacts
# ---------------------------------------------------------------------------
# The gallery and the FAISS indexes are NOT reconstructible from the database
# alone: the images are the source material, and rebuilding indexes requires
# re-running inference over all of them.
if [ -d /data/storage ]; then
    log "archiving storage gallery..."
    tar -czf "${RUN_DIR}/storage.tar.gz" -C /data storage 2>/dev/null || \
        log "WARNING: storage archive incomplete"
fi

if [ -d /data/database ]; then
    log "archiving FAISS indexes and model artifacts..."
    tar -czf "${RUN_DIR}/artifacts.tar.gz" -C /data database 2>/dev/null || \
        log "WARNING: artifact archive incomplete"
fi

# ML registry artifacts + Parquet datasets (ml_artifacts_data). The database
# registry references every one of these files by sha256: restoring the
# database without them leaves each registered model/dataset row pointing at
# a file that no longer exists. Guarded so pre-existing deployments whose
# backup service predates the /data/ml mount keep working.
if [ -d /data/ml ]; then
    log "archiving ML registry artifacts and datasets..."
    tar -czf "${RUN_DIR}/ml_artifacts.tar.gz" -C /data ml 2>/dev/null || \
        log "WARNING: ml artifact archive incomplete"
fi

# ---------------------------------------------------------------------------
# Checksums and manifest
# ---------------------------------------------------------------------------
log "computing checksums..."
( cd "$RUN_DIR" && sha256sum ./* > SHA256SUMS 2>/dev/null ) || true

DB_SIZE="$(wc -c < "${RUN_DIR}/database.dump" 2>/dev/null || echo 0)"
cat > "${RUN_DIR}/manifest.txt" <<EOF
backup_utc=${STAMP}
database=${PGDATABASE:-face_recognition}
database_dump_bytes=${DB_SIZE}
pg_dump_format=custom
retention_days=${RETENTION_DAYS}
contents=database.dump storage.tar.gz artifacts.tar.gz ml_artifacts.tar.gz
restore=scripts/backup/restore.sh ${STAMP}
EOF

# A zero-byte dump means pg_dump "succeeded" while producing nothing.
if [ "$DB_SIZE" -lt 1024 ]; then
    log "ERROR: database dump is implausibly small (${DB_SIZE} bytes)"
    exit 1
fi

# ---------------------------------------------------------------------------
# Retention
# ---------------------------------------------------------------------------
log "pruning backups older than ${RETENTION_DAYS} days..."
find "$DEST" -maxdepth 1 -type d -name '20*Z' -mtime "+${RETENTION_DAYS}" \
    -exec rm -rf {} + 2>/dev/null || true

log "OK  ${RUN_DIR}  (database ${DB_SIZE} bytes)"

# ---------------------------------------------------------------------------
# Off-host copy
# ---------------------------------------------------------------------------
# A backup on the same host does not survive the failure it exists for.
if [ -n "${BACKUP_REMOTE_PATH:-}" ]; then
    log "copying to ${BACKUP_REMOTE_PATH}..."
    cp -r "$RUN_DIR" "$BACKUP_REMOTE_PATH/" || log "WARNING: off-host copy failed"
else
    log "NOTE: BACKUP_REMOTE_PATH unset — backups exist only on this host"
fi

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
# Restore: scripts/backup/restore.sh — and see Docs/11_BACKUP_AND_RESTORE.md.
# Backups are NOT considered working until a restore has actually succeeded
# into a clean environment.

set -eu

# Resolve the backup role's password HERE, at the point of use, not only in
# backup-loop.sh. deploy.sh's backup stage runs this script through
# `compose exec -T backup sh /scripts/backup.sh`, a fresh shell that does not
# inherit the loop's exported PGPASSWORD - so the scheduled backups succeeded
# while every on-demand one failed with "fe_sendauth: no password supplied".
if [ -z "${PGPASSWORD:-}" ] && [ -r /run/secrets/backup_db_password ]; then
    PGPASSWORD="$(cat /run/secrets/backup_db_password)"
    export PGPASSWORD
fi

DEST="${1:-/backups}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
RUN_DIR="${DEST}/${STAMP}"
RETENTION_DAYS="${BACKUP_RETENTION_DAYS:-14}"
case "$DEST" in /*) ;; *) echo 'Backup destination must be absolute' >&2; exit 2;; esac
test "$DEST" != / || exit 2
case "$RETENTION_DAYS" in ''|*[!0-9]*) exit 2;; esac
FINAL_DIR="$RUN_DIR"
RUN_DIR="${DEST}/.incoming-${STAMP}-$$"
STAGING_DIR="$RUN_DIR"
trap 'test ! -d "$STAGING_DIR" || rm -rf "$STAGING_DIR"' 0
umask 077

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
    tar -czf "${RUN_DIR}/storage.tar.gz" -C /data storage || exit 1
fi

if [ -d /data/database ]; then
    log "archiving FAISS indexes and model artifacts..."
    tar -czf "${RUN_DIR}/artifacts.tar.gz" -C /data database || exit 1
fi

# ML registry artifacts + Parquet datasets (ml_artifacts_data). The database
# registry references every one of these files by sha256: restoring the
# database without them leaves each registered model/dataset row pointing at
# a file that no longer exists. Guarded so pre-existing deployments whose
# backup service predates the /data/ml mount keep working.
if [ -d /data/ml ]; then
    log "archiving ML registry artifacts and datasets..."
    tar -czf "${RUN_DIR}/ml_artifacts.tar.gz" -C /data ml || exit 1
fi

# ---------------------------------------------------------------------------
# Checksums and manifest
# ---------------------------------------------------------------------------
log "computing checksums..."
( cd "$RUN_DIR" && find . -maxdepth 1 -type f ! -name SHA256SUMS -exec sha256sum {} \; > SHA256SUMS && sha256sum -c SHA256SUMS )

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
test ! -e "$FINAL_DIR" || exit 1
mv "$RUN_DIR" "$FINAL_DIR"
RUN_DIR="$FINAL_DIR"

# ---------------------------------------------------------------------------
# Retention
# ---------------------------------------------------------------------------
log "pruning backups older than ${RETENTION_DAYS} days..."
find "$DEST" -maxdepth 1 -type d -name '20*Z' -mtime "+${RETENTION_DAYS}" \
    -exec rm -rf {} +

log "OK  ${RUN_DIR}  (database ${DB_SIZE} bytes)"

# ---------------------------------------------------------------------------
# Off-host copy
# ---------------------------------------------------------------------------
# A backup on the same host does not survive the failure it exists for.
if [ -n "${BACKUP_REMOTE_PATH:-}" ]; then
    log "copying to ${BACKUP_REMOTE_PATH}..."
    cp -r "$RUN_DIR" "$BACKUP_REMOTE_PATH/" || exit 1
else
    log "NOTE: BACKUP_REMOTE_PATH unset — backups exist only on this host"
fi

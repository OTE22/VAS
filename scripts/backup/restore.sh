#!/bin/sh
#
# Restore a backup produced by backup.sh.
#
#   sh scripts/backup/restore.sh <timestamp> [--force]
#   sh scripts/backup/restore.sh 20260727T031500Z --force
#
# Refuses to touch a populated database without --force, because the common
# way to lose data during an incident is to restore over the wrong target.
#
# Verifies checksums BEFORE restoring: a corrupt archive discovered halfway
# through leaves the database in a worse state than the outage did.

set -eu

STAMP="${1:-}"
FORCE="${2:-}"
DEST="${BACKUP_DIR:-/backups}"

log() { echo "[restore] $(date -u +%H:%M:%S) $*"; }
die() { echo "[restore] ERROR: $*" >&2; exit 1; }

[ -n "$STAMP" ] || die "usage: restore.sh <timestamp> [--force]
available:
$(ls -1 "$DEST" 2>/dev/null | grep 'Z$' || echo '  (none)')"

RUN_DIR="${DEST}/${STAMP}"
[ -d "$RUN_DIR" ] || die "no backup at ${RUN_DIR}"
[ -f "${RUN_DIR}/database.dump" ] || die "no database.dump in ${RUN_DIR}"

# ---------------------------------------------------------------------------
# Integrity first
# ---------------------------------------------------------------------------
if [ -f "${RUN_DIR}/SHA256SUMS" ]; then
    log "verifying checksums..."
    ( cd "$RUN_DIR" && sha256sum -c SHA256SUMS ) \
        || die "checksum mismatch — this backup is corrupt, do not restore it"
    log "checksums OK"
else
    log "WARNING: no SHA256SUMS present; integrity is unverified"
fi

# ---------------------------------------------------------------------------
# Refuse to clobber a populated database by accident
# ---------------------------------------------------------------------------
EXISTING="$(psql -tAc "SELECT count(*) FROM information_schema.tables WHERE table_schema='public'" 2>/dev/null || echo 0)"
if [ "${EXISTING:-0}" -gt 0 ] && [ "$FORCE" != "--force" ]; then
    die "target database already has ${EXISTING} table(s).
Re-run with --force to overwrite, or point PGDATABASE at a scratch database
to rehearse the restore first (which is what the periodic restore test does)."
fi

# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------
log "restoring database into ${PGDATABASE:-face_recognition}..."
# --clean --if-exists drops existing objects first; without it a partial
# restore silently merges old and new rows.
pg_restore --clean --if-exists --no-owner --no-privileges \
    --dbname="${PGDATABASE:-face_recognition}" \
    "${RUN_DIR}/database.dump" 2>&1 | grep -vE "does not exist, skipping" || true

RESTORED="$(psql -tAc "SELECT count(*) FROM information_schema.tables WHERE table_schema='public'")"
[ "${RESTORED:-0}" -gt 0 ] || die "restore produced no tables"
log "restored ${RESTORED} tables"

# ---------------------------------------------------------------------------
# Files
# ---------------------------------------------------------------------------
if [ -f "${RUN_DIR}/storage.tar.gz" ] && [ -d /data ]; then
    log "restoring storage gallery..."
    tar -xzf "${RUN_DIR}/storage.tar.gz" -C /data || log "WARNING: storage restore incomplete"
fi

if [ -f "${RUN_DIR}/artifacts.tar.gz" ] && [ -d /data ]; then
    log "restoring FAISS indexes and model artifacts..."
    tar -xzf "${RUN_DIR}/artifacts.tar.gz" -C /data || log "WARNING: artifact restore incomplete"
fi

cat <<EOF

[restore] Database restore complete from ${STAMP}.

Reconcile before declaring success — the database and the file archives are
captured seconds apart, so rows can reference images that the gallery archive
missed, and vice versa:

  1. Restart the API so it reloads indexes and model artifacts.
  2. Check for identities whose image files are absent.
  3. Rebuild the FAISS index if artifacts.tar.gz was missing or stale.
  4. Confirm the schema revision matches the application:
       python -m backend.utils.migrations --verify

EOF

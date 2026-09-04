#!/bin/sh
#
# Entrypoint for the `backup` service: take a backup, sleep, repeat.
#
# A plain loop rather than cron, so failures land in `docker logs backup` and
# the container's restart policy applies. BACKUP_INTERVAL_SECONDS defaults to
# 24h; set it lower to tighten the RPO.

set -eu

INTERVAL="${BACKUP_INTERVAL_SECONDS:-86400}"

echo "[backup-loop] starting; interval=${INTERVAL}s retention=${BACKUP_RETENTION_DAYS:-14}d"

# Wait for Postgres to accept connections rather than failing the first run.
until pg_isready -q 2>/dev/null; do
    echo "[backup-loop] waiting for postgres..."
    sleep 5
done

while true; do
    if sh /scripts/backup.sh /backups; then
        echo "[backup-loop] backup succeeded"
    else
        # Non-fatal: keep the loop alive so a transient failure does not stop
        # all future backups. The BackupFailed alert fires on the age of the
        # newest backup, so a persistent failure still pages.
        echo "[backup-loop] BACKUP FAILED — see output above" >&2
    fi
    sleep "$INTERVAL"
done

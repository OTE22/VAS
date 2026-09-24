# Backup and recovery

[All guides](README.md) · [Offline move](offline-deployment.md)

Reviewed against the scripts on 2026-09-24. No backup or restore was executed
for this documentation change.

## Take and export a VAS backup

From the repository root, with the existing production database and backup
service running:

```bash
sudo bash deploy.sh backup
```

The wrapper creates a new timestamped directory in the backup service's
`/backups` volume and verifies its checksums. The backup script includes a
PostgreSQL custom-format dump, storage gallery, database/index artifacts and,
when mounted, ML registry/dataset artifacts. It also prunes backups older than
its configured retention period.

Use the timestamp printed by the command, replacing the example below. Copy to
an already mounted, restricted external backup directory:

```bash
sudo docker cp face_detector_prod-backup-1:/backups/20260924T120000Z /media/backup/
cd /media/backup/20260924T120000Z
sha256sum -c SHA256SUMS
```

Expected: every listed artifact reports OK. Protect this data and verify you
copied the actual new timestamp, not the example. Checksums establish integrity,
not that restoration has been rehearsed successfully.

The dump and file archives are captured sequentially. Quiesce writers for a
consistent final snapshot. These archives do **not** preserve every dependency:
retain configuration, secrets, CA/private keys, models, map data, Docker images,
other named volumes and the separate VMS/chatbot data through their own backup
procedures. A backup on this same disk does not protect against disk loss.

## Recovery limitations in the current scripts

Read [restore.sh](../scripts/backup/restore.sh) and the
[deployment wrapper](../scripts/deploy/stage-upgrade.sh) before recovery.
The wrapper asks for a typed confirmation, and the restore script requires
`--force` for an already populated database. Restoration replaces data; it is
not a step in a normal same-server network move.

The current implementation has material limitations:

- `restore.sh` extracts storage and database archives but does not extract
  `ml_artifacts.tar.gz`, even though backup.sh creates it.
- Production mounts `/data/storage`, `/data/database` and `/data/ml` read-only
  in the backup service. File restoration needs a controlled recovery container
  or maintenance mount configuration with write access.
- The backup role is intended for reading. Recovery needs an appropriately
  privileged database connection and subsequent role/grant reconciliation.
- The current `pg_restore` pipeline can mask failures, and the final table-count
  check alone does not prove a complete restore.

Consequently, do not treat `deploy.sh restore` printing completion as proof of
recovery. Rehearse in an isolated target, restore every required archive into its
correct volume with correct ownership, verify schema/migrations, row counts,
images, ML artifact hashes and application behavior, then plan production
recovery. Keep the original volumes and another backup until validation passes.
This guide intentionally does not prescribe a blind production overwrite.

Sources: [backup.sh](../scripts/backup/backup.sh),
[production mounts](../docker/docker-compose.prod.yml).

# Background job reliability changes

## Implemented behavior

- Detection batches are parked atomically under `STORAGE_DIR/.detection-queue` before acceptance. The existing detection UUID deduplicates replay, including an ambiguous commit, without a schema migration. Both timer and batch-size flushing remain. Transient failures retain the frame; invalid database evidence is quarantined as `.failed.json` with an error sidecar for operator investigation.
- Camera-embedding reconciliation and snapshot cleanup protect pending detection evidence. Gallery protections and existing shared-file reference checks remain in force. Pending files are not an automatic orphan-deletion target. A working storage disk is required to accept durable work.
- Cleanup failures propagate to supervision and are recorded as failed task outcomes. Partial file cleanup retains database references so a later pass can retry. Task-history retention excludes scheduled, queued and running work.
- Watchlist, date-based live-alert and pending-enrollment expiry sweeps run every five minutes, starting one minute after API startup. Expiration updates lifecycle state; it does not delete people. Pending enrollment requests keep their existing best-effort sweep, with row locking to avoid overlapping claims.
- Face-tracker cleanup uses `FACE_TRACKING_CLEANUP_INTERVAL`. Queue consumers have individual task-liveness probes. Cache writes and the Redis event listener are supervised; optional cache failures still allow request fallbacks. The unused cache-warming placeholder is no longer launched.
- First-run health deadlines include deliberate startup delays. Missing expected services and stopped consumers are visible. Batch-writer health details include pending and quarantined frame counts. Liveness probes establish that consumer tasks exist; they do not substitute for an end-to-end camera test.

## Log policy

| Category | Default retention | Protection |
|---|---|---|
| Numbered application rotations, including older numbering beyond the current backup count | Existing `LOGS_LIFE_TIME_HOURS`, 48 hours | Active application file and open files excluded |
| Closed legacy root `access.log` / `error.log` and numbered rotations | `LEGACY_LOG_RETENTION_DAYS`, 14 days | Requires open-file inspection; fails closed when that inspection is unavailable |
| Files under `logs/smoke` and `logs/regression` | `DIAGNOSTIC_LOG_RETENTION_DAYS`, 30 days | Recent, open and symlinked files excluded |
| `.log` files under `logs/audit` | Same diagnostic retention | Audit datasets, scripts, models and other reports retained |

The two new settings are environment configuration and require restart; zero disables that category. Log cleanup runs approximately every six hours. `log_cleanup_manager.preview()` returns candidate counts/bytes without deleting anything. Results now use `deleted_files`, not `deleted_lines`. Docker stdout rotation remains separate.

## Backups

Development Compose now starts a daily backup service with a separate destination volume and read-only source mounts. The API mounts that destination read-only for backup statistics. Production retains its existing backup service and gains a health check. Both use the shared backup script, which publishes a completed directory only after successful dump/archive/checksum validation, reports incomplete runs as failures and cleans its temporary staging directory. Backup retention is 14 days by default.

The development destination is `face_detector_dev_backup_data`. The first completed run is `20260913T120328Z`. SHA-256 checks passed for database, storage, model/index and ML archives. The database dump was restored using `pg_restore --exit-on-error` in a temporary container with no network or published ports: 45 identities and 147 embeddings were recovered. File archives were checksum-verified; a full application failover was not performed. Backups remain on this host unless an off-host destination is configured.

## Validation and rollout state (13 September 2026)

- 74 focused tests passed together: background failure injection, supervisor, shutdown lifecycle, detection storage/evidence, known-face lifecycle and enrollment review.
- An expanded background-fix suite subsequently passed all 16 tests, adding real PostgreSQL UUID replay, expiration boundaries and optional-cache failure behavior.
- Python compilation and whitespace checks passed locally after the final edits.
- Previewed and removed exactly two expired legacy logs: 14,736,861 bytes (14.05 MiB). Cleanup task history records success with no failed files. Other diagnostic artifacts were within their retention period.
- The backup service is running. The existing API has **not** been recreated with the final changes.

Automatic approval review rejected the final container regression command because its usage allowance was exhausted. The final Redis listener/size-triggered-flush changes therefore still need the container test pass. Remaining work when that access is restored:

1. Run `tests/test_background_job_fixes.py` and the focused regression suite again for the final code.
2. Recreate only the API service with the development Compose file so the new jobs and backup statistics mount take effect.
3. Verify detailed health, the first expiration sweeps, ML worker heartbeat, backup health and representative dashboard/known/unknown/alert routes. Check that no service is unexpectedly degraded.
4. Remove the temporary restore-verification container `face_background_restore_check` (ID `2c32919c4c98141513090ad1ea5371311831e01684a98ce4f2544a57d48814d4`). Its database is temporary RAM storage; the backup volume is mounted read-only. Do not remove the live database or backup volume.

Automatic ML drift/retraining remain deliberately disabled. Historical invalid training inputs were not silently rerun or converted to successful history records.

### Rollout completed during the camera-event work

Tool access resumed. The final code passed the expanded 80-test regression suite, the `face_recognition` API service was recreated, and live health showed the supervised workers and expiration sweeps with no errors. Known Faces and Unknown Persons API checks succeeded, as did the live WebSocket check. The exact temporary restore-verification container listed above was removed; the backup service and backup volume were retained. The earlier pending-rollout notes describe the interrupted checkpoint, not the current deployment state.

# Operational integrity fixes — deployed and verified

Date: 2026-09-15. Implemented in `/tmp/vas-integrity-fix` on branch `fix/alert-integrity-validation`, tested against an isolated copy of production schema, then copied into the main working tree. After explicit deployment approval, the production migration and VAS API/ML container update completed on 2026-09-15. VMS and chatbot containers were unchanged.

## Result

**59 tests passed, zero failures and zero expected failures.** All six former expected-failure cases now pass. Deprecation warnings from existing dependencies remain.

Coverage includes real PostgreSQL constraints, alert create/update HTTP handlers, camera rename, merge and unmerge with different/shared photos, pending suggestion invalidation, concurrent image ownership changes, deletion/retention, replay and intelligence filters, populated upgrade refusal/rollback, full fresh migration and application database startup.

HTTP tests supply a synthetic authenticated admin via a test dependency override and stub the separate audit writer; they do not test password login. Merge tests bypass face similarity scoring and use synthetic missing photo paths, but run real database merge/unmerge/consolidation logic. No production rows were copied. No model inference or browser/load test was performed for this change.

## Changes

- `LiveAlertService.validate_scope` checks camera existence and weekday values on create/update. Existing `None`/empty-list unrestricted semantics and response fields are retained. Invalid updates return HTTP 400 instead of becoming server errors.
- Reversible Alembic revision `fee5f6a7b8c9`, parent `fdd4e5f6a7b8`, adds a weekday CHECK and trigger-maintained `live_alert_pipeline_links`. The existing JSON is still the application interface; FK-backed links prevent nonexistent/deleted camera references. Camera rename updates the JSON restrictions before removing the old camera.
- Image/embedding ownership checks run at transaction end, allowing valid merge/unmerge intermediate states. Image locking serializes competing ownership changes. Image deletion keeps its SET NULL behavior. Multiple embeddings per image remain permitted; no new cardinality policy was assumed.
- Trigger-maintained `pending_merge_members` validates pending suggestions against distinct, actionable identities. Deletion/retirement invalidates pending suggestions and removes current links while preserving historical JSON. Existing approval validation remains in place.
- Chatbot code, auth/SSO, public response fields, audit snapshots, and runtime data remain unchanged. `backend/routes/users.py` changed only inside the camera-rename handler.

The two link tables are derived internal relations maintained by database triggers. Application code must continue writing the original alert/suggestion fields, not manually editing the link tables. They have reverse-reference indexes and database-owned cascade behavior.

## Migration safety

Upgrade refuses inconsistent existing image ownership, invalid weekdays, missing camera references, or invalid pending memberships. It does not silently delete/repair offending records. The migration uses PostgreSQL transactional DDL and a five-second lock timeout: contention stops the upgrade rather than waiting indefinitely. Backfill updates assign the same existing JSON values solely to populate/validate derived links.

The populated test downgraded, inserted an intentionally invalid synthetic embedding, and verified that upgrade failed with the old revision and original invalid row intact. After removing only that test fixture, upgrade succeeded and preserved valid alert/suggestion/photo data. Downgrade removes the new guards/derived tables, retaining existing business columns and rows. Invalidated suggestion statuses reflect lifecycle changes and are not automatically reversed by downgrade.

A historical database that fails preflight needs case-by-case review. Do not retry by deleting rows or disabling constraints.

## Reproduce tests

```bash
sudo bash scripts/test_database_integrity.sh
```

Requires the local development/PostgreSQL images and deployed VAS PostgreSQL container. The script exports schema only, restores to a disposable isolated server, stamps that copy's starting revision, upgrades the copy, runs tests, and rehearses downgrade/re-upgrade. It removes the test container and prints a temporary evidence directory. **It never upgrades production.**

Final isolated evidence directory: `/tmp/vas-normalization.Flsgtz` (root-owned). Tests are in `tests/test_normalization_integrity.py`; the runner also selects existing background-job, expiry, intelligence and fresh-migration tests.

## Production rollout completed

Deployed on 2026-09-15. Live database revision is **`fee5f6a7b8c9`**. Updated the three production Compose revision defaults and the explicit `docker/.env` pin to match. Built API, ML worker and migration images; stopped VAS API/ML writers, captured a cutover backup, applied the established migration job successfully, restarted the updated writers and reloaded Nginx.

Verification:

- API, ML worker and Nginx healthy; persisted ML heartbeat healthy.
- Trusted HTTPS `/health/live` and `/health/ready` returned 200, as did intelligence, alert and log JavaScript assets.
- Protected admin pages returned 302 and the protected monitoring API returned 401 without credentials, as expected. Production password login was not tested.
- Both derived tables are accessible to the actual application database role; the weekday constraint is validated and all seven new triggers are enabled.
- Image/embedding ownership mismatch count is zero. The actual API database role successfully read the new revision and evaluated the weekday guard.
- VMS, VMS-db, vas-assistant and vas-assistant-gate container IDs and images matched their pre-deployment snapshots.

Backups and protected evidence: `/var/backups/vas-integrity-20260915/`. The full pre-deployment `database.dump` was successfully restored in a disposable, network-isolated PostgreSQL container, which was then removed. `database-at-cutover.dump` captures the state after stopping writers; its archive contents were verified. Build, migration, start and verification logs are retained alongside the backups.

Rollback image tags: `face_detector_prod-face_recognition:before-integrity-20260915`, `face_detector_prod-ml_worker:before-integrity-20260915`, and `face_detector_prod-migrate:before-integrity-20260915`.

If reversing this successful rollout, stop VAS writers, use the new migration image to downgrade to `fdd4e5f6a7b8`, restore the old images and revision pins, then restart and verify readiness. Do not restore an older database dump over newer production writes without reviewing the data-loss implications.

Nginx configuration validation succeeded but reported that `worker_connections=8192` exceeds the process open-file limit of 1024. This deployment did not change Nginx resource limits; capacity tuning remains separate from the database fixes.

No Git commit or push was performed.

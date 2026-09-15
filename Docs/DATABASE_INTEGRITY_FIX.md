# Operational integrity fixes — tested, not deployed

Date: 2026-09-15. Implemented in `/tmp/vas-integrity-fix` on branch `fix/alert-integrity-validation`, tested against an isolated copy of production schema, then copied into the main working tree. No production schema/data/container changes were made in this fix task.

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

## Production rollout still pending

The live database remains on `fdd4e5f6a7b8`. These source changes introduce a newer required revision; do not restart a newly built API image against the old revision. Use the established production migration gate.

For a controlled rollout: retain rollback images, take/verify a database backup, rebuild API/ML worker/migration images using the active GPU Compose overrides, stop application writers for the migration window, run the migration job, then start the updated writers and verify readiness, alerts and tasks. The migration-only image remains CPU-based. Keep databases, persistent volumes, VMS and chatbot containers intact.

If migration fails, PostgreSQL rolls its transaction back; restore the old application images. If reversing a successful rollout, stop writers, downgrade to `fdd4e5f6a7b8`, then restore the old images. Recheck the live revision before any subsequent deployment.

No production deployment or Git commit/push was performed in this fix task.

# Separate person history and image retention

## Behavior

`PRESERVE_PERSON_HISTORY=true` is the default for new code and Compose deployments.
It prevents scheduled age-based deletion of detections (and their cascading face
rows), and disables scheduled trimming of recorded person embeddings. Identity
and appearance history remain until an explicit administrative operation removes
them. No schema migration is required.

This does **not** disable all database deletion. Search-query logs, task history,
audit logs, temporary artifacts and eligible ML housekeeping retain their own
cleanup behavior. Generated reports continue to use `DATA_RETENTION_DAYS`.
Crash recovery can still remove camera embeddings and empty identities belonging
to failed frames with no recorded appearances or face history. Recorded history
protects legacy embeddings whose detection link has already been lost.

`SNAPSHOT_RETENTION_DAYS` controls routine camera-image expiry (default 90 days).
The identity cleanup job retires expired face-image references, appearance
snapshots and eligible representative portraits, without deleting the person
records. File deletion is reference-aware: enrollment photos, pending writes,
recent sightings, retained portraits and alert/merge/search evidence can protect
shared files beyond this age. A representative portrait's existing eligibility
is based on the identity's last-seen time. This is not a strict 90-day TTL for
every image file. Older sightings do not acquire newer timestamps during cleanup.

Historical views use “Image unavailable — history retained” when a stored image
is unavailable. In current image-search results, an available uploaded reference
is shown instead, labelled **Uploaded search image**. This applies to Quick
Search, Advanced Search (including its match-details panel), and the Identity
Intelligence and Security Intelligence photo pickers. Stored photos are preferred;
missing or failed images fall back to the reference, then to the unavailable state.

The reference is a browser-only display fallback, never saved as a portrait or
substituted for historical sighting evidence, and does not change exported API
results. References are released when results are cleared/replaced, pickers close,
or the page unloads. A failed Advanced Search rerun preserves the previous
successful search's reference. Batch matches use their own submitted image; if
decode failures omit uploads, unique filenames can remap the reference. Duplicate
filenames in such a shortened response are ambiguous, so those matches retain the
unavailable state rather than show a potentially incorrect photo. Image expiry
does not recover removed vectors or make a face match certain.

## Search

Quick and Advanced Search include retained inactive known and unknown identities.
Merged identities are excluded. A search does not reactivate the identity or
change live camera-recognition eligibility. Existing permissions remain in force.
Historical retrieval selects distinct people so one person's many retained
vectors cannot occupy every result slot. The FAISS configuration uses the
existing authoritative PostgreSQL vectors for historical searches, since its
live index omits inactive identities.

Existing search-date/filter semantics are unchanged. This change does not imply
that every screen lists inactive identities or that every captured person has a
usable matching vector.

## Configuration and deployment

1. Rebuild and deploy the API containing these changes and the updated frontend.
   Source edits alone do not change currently running containers.
2. In Settings, verify **Preserve person history until explicit deletion** is on
   and inspect its effective value. Database-saved settings override startup
   defaults. Production Compose also accepts `PRESERVE_PERSON_HISTORY=true`.
3. Verify **Snapshot retention** separately. It runs in Identity Retention.
4. Use the data-retention dry run to confirm detection deletion candidates are
   zero while preservation is on. This preview does not preview the separate
   identity-image cleanup job. An API dry run creates a job/audit record but
   deletes no retained data.
5. Monitor storage growth: history and matching vectors now accumulate. Backups
   have separate expiry; preservation is not a backup or an undo mechanism.

Switching preservation off resumes the existing configured detection expiry and
embedding cleanup. Review the deletion preview first. Explicit administrative
operations (such as delete/unmerge) retain their existing behavior.

## Validation performed

Tests use a separate disposable PostgreSQL container, synthetic vectors/records,
and temporary files, with no production volumes or network attached.

- 108 focused backend/storage/configuration tests passed, including the 12 new
  retention cases: image expiry with history preserved, inactive matching,
  continued log cleanup, enrollment/evidence/shared-file/pending-write protection,
  missing files, retry after unlink failure, distinct-person ranking, legacy
  orphan preservation and failed-frame recovery, and explicit legacy mode.
- 7 Quick Search tests and 11 Advanced Search filter tests passed separately.
- 55 guided-installer tests passed on the host.
- All six changed JavaScript files passed syntax compilation; a full browser
  workflow was not exercised in this database-only test environment.
- Three existing HTTP search tests require an API/model instance and were not
  run successfully by the database-only runner. This is not live deployment or
  load-test acceptance.
- A broader exploratory run found existing environment-reader policy violations
  in unrelated ML/deployment scripts; these were not changed for this task.

Production settings, data, images and running services were not modified during
implementation or testing.

## Search-reference display verification

The display change is frontend-only; no database migration or cleanup is required.
DOM regression tests cover existing/absent/failed images, batch indexing and
omissions, failed reruns, cancellation, selector cleanup, and untrusted URLs.
Firefox checks use a loopback-only staged frontend with synthetic PNG references
and mocked search responses to verify actual decoding, HTTP 404 fallbacks, CSP,
and visible labels at desktop and mobile widths. They do not validate face-match
accuracy or substitute for backend integration/load testing.

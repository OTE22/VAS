# Image-search match integrity

Main image search, batch search, Quick Search and the intelligence photo pickers use the same evidence-aware matcher.

## What a result means

- `similarity` belongs to `matched_embedding_id`. The image is that vector's owned enrollment image or its uniquely linked camera Face, never the identity group's representative portrait.
- Both Known and Unknown image-search results must meet the current `SIMILARITY_THRESHOLD`. Quick Search no longer uses the lower live Unknown grouping threshold.
- Only vectors from the current recognition model are compared. Inactive person history remains searchable; merged-away identities do not produce duplicate results.
- Camera and date filters qualify the observation that produced the result. Filters apply before ranking and limiting distinct identities. Enrollment-only images have no camera sighting and do not satisfy camera/date filters.
- A Known name is shown only when both the uploaded query and the matching historical observation meet the threshold against the same completed, identity-owned enrollment reference. Enrichment and historical group membership do not establish an approved reference.
- Otherwise a Known-associated observation is labeled **Unverified historical match**, without the name. It does not trigger a Known-person watchlist alert. Unknown watchlist matching additionally requires direct agreement with the stable current-model group reference.
- These checks establish reference consistency, not independently verified human identity. A similarity score is not a probability, and no threshold guarantees zero false matches. Approved enrollment references must themselves be correct.

## All matching recorded appearances in Advanced Search

A single-image search retains the original query vectors and selected filters in the existing Redis cache for 30 minutes. The opaque context ID is bound to the requesting administrator. Vectors are never returned to the browser. An expired context, a different owner, or changed recognition model/threshold requires a new search; it never falls back to an identity-group reference.

**View matching appearances across cameras**, and single-image result-card clicks, open a chronological paginated view. Each sighting is compared directly with the original uploaded face across the selected Known/Unknown scope. Identity Groups Per Face controls summary cards only; it does not truncate the appearance query. Camera, dates and exclusions remain fixed across pages. Page size is bounded to 50 in the UI (API maximum 100), and the initial maximum appearance ID excludes newly arriving events until search is rerun. The journey covers the current page. Rows can still change through an administrator's normal edits/deletions; an invalid cursor requests a restart.

The query requires a uniquely linked current-model observation vector. Missing vectors, ambiguous multiple links and incompatible model versions cannot establish a match. An old group association does not establish identity. A missing/expired snapshot does not exclude an otherwise usable embedding match, but the viewer never substitutes another person's/group's portrait for the event. When Face has been removed but a unique appearance-to-vector link survives, the appearance's own snapshot can be displayed. This does not restore a missing original image or guarantee recognition recall.

Dates in the browser refer to local calendar days: Date To includes the last microsecond of the selected day. The API normalizes explicit offsets to UTC; date-only API bounds use UTC with an inclusive end of day. Reversed ranges are rejected. Advanced Search and Quick Search share parsing.

Batch image search retains its existing summary workflow and does not provide this per-query appearance viewer. Run a single-image search to inspect all recorded appearance matches for a selected face. Paging does not create new watchlist alerts or modify identities. The existing search audit remains unchanged.

## Other identity-detail entry points

The client passes `search_embedding_id` to identity details and profile links. The backend checks ownership and model compatibility; stale or mismatched IDs return 404. A query's unverified status is preserved as a downgrade with `search_unverified=true`.

The header retains the exact matched photo. Sighting comparisons use the matched vector and the image-search threshold, without chaining through intermediate observations. Other historical associations stay stored and are marked for review; they are not silently included in the journey. Group-wide analysis and alert actions are withheld in photo-search contexts where the group's reference is unverified. A selected Unknown sighting can still be reviewed and promoted through the existing selected-sighting workflow.

The main page also distinguishes multiple query faces that hit the same identity: the selected vector ID, not the identity ID alone, chooses the match's score and photo.

## Missing or ambiguous evidence

If the retained schema cannot unambiguously associate a vector with a Face (for example multiple faces/vectors in the same detection), search does not guess a photo. A deleted camera image does not remove the search embedding. The existing browser-only uploaded-photo fallback remains explicitly labeled **Uploaded search image** and is not camera evidence.

Intelligence photo pickers show an unverified group's matching observation for review but do not select the whole group as a verified person.

## Configuration and storage

Exact database ranking replaces the lossy identity-only index result in image-search workflows. Existing FAISS/pgvector live-recognition behavior is unchanged. `SEARCH_RETRIEVAL_FLOOR`, `SEARCH_CANDIDATE_MULTIPLIER` and `SEARCH_FILTERED_CANDIDATE_MULTIPLIER` remain accepted for old configuration compatibility but are labeled legacy and are unused by image search. `SEARCH_DEFAULT_TOP_K` and `SEARCH_MAX_TOP_K` still control result depth.

No database schema migration, reclassification, deletion or image/embedding rewriting is required. Normal search audit logging continues. Deployment does not repair historical group membership automatically.

## Verification

`tests/test_search_filters.py` exercises exact photos, incorrect Known associations, reference ownership/status/model checks, sighting filters before ranking, deduplication, inactive history, ambiguity, stale/cross-identity context, single/batch serialization and HTTP response-model preservation. Quick Search and frontend regressions cover the same contract, including multi-face clicks and unverified picker selections.

Integration fixtures use a disposable PostgreSQL instance named `retention-test-db`, database `retention_test`, isolated from production. Tests never seed the application database. The candidate was also checked with read-only transactions against the previously investigated historical cases and a synthetic Firefox desktop/mobile session. Two read-only searches measured roughly 65–81 ms; this is a spot check, not a throughput or recognition-accuracy benchmark.


## New camera captures: retain observation evidence

For newly processed, accepted camera faces, a valid finite, nonzero embedding of the configured dimension is retained even below the reference-quality threshold. This changes storage admission only: camera face acceptance, matching thresholds, and enrollment quality checks still apply.

Known captures remain `known_observation`, without a gallery-image link. Low-quality or unscored Unknown captures are marked `unknown_observation` in the existing type column; they remain available to historical image search but are excluded from stable live-recognition anchors, automatic Unknown reference verification and clustering reference pools. An existing trusted Unknown anchor is not replaced by a later observation.

The processing path emits a required-embedding marker in the durable queue payload. The shared writer checks the exact vector's owner, camera, model metadata and numeric validity before committing the sighting. It locks identities in deterministic order before Face insertion so concurrent sightings do not deadlock by upgrading foreign-key locks. A failed required link rolls back the detection, faces, appearances and alerts together; the existing retry/quarantine mechanism reports failures. Explicit enrichment reuses its new vector instead of creating two ambiguous vectors for one observation.

This is a code-only forward change. It adds no tables or columns, does not backfill old records, and accepts legacy queued payloads without pretending that their historical links were verified. It does not relax a search similarity threshold or guarantee that every retained sighting appears for every query. Rejected/unusable faces remain unassigned evidence. Ambiguous legacy links and multiple indistinguishable faces under one identity/detection remain unverified.

With person-history preservation enabled, retaining more observations increases database storage; index synchronization and database capacity still require monitoring. Photo retention is independent of embedding retention.

`tests/test_future_capture_evidence.py` covers low-quality Known and Unknown captures, reference isolation on both backends, invalid-vector rejection, strict evidence rollback, duplicate replay, concurrent captures, and ingest-to-search provenance with synthetic fixtures. No production data is required.

## Camera snapshots after the photo-cap correction

Each camera crop accepted for saving keeps its own event snapshot, independent of the legacy `MAX_PHOTOS_PER_PERSON` value. `SAVE_IMAGES` and `SAVE_UNKNOWN_FACES` remain respected. The obsolete cap/count/alert-exception branch has been removed; the setting remains accepted for compatibility and is clearly labeled unused. Enrollment gallery limits, recognition thresholds, detection acceptance and reference learning are unchanged.

`SNAPSHOT_RETENTION_DAYS` continues to expire camera photos independently from preserved person history. Saving more snapshots increases disk use until retention expires them. This forward change does not reconstruct older missing images and requires no schema migration or database repair.

Validation uses `tests/test_search_appearances.py` with disposable PostgreSQL and synthetic vectors, the existing search/ingestion regressions, and `tests/admin_search_ui.test.cjs` for local-date boundaries, pagination, expired contexts and stale-request handling.


## Review one recorded association

An administrator can open **Unknown Faces → Identity Details → Other recorded associations — review required → Review this sighting**. The dialog compares the exact selected reference photo and the individual recorded sighting, with camera names and times. Both saved photos must load before a decision is enabled.

- **Confirm same person** adds that sighting to the selected reference's journey with an **Operator confirmed** label and reviewer/time. The decision persists across refreshes and sessions.
- **Different person** excludes that sighting from this reference's journey, even if automatic similarity would have included it. The recorded sighting, photo and embedding remain stored and follow their existing retention settings.
- **Not sure — close** saves nothing.
- **Review / undo decision → Undo decision** clears the current manual override and restores the automatic result. Previous decisions remain in the journal. Undo remains available if the photos later expire.

Included sightings also offer review/undo in the selected-photo panel and **Browse all photos**. The standalone Identity profile uses the same review controls when a reference sighting is available. Non-administrators can see the result when authorized to view that identity, but cannot save decisions.

A review records a direct association between two existing sightings in the same identity group. It works in either direction; A–B confirmation does not confirm A–C or B–C. It does not reassign every sighting, merge groups, enroll a recognition reference, change similarity thresholds or expand the original uploaded-image query in Advanced Search. Human decisions are distinct from automatic matches. **Promote to Known** remains the separate workflow that moves only the selected sighting and enrolls its exact approved photo for subsequent recognition.

The server issues an administrator-bound preview valid for ten minutes and checks the exact sighting ownership, photo fingerprints and last saved decision again inside the identity-mutation lock before committing. A changed photo, moved sighting, expired preview or competing review requires reopening the dialog. Reassignment or replacement of a reviewed sighting invalidates its previous journey override; image expiry alone does not erase the decision. If a response fails after saving, close and reopen to inspect the committed state before retrying.

Decisions use the existing `identity_audit_log` with action `sighting_review`; no schema migration is required. These records are association evidence and are exempt from routine audit-log expiry. Other operational logs still expire. No historical approvals are seeded by deployment. The API and frontend must be deployed together; older APIs do not advertise review capability, so the frontend hides the new write controls.

`tests/test_sighting_review.py` covers durable confirmation, rejection, undo, exact-pair isolation, permissions, stale previews and retention using a disposable PostgreSQL database. `tests/sighting_review_ui.test.cjs` exercises photo-load gating, exact-preview submission, errors and dialog races with synthetic data.

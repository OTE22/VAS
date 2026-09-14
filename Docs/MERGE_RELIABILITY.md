# Merge integrity and scoring changes

Implemented 2026-09-13 for the Unknown Persons card merge workflow.

## Database ownership and failure handling

- Pair and multiple merges acquire an exclusive transaction advisory lock before
  locking and refreshing their identity rows in UUID order. Detection persistence
  uses the shared form of the same lock, so detections may proceed concurrently
  while a merge waits. A merge temporarily pauses persistence; queued work remains
  replayable. Unmerge uses the exclusive lock too.
- Queued detections follow merged_into_id chains, with missing-target and cycle
  checks, before inserting faces or appearances. Their owned embedding rows follow
  the survivor. Camera IDs, event IDs, capture timestamps, and snapshots stay with
  the original occurrence. The queue payload itself is never rewritten in memory.
- Pair and multiple merge audit writes join the merge transaction. Audit failures
  propagate to rollback instead of silently rolling back inside the logger.
  Staged gallery cleanup is disarmed immediately after a successful commit.
- Self-merge is checked on parsed UUIDs at the route and service boundaries.
- Multiple merges into an already-known survivor also relabel absorbed unknown
  embeddings as known.

## Algorithm

- Suggestion approval assesses every original identity pair before sequential
  re-parenting can change the sample population. Explicit risk overrides remain
  available and audited; scores are recalculated on the server.
- Auto-selection prioritizes an active known identity before the existing
  sighting/diversity/age score. UUID ordering breaks ties deterministically.
  An explicit multiple-merge target remains an explicit operator choice.
- Compatibility uses up to eight distinct views per verified model, selected
  from at most 128 candidates balanced by camera/model and ranked by current
  measured quality before recency. Near-identical frames do not fill the sample.
  Missing model provenance is insufficient evidence, not a shared model bucket.
- Pipeline-aware suggestions compare only verified matching model versions and
  use the current quality scorer. Legacy records remain available to manual
  review, including the existing insufficient-evidence confirmation.
- Preview and execution measure the actual candidate snapshot. Stored quality
  is reused only for that exact image and current scorer; otherwise the image
  is assessed. A known person's chosen representative is preserved.

## Browser behavior

Committed merges emit pipeline-scoped notifications. Other Unknown Persons
pages reload authoritative data and ignore delayed events for merged-away IDs.
Older HTTP requests cannot overwrite the newer merge refresh. A current API
response can restore a person after an administrator reverses a merge.
The dialog states that merging affects the identity across all cameras.

## Validation

`tests/test_merge_reliability.py` uses rollback-only database fixtures and mocked
failure paths. It covers delayed occurrences through merge chains, replay,
self-merge, known survivor selection and labels, representative sampling,
missing models, whole-group suggestion refusal, exact snapshot selection,
audit failure, and shared-reader/exclusive-writer coordination.

Existing detection evidence, camera history, known-person lifecycle and enrollment
checks were also run. `scripts/dev/face_cards_ui_test.js` checks both pages and
merge-notification/stale-event behavior with mocked APIs; it performs no API writes.

Similarity remains a score, not a calibrated probability. The existing warning
threshold is unchanged. Camera-specific accuracy calibration requires labelled
examples; synthetic regression tests do not establish recognition accuracy.
These changes do not attempt to infer or rewrite historical bad merges.

Final verification: 10 merge regression tests and 42 existing detection,
camera-history, lifecycle and enrollment tests passed. Both browser suites passed.
The API container was refreshed with dependencies untouched. After startup, API
health was good; 53 HTTP camera events matched their database records, 72 initial
WebSocket events retained individual event metadata, and Known Faces returned
HTTP 200. Other containers remained healthy. The first live probe timed out
during startup; the retry after readiness passed.

# Selected-sighting promotion and camera map labels

Promotion from Unknown Faces now moves one operator-selected sighting into a new Known profile. Other sightings, labels, photos and vectors remain in their Unknown group. The new profile receives a primary gallery copy of the exact previewed photo and a current-model image-backed recognition reference. Only the unambiguously linked Face, appearance and camera observation embeddings change ownership. No records or source photos are deleted. If the source has no remaining appearances or Faces, it is kept as an inactive Unknown identity.

## Operator workflow

1. Select a camera card or a sighting in Identity Details.
2. Choose **Promote to Known**.
3. Check the photo, camera, time and sighting number in the preview.
4. Enter **Display Name** and optionally **Person Code**.
5. Click **PROMOTE**. Review any existing-person warning before confirming a separate profile.
6. Open Known Faces to verify the new profile. Other sightings remain in Unknown Faces.

Promotion candidates open existing profiles for review; this dialog does not merge the Unknown group. The separate Merge workflow still applies to entire identities. Search results without a persisted sighting selection lead through Identity Details.

## API contract

`GET /api/admin/unknown/{id}/match-candidates?appearance_id=<id>` is read-only and returns `selection.appearance_id`, `selection.photo_sha256`, `selection.preview_image`, camera name and time. Missing or ambiguous evidence, unreadable media and unusable single-face references block promotion. The selected camera must be authorized for the operator.

`POST /api/admin/unknown/{id}/promote` requires `appearance_id`, the preview's `photo_sha256`, `display_name`, and `decision: "create_new"`; optional fields are `person_code` and `confirm_create_new`. Requests without selection have no bulk fallback. The server rechecks ownership, source status, checksum and current-model duplicate matches within the mutation transaction. The response includes the **new** Known identity ID, source Unknown ID, moved appearance ID and remaining source count/status.

Successful changes and both identity IDs are audited atomically. Any pre-commit failure rolls back ownership changes and removes copied gallery files. Cache refresh events carry the moved sighting separately from the still-Unknown source. A repeated request cannot transfer the same event again.

## Maps

Movement maps display configured camera names beside their locations, with the pipeline ID as a fallback if no name is configured. Cameras at the same coordinates share a label. Playback highlights its current camera; map-style changes preserve labels. Overview framing leaves room for labels and replay controls. Cameras still require valid coordinates to appear geographically. Labels do not establish a person's identity or movement between observations.

## Verification

Synthetic PostgreSQL tests cover exact selection, unchanged unselected history, sole approved reference, duplicate-review isolation, permissions, absent/stale/ambiguous images, audit/copy/reference rollback, retry, empty-source preservation and queued detections. JavaScript tests cover preview races, display failures, request payload, duplicate clicks and source refresh. Firefox validation exercises the real dialog on desktop/mobile and MapLibre labels, current-camera highlighting, basemap changes, repeated rendering and cleanup. Production verification is read-only; no real person is promoted for testing. No database migration or retention-setting change is required.

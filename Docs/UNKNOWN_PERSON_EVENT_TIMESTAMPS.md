# Unknown Persons: authoritative camera events

## Event contract

Each accepted face occurrence is stored as a `Detection` + `Face` + `IdentityAppearance` in one transaction. `IdentityAppearance` now carries its own unique event ID, detection ID and UUID, identity, camera, recorded location, timestamp source and snapshot reference. Existing per-frame duplicate suppression remains; unknown-person occurrences are no longer suppressed by the cross-frame temporal tracker.

`Detection.timestamp` and `IdentityAppearance.start_time` use the same UTC instant. An explicit top-level webhook `captured_at` must be ISO-8601 with a timezone, for example `2026-09-13T10:15:32+03:00`; it is normalized to UTC and labeled `camera_reported`. Without it, the webhook's server receipt time is preserved through queueing and labeled `server_received`. Direct processing without a receipt timestamp uses `server_processed`. A camera-reported time is the camera's claim, not proof that its clock is synchronized. One top-level capture time applies to the images in that request; send separately timed captures as separate requests.

Unknown identity first/last-seen summaries advance only while persisting their appearance. A late older occurrence is retained but cannot move the latest summary backwards. New unknown-person WebSocket messages and dashboard unknown-activity notifications are published only after commit. The batch and direct writer use the same persistence and publication functions. UUID replay does not create duplicate rows. WebSocket delivery itself is best effort; the API and appearance history remain authoritative after reconnect.

## API and page

`GET /api/admin/unknown` includes `camera_events`, keyed by camera ID. Each value contains:

```
event_id, appearance_id, detection_id, detection_uuid,
identity_id, pipeline_id, location_name, timestamp, timestamp_source,
snapshot_path, snapshot_url, appearances_count
```

Latest events are selected by identity + camera, ordered by event timestamp and appearance ID. Counts are computed in the backend per identity/camera. Date and camera filters require an occurrence satisfying those conditions together. Existing identity-wide summaries remain separate.

The initial WebSocket payload carries a timestamp on every event rather than assigning one camera-group timestamp to all people. Card updates target identity + camera, reject duplicate/older events and display date and time. A previous event's image is not retained as the new event's snapshot when the replacement is unavailable. Appearance history exposes the event/detection IDs, and its camera tooltip shows provenance. Recorded locations are preferred in the timeline.

## Historical data and retention

Migration `fdd4e5f6a7b8` preserves existing appearances and gives them stable legacy event IDs. It backfills detection links only for an exact, unambiguous identity + camera + timestamp match. Historical camera names and camera capture times that were never recorded are not invented. Missing pre-existing history cannot be recovered by this change.

Normal detection retention may remove a `Detection` row; its appearance still retains the event ID, detection UUID, camera and timestamp. The live detection foreign key becomes NULL. There is no new automatic appearance-age deletion policy. Explicit identity deletion and merge/unmerge still follow their existing lifecycle rules.

Image-saving limits and snapshot-retention settings remain in effect. Consequently a preserved event can have no available image. A representative image from another event is not substituted as though it belonged to that sighting.

## Validation and deployment

- Additive migration applied; API refreshed.
- 80 backend regression tests passed, including the three-sighting example, another person, late arrival, replay, time-zone normalization, retained history, enrollment, known-face deletion and background services.
- Browser tests passed for dashboard and Unknown Persons, including two camera cards for the same person, a separate person, delayed messages, image failures, and desktop/mobile layouts. Pure event-handler checks also passed.
- Live API verification matched 51 camera events against their database rows; the live initial WebSocket contained 54 individually timestamped events. Known Faces returned HTTP 200. Background health reported no errors.
- Old derived display caches were invalidated during rollout. No synthetic people were left by the rollback-only scenario test.

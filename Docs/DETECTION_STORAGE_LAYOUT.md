# Camera image storage

New camera crops use:

`storage/detections/YYYY/MM/DD/<camera_uuid>/<capture_uuid>/<face_event_uuid>.jpg`

- Date is UTC at the start of frame processing.
- UUID pipeline IDs are used directly. Other pipeline identifiers map to a
  deterministic UUIDv5 (`NAMESPACE_URL`, `face-detector:camera:<pipeline_id>`).
  Camera display names remain in the database and frontend.
- The capture UUID is allocated before inference and groups crops from the same
  incoming frame. It is not the integer `detections.id`, allocated later.
- Each crop filename uses the face's `_event_id`, also used by detection alerts.
  `faces.detection_id` and `faces.identity_id` remain the authoritative links.
- Images are JPEG quality 90, as before. A temporary file is renamed after a
  successful write. Failed or disabled saves produce no database image path.

## Compatibility

Existing files and database paths are not migrated. Enrollment continues to use
`storage/faces/<identity_uuid>/image_NNN.ext`; pending enrollment is unchanged.
Consumers use saved database paths, so history, profile selection, promotion,
merge and image serving support both layouts. The dashboard also looks up prior
saved camera crops by identity and camera when a capped event has no image.

`SAVE_IMAGES`, `SAVE_UNKNOWN_FACES`, and `MAX_PHOTOS_PER_PERSON` keep their
existing roles. The photo cap counts existing files linked to the identity and
camera, plus the historical name folder. It does not cap enrollment photos.
Unknown crops retain the existing unlimited behavior when saving is enabled.
This change introduces no new sampling or recognition thresholds. As in the
previous implementation, the cap is not a cross-worker transactional quota.

Scheduled cleanup excludes files referenced by surviving path columns, merge
provenance or search results, and always protects enrollment files. It clears
expired snapshot references without removing another record's file. Permanent
person deletion continues to gather paths from database records.

No automatic orphan-file deletion or historical migration is introduced. Files
left by an interrupted detection write require a separate audited cleanup.

Validation: `python -m pytest tests/test_detection_storage.py -q`

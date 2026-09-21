# Known Faces audit — 2026-09-21

Scope: `/admin/known`, its directory and lifecycle APIs, shared enrollment dialog, review/confirmation APIs, gallery/primary-photo APIs, image storage, database links, and production storage integrity. The initial audit changed neither application code nor production records. The subsequent requested fixes are described below.

## Findings

1. **Medium — simultaneous primary-photo changes can return HTTP 500.** `backend/core/enrollment_service.py:1738` loads the person without a row lock, demotes the previous primary, then promotes the selected image. Two overlapping transactions can both start with the same previous primary. The second transaction does not demote the first transaction's newly selected primary and hits `uq_identity_image_one_primary`. Reproduced against the disposable PostgreSQL database using the actual `set_primary_image` service: one request succeeded, the other returned `primary_update_failed`, status 500. The unique constraint protects the data from having two primaries. Serialize photo mutations by locking the identity before reading/changing primary state. Also examine the analogous first-photo race in `enroll_image`, which determines primary status with a separate count query.

2. **Medium — an old upload response can overwrite a newly opened dialog.** `frontend/js/upload-modal.js:464` has no request-generation check. Closing a dialog clears timers that already exist, but does not prevent an outstanding request from updating the form and scheduling a new close timer after it has been reopened for another person. Executed the real JS handler with deferred fetch and mocked DOM: the old response displayed “Saved Person A” in the new dialog, cleared the new name, and closed it. The request URL is captured before fetch, so this reproduction does not demonstrate saving A's file onto B. Bind response/UI callbacks to an upload generation; still refresh the directory when a background save succeeds. Do not treat closing a dialog as cancellation of a server-side save that may already have committed.

3. **Medium — “Last seen” sorting disagrees with the displayed date.** `backend/routes/known_faces.py:45` orders by raw `Identity.last_seen_at`, while the response hides that timestamp when `appearances_count` is zero. Enrollment seeds the non-null timestamp with enrollment time. An unseen person enrolled today therefore sorts ahead of a person actually seen yesterday. Reproduced by calling `list_known_faces` against two isolated records: output was unseen, then seen. Sort on the same effective timestamp returned to the client, with unseen records last.

4. **Low — merge photos are mislabeled as uploads.** `frontend/js/admin-known.js:134` recognizes only `source_type === 'promotion'`; all other values display “Uploaded photo.” Production has one completed gallery photo with `source_type='merge'`, so the page currently presents its provenance incorrectly. Use explicit labels for upload, cropped_face, promotion, and merge, with a neutral fallback.

5. **Low — recognition-index warnings are hidden.** The enrollment service preserves successful image/database saves if index synchronization fails and returns `warning`/`warnings` containing `vector_index_sync_pending`. Both upload-success branches in `frontend/js/upload-modal.js` ignore these fields. This is a code-confirmed missing notification, not an observed production index outage. Show that the photo is saved but recognition indexing is pending when the response includes that warning.

## How an image is saved

1. The page opens the shared upload dialog. New people use `POST /api/upload-person`; an existing person's Add photo action uses `POST /api/identities/{id}/images`. Requests carry cookies and `X-Requested-With`.
2. The backend validates the image size/type/decoding, requires a detectable single face with real landmarks, computes and validates its face signature, and checks whether it belongs to the selected person. Ambiguous matches return HTTP 202 and require review rather than claiming a completed save.
3. A review upload is temporarily stored separately under managed pending storage. Its database ticket is bound to the administrator and uses a hashed token with expiration. Confirmation revalidates the selection, consumes the ticket, and enrolls through the shared service; cancellation and expiration remove pending data.
4. The ordinary enrollment path writes the original uploaded bytes to a temporary file. It chooses a UUID-owned directory and reserves a server-generated filename such as `storage/faces/<person-uuid>/image_001.jpg`. Display names and client paths do not determine directory ownership. The original full image is stored; `is_face_image` records that the input was already a crop, rather than converting every upload into a saved face crop.
5. PostgreSQL stores the person in `identities`; path, checksum, dimensions, size, source, primary flag, and uploader in `identity_images`; and the recognition vector linked to the person and image in `identity_embeddings`. Image bytes are filesystem data, not a database blob.
6. The file moves into its final location with `os.replace` immediately before the database commit. Ordinary caught failures roll back the transaction and remove unfinished files. This is compensating cleanup across filesystem and database, not a single cross-system atomic transaction; sudden process/host failure remains a separate crash-recovery concern.
7. Recognition-index synchronization follows the commit. A failed index update should leave the saved image intact and report a pending-index warning; reconciliation can retry it.
8. The gallery lists `identity_images` via GET `/api/identities/{id}/images`. Setting primary changes flags and `identities.best_snapshot_path`; it does not rename files or create another embedding. Renaming a person does not move their UUID folder.

Production uses the named Docker volume `face_detector_prod_storage_data` mounted at `/app/storage`. It persists across normal container recreation. It must remain part of backup/restore alongside PostgreSQL.

## Verified production state (read-only)

- Two active known people, three completed gallery-image rows, two primary images, no incomplete gallery rows.
- All three image files exist. All recorded byte sizes and SHA-256 checksums match their files.
- Both directly uploaded images have linked embedding rows. The third image came from merge snapshot adoption and has no direct `image_id` embedding link; its surviving identity has embeddings. Merge adoption deliberately adds gallery evidence and transfers identity-level signatures separately, so this alone is not proof of failed enrollment.
- `/admin/known` and management endpoints require administrator access. Mutation routes enforce the application's custom-header CSRF policy. `/storage/{path}` requires authentication and checks the resolved path stays inside managed storage; storage access is not restricted to administrators by this route.
- Deactivation retains photos/signatures. Permanent deletion checks an exact name and preview token, journals the operation before destructive cleanup, leaves an interrupted deletion inactive and retryable, and retains files referenced by other records.

## Verification and limits

Dedicated isolated PostgreSQL probes reproduced findings 1 and 3. A mocked-DOM execution of the real upload handler reproduced finding 2. Five shared upload/promotion JavaScript tests passed. Production file/hash checks were read-only. Findings 4 and 5 follow directly from response values and UI branches.

The original Python run completed with **142 passed, 4 failed** across `test_known_faces_management.py`, `test_known_face_lifecycle.py`, `test_identity_multi_image_enrollment.py`, `test_enrollment_target_review.py`, and `test_enrollment_decision_gate.py`. All four failures were in multi-image test helpers: three expected HTTP 201 despite receiving the intentional HTTP 202 review prompt; one answered a target-person review with `create_new` and received the expected HTTP 409 `name_already_exists`. These original tests need updating to follow review confirmation. **All four scenarios passed** in a fresh isolated stack using a temporary adapted copy that explicitly confirms the target person and accepts the confirmation route's HTTP 200. Application code was unchanged. The temporary test module was removed afterward; its copy is retained with audit evidence.

Evidence: `logs/regression/regression_232872_186a039b.log`, its adjacent API log, and `logs/known-faces-audit-20260921/` for the independent reproduction scripts and results. Both test databases and their storage volumes are disposable and separate from production.

This audit does not claim a full interactive browser run or power-loss recovery testing.


## Follow-up fixes

All five findings have focused code fixes: optional identity row locking for enrollment and primary-photo mutations; dialog-generation guards for uploads and review responses; effective camera timestamps for Last seen ordering; explicit photo-source labels; and visible pending-index warnings. Successful background saves still refresh the directory, and review tickets received after the dialog closes are cancelled. Script versions were bumped for the Known Faces page and shared upload code.

Regression coverage includes overlapping primary-photo transactions, camera-date ordering, stale upload/review responses, pending-ticket cleanup, and both warning-display paths. The legacy helpers for the four affected scenarios now answer target-person review prompts correctly. The fixes were subsequently deployed at the user's request.

Fix validation: **147 Python tests passed**, including the new sorting/concurrency regression; **37 frontend tests passed**. The finalized isolated concurrency test also passed independently. Logs: `logs/regression/regression_259855_4fabf088.log` and `logs/known-faces-audit-20260921/fix-tests.log`.


Deployment verification: API and ML worker are healthy; public `/health` is healthy; all six application-file hashes match the tested workspace in both containers. Authenticated `/admin/known` serves the bumped script version and the directory API returns HTTP 200. Environment variables, storage mounts, and GPU allocations were preserved; Ollama still has no GPU device requests. Backup: `/backups/20260921T081323Z`. Rollback images: `face_detector_prod-face_recognition:before-known-fixes-20260921` and `face_detector_prod-ml_worker:before-known-fixes-20260921`. Updates were built as file-only layers over the existing production images because the standard build path was blocked by the host's missing Buildx plugin and unreadable `.deployment` directory.

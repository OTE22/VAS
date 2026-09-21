# Intelligence page audit — 2026-09-21

Scope: `/admin/intelligence`, identity selection, photo search integration, related identities, temporal charts, tracking/map data, complete analysis, and relationship recalculation/persistence. The initial audit did not change Intelligence application code or recalculate production relationships. The subsequent requested code fixes are recorded below.

## Confirmed findings

1. **Medium — Daily Distribution renders zero bars despite existing data.** `backend/core/intelligence_service.py:717` counts weekdays using Python's numeric weekday index, and the JSON response contains keys such as `"2"` and `"3"`. `frontend/js/admin-intelligence.js:1113` instead reads `monday`, `tuesday`, etc. A production response contained 18 appearances with numeric weekday keys. Executing the actual renderer with four Monday appearances produced `[0,0,0,0,0,0,0]`. Read weekday indices 0–6 in the renderer, keeping Monday first.

2. **Medium — Stored relationship percentages depend on recalculation order.** `backend/core/intelligence_service.py:624` orders each stored pair by UUID but copies the percentage calculated relative to the current target person. The database defines that percentage relative to `identity_id_1`. In an isolated reproduction, person A had four appearances and B overlapped one of them. Refreshing A saved 25%; refreshing B then replaced that same ordered A–B row with 100%, although A's evidence had not changed. Fix the stored denominator to the canonical first identity (or explicitly store separate directional percentages). Revisit the stored strength at the same time because it is derived from that percentage. The main Related tab currently recalculates results directly, so the demonstrated corruption concerns persisted relationship data and its consumers, rather than proving every displayed Related result is wrong.

3. **Medium — Complete Analysis and map data bypass feature toggles.** The dedicated related/temporal/tracking routes enforce their respective enabled settings. `backend/routes/intelligence.py:970` calls all three services directly without those checks; `get_tracking_map_data` likewise calls tracking without checking `CROSS_CAMERA_TRACKING_ENABLED`. With all three features disabled in the isolated process, the dedicated temporal route returned 403, while Complete Analysis returned ready temporal and related sections plus tracking results. This is inconsistent feature control for already authorized administrators, not a demonstrated authentication bypass. Apply the same feature policy across aggregate/map routes and show a disabled section explicitly.

4. **Medium — Timeouts can leave a permanent loading state.** `frontend/js/admin-intelligence.js:268` marks every fetch `AbortError` as an intentional cancellation. Section loaders return silently on `err.aborted`. The request's own timeout therefore follows the same path as a superseded request: no error state replaces the spinner. A runtime probe triggered the timeout controller and received `{aborted:true,message:"Request cancelled"}`. Distinguish timeout expiry from user/request cancellation and display a retryable timeout error. The fallback when `AbortSignal.any` is absent also drops the timeout signal whenever an external signal is supplied.

5. **Low — Exact-date tracking excludes the final fraction of a second.** `backend/core/intelligence_service.py:790` ends a selected date at `23:59:59.000000` and filters with `<=`. A sighting at `23:59:59.500000` is incorrectly excluded. The isolated probe stored four appearances on the selected day but received only three. Map data shares this service and the same boundary. Use a half-open date interval: start of the selected day inclusive, start of the next day exclusive.

## API and persistence flow

| UI action | Backend | Database/file behavior |
|---|---|---|
| Select/search people | `GET /api/admin/identities`, `GET /api/admin/identity/{id}` | Reads identity metadata and available snapshots; server pagination/filtering |
| Search by photo | `POST /api/search/by-image` | Decodes image, extracts a face, ranks identities; records search metadata and SHA-256 in `search_history`, not uploaded image bytes |
| Related identities | `GET /api/identities/{id}/related` | Calculates from `identity_appearances`; does not persist those computed relationships |
| Temporal charts | `GET /api/identities/{id}/temporal-patterns` | Aggregates appearance start times and pipelines in memory |
| Tracking / map | `GET /api/identities/{id}/cross-camera`, `GET /api/identities/{id}/map-data` | Reads appearance intervals and pipeline coordinates; emits movement/snapshot data and GeoJSON |
| Complete Analysis | `GET /api/identities/{id}/analyze` | Calls related, temporal, and tracking services; returns individual section statuses |
| Calculate all relationships | `POST /api/intelligence/relationships/calculate-all` | Creates a background-job record; rebuilds `identity_relationships` per active identity with per-identity commits and progress updates |

The relationship table has ordered-pair and uniqueness constraints. Those prevent duplicate reversed rows but cannot enforce that the percentage uses the correct person's denominator. Recalculation is not one transaction covering the entire job: failures can leave a partially refreshed set, with job progress/failure details recording the outcome.

Snapshots shown on this page reference existing managed storage files; displaying Intelligence does not enroll a new person. The photo-search input is processed separately from Known Faces enrollment.

Other reviewed limits: Related analysis scans at most 500 target appearances and 1,000 counterpart rows per window without returning truncation metadata, so high-volume histories can be incomplete. Temporal/tracking services load their selected appearance rows into memory. These are scaling/interpretation concerns in addition to the five reproduced or code-confirmed defects above.

## Verification

- Authenticated production GET checks returned HTTP 200 for `/admin/intelligence`, related identities, temporal patterns, cross-camera tracking, Complete Analysis, and map data, using existing identities with sightings. Identity-prefix search also returned the expected person. No production recalculation or data mutation was performed.
- Existing Intelligence/Security Intelligence suites: **68 passed, 3 skipped** in disposable services. The skipped cases required existing identity fixtures that were absent in the empty test database; live read-only checks covered the corresponding basic analysis paths.
- Two Intelligence filter JavaScript tests passed.
- Dedicated database probes confirmed the date exclusion, percentage overwrite, and disabled-feature inconsistency. A separate JS probe executed the chart renderer and timeout handler.
- The disposable API worker stalled during startup once and was restarted before the probes; production was unaffected.
- No full interactive browser/map-rendering run was performed. Successful HTTP responses do not imply that chart values or persisted percentages are correct.

Evidence: `logs/intelligence-audit-20260921/`, `logs/regression/regression_283552_3ffc72ef.log`, and `logs/regression/regression_292251_d2177b90.log`.


## Requested fixes

All five findings now have focused fixes in the workspace:

- Weekday charts read numeric weekday keys, Monday through Sunday.
- Reversed relationship pairs are recalculated from the canonical first person's perspective before storing percentage, count, and strength. The pair-restricted query avoids another person's ranking displacing the requested pair.
- Complete Analysis returns explicit disabled sections without running disabled services. Map data applies the tracking feature flag. The UI labels disabled sections and disables their detail buttons.
- A single abort controller carries both caller cancellation and timeout; timeouts produce a visible retry message while deliberate cancellation remains silent. This also works without `AbortSignal.any`.
- Exact-date tracking uses the next day's start as an exclusive upper boundary; out-of-range end dates are rejected cleanly.

Validation: **69 Python checks passed across the regression run and targeted recheck; 3 empty-database fixture cases skipped. Eight Intelligence JavaScript tests passed.** The first regression run had one stale cache-version assertion, corrected and rechecked successfully; it was not an application failure. New database coverage confirms 25% stays 25% when recalculating from either person, the final fractional-second sighting is included, and disabled sections do not call their services.

The Intelligence changes were subsequently deployed at the user's request, and stored relationships were recalculated successfully (details below). Canonical reverse-pair calculation adds work to relationship-cache refreshes; normal read-only Related requests retain their existing calculation path and bounds.

Evidence: `logs/regression/regression_330522_68c05c6e.log` and `logs/intelligence-audit-20260921/fix-tests.log`.


## Deployment — 2026-09-21 08:50 UTC

- API and ML worker updated from the current production images with the four tested Intelligence application files; previous Known Faces fixes preserved. All ten changed application-file hashes match in both running containers.
- Both services and the public health endpoint are healthy. The authenticated Intelligence page serves `intel-8-consistency`; temporal, tracking, map-data, and complete-analysis endpoints return HTTP 200.
- Relationship job `relationships-cd1eb8ec` completed successfully: 26/26 identities processed, zero failures, 100% progress.
- Runtime environment, persistent mounts, and device allocations preserved. Ollama remains CPU-only with no GPU device requests.
- Verified backup: `/backups/20260921T084800Z`. Rollback image tags retained: `face_detector_prod-face_recognition:before-intelligence-fixes-20260921` and `face_detector_prod-ml_worker:before-intelligence-fixes-20260921`.

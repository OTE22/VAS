# Read-only camera analytics — Stage 2 implementation report

Date: 2026-09-29. Scope: approved Stage 1 design, superseded by the user's explicit requirement to **use existing database contents and add nothing to the database**.

## 1. Outcome and deployment status

Implemented a bounded logical analytics layer by extending the existing dataset explorer, notebook exporter and ML Ops page, and refactoring existing pair/graph/camera-prediction code. No new database objects, migrations, grants, model registrations, feature snapshots, labels or training jobs were created. Existing operational and managed training workflows remain separate.

**The application changes have not been deployed.** Executed inspection notebooks have been published in the existing Jupyter workspace for review. Publishing these files did not restart Jupyter, modify its runtime or activate a model.

Executed notebooks: [Existing-data-audit-20260929/START-HERE.ipynb](https://face-detector.internal/notebooks/lab/tree/Existing-data-audit-20260929/START-HERE.ipynb).

## 2. Existing architecture

VMS selects recognition evidence from inference/tracking and delivers events to VAS. VAS retains detections, face results, identity appearances and embeddings, plus existing assessments, alerts, relationships and ML artifacts. It does not persist continuous per-frame scoped object positions. Redis and WebSockets are transient transport; neither becomes an invented position dataset in this implementation.

The full inventory and measured limitations remain in [existing state](data_mart_existing_state.md), [gap analysis](data_mart_gap_analysis.md), [design](data_mart_design.md) and [data quality](data_mart_data_quality.md).

## 3. Five-service mapping

| Service | Reused inputs | Result of this change |
|---|---|---|
| Behavior assessment | Existing numeric feature snapshots, appearances, assessments, saved datasets | Clear feature-snapshot grain and retained-data limitations; no new trajectory model or changed scoring contract |
| Pair relationships | `identity_appearances`, camera configuration, currently activated thresholds | Shared bounded source matching with canonical identity/source references and explicit tolerance/provenance |
| Social network | Same pair matcher, existing relationship cache | Consistent period bounds; read-only period edges from exactly the returned pair-evidence population; current cache explicitly separate |
| Analyst review queue | Existing assessments, labels and linked predictions | Read-only inspection preserves risk score, model output and human review as distinct fields; no candidate creation |
| Numeric experiments | Existing registered Parquet datasets, frozen feature definitions | Existing explorer verifies recorded file hashes; new analytical exports support inspection, without silently registering data or changing experiment/training inputs |

## 4. Tables reused and database additions

Read paths reuse `pipelines`, `detections`, `faces`, `identity_appearances`, `identities`, `identity_relationships`, `learned_thresholds`, `threat_assessments`, `live_alert_triggers`, `ml_labels`, `ml_predictions`, and existing dataset metadata/artifacts. Existing settings determine matching tolerances. The live validation read only relevant settings keys.

**New tables/views/materialized views/indexes/columns/triggers/extensions/roles/grants: none.** Existing SQL grant/revocation scripts and migrations were untouched. Production data was never seeded, refreshed, repaired or rewritten.

## 5. Missing grains remain unavailable

Continuous track positions, normalized image coordinates, velocity, acceleration, measured path length, physical person-to-person distance, occupancy, entry/exit counts, observed zones and next-zone targets cannot be derived from this retained population. NULL identity, image, end-time and track references are preserved or identified; missing prerequisites are not replaced with zero or synthetic labels.

## 6. Logical dataset contracts

All new projections live in `backend/ml/dataset_explorer.py` under contract `existing-data-v1`.

| `kind` | Grain | Key and interpretation |
|---|---|---|
| `observations` | One saved face result | `source_id = faces.id`; detection ID/UUID, similarity, pixel ROI and image-reference availability |
| `sightings` | One saved appearance | Appearance ID with identity/camera, event/detection references, source/start/end/created timestamps and nullable track |
| `camera_windows` | Camera / UTC window | Independently counted detection events, recognition results, sightings, distinct observed identities, alert triggers and assessments |
| `co_occurrences` | Canonical identity pair / source appearance pair | Both appearance IDs, camera IDs, timestamps, same/cross-camera classification and sighting gap; physical encounter duration unavailable |
| `period_edges` | Canonical pair within requested scope | Counts and evidence IDs aggregated from the same pair-match population; includes anchor identity and denominator |
| `cached_edges` | Existing mutable cache row | Requested scope selects identities; cached counts can include other times/cameras and are not historical period counts |
| `events` | Source type / source ID | Existing assessments and live-alert triggers with distinct score semantics |
| `reviews` | Existing assessment | Separate nested existing label records and linked model prediction; scores are never overwritten |
| `next_camera` | Observed adjacent camera transition | Input appearance IDs and anchor/availability time separated from actual future target camera/time/appearance ID |

The existing registered dataset explorer remains the route for exact training datasets. Analytical inspection exports are not automatically ML-ready training datasets.

## 7. Read flow and access control

The new `GET /api/ml/analytics/existing` route uses the existing **admin-only `ML_MANAGE` capability**, existing rate limiting, and a separate `REPEATABLE READ, READ ONLY` transaction. Authentication can have its own existing request session; the analytical session is not reused for mutations. The reader refuses dirty sessions and transactions without PostgreSQL read-only mode.

Parameters: `kind`, UTC `start`/`end`, repeated `pipeline_ids`, optional identity UUID, `limit`, `window_seconds`, and `notebook=true` for an offline notebook download. Pair and next-camera projections require an anchor identity. Camera windows reject an identity filter so their scope cannot be misleading. Unknown cameras are rejected. Every camera-based join/count uses the selected camera scope; the admin capability is intentionally estate-wide. This endpoint must not be exposed to a lesser role without introducing its existing access policy.

There is no ETL writer or persistence job. Database errors do not trigger dataset creation, grants, refresh, training or automatic retries that mutate state. Query-budget errors request a narrower scope; denied source access is reported explicitly.

## 8. Feature engineering implemented

Recognition ROI width, height and center are calculated only for finite, nonnegative, positive-size pixel boxes. Original frame dimensions are unavailable, so no normalized coordinates or motion values are generated. Camera windows use UTC `date_bin` with epoch origin and independently grouped sources, preventing join fanout. Window size defaults to 10 seconds and is a bounded request parameter, not a saved database setting.

Camera sequences use stable timestamp/source-ID order. Exact same-camera/time duplicates select the lowest source ID. Simultaneous observations at different cameras break both sides of a session. Gaps over the existing two-hour session threshold split sessions. Examples use only inputs available at their anchor; a target must actually occur later. Late-arriving inputs cannot acquire targets that already occurred before the input became available.

## 9. Pair and graph changes

`intelligence_service._calculate_co_appearances` now applies the requested half-open period to **both sides** of matching, uses deterministic source-ID tie ordering and returns optional bounded evidence/provenance behind its existing list-compatible interface. Same/cross-camera counting shares one recording function. The inspection path also excludes appearances recorded after its historical period end.

The existing social-network calculation passes one fixed period end through all calls and canonical rechecks; its unreachable duplicate “higher count” branch was removed. New period-edge inspection aggregates precisely the exported match definition. It describes an anchor identity's neighborhood, not an unbounded whole-estate graph. Direction-sensitive threshold policy and the anchor denominator are explicit.

Thresholds describe tolerated observations and configured camera separation, not measured encounters between people. A count of many source pairs is not the same as that many unique physical meetings.

## 10. Historical model compatibility

Existing `coappearance-features-v1` and other saved vectors/models were **not redefined or rewritten**. Their limitations now state that legacy numeric pair counts have a one-sided lower lookback bound and cache-derived `90d` fields describe collection-time cache observations. Correcting that saved contract requires an explicit future feature-version boundary, not a silent change under a trained model.

The current behavior models, numeric experiment definitions, training-only preprocessing, tuning, evaluation, approval and deployment workflow remain unchanged. No new training population or registry entry was introduced.

## 11. Threshold support

Pair analysis retains existing static/current activated threshold precedence (location, pipeline, global, static fallback). Responses identify tolerances, camera-distance threshold, provenance and whether an explicit time override applies. Current activation is not reconstructed as a historical activation state. Period analysis avoids the unrelated wall-clock correlation boost.

Camera-window measurements can be inspected statistically. They are not exposed as occupancy, people present or vehicle counts, and no new alert business thresholds were hardcoded or activated.

## 12. Review and feedback

Existing assessment scores/confidence/status, linked model scores/calibration/version and existing human labels/reviewer/status/timestamps/supersession references remain separate. Labels are fetched separately to avoid duplicating assessment rows. If label/prediction limits are reached, the response is partial. No prediction or review outcome is created from a heuristic.

This camera-scoped view includes assessments with a selected camera. It does not assign global/unlinked reviews to a camera or replace existing enrollment, merge or label-management screens.

## 13. Maps and next-camera prediction

Removed name-derived security-zone polygons and risk classifications from `_security_inputs`. Maps retain valid camera locations and journeys; metadata and warnings identify unavailable security zones and explain that connecting lines are not measured paths.

The existing frequency predictor now filters event time **and record-availability time** strictly before the requested prediction anchor, using its anchor-relative lookback instead of wall-clock history. It reuses deterministic session extraction. Its algorithm label is `trajectory-v4`. Dead distance/walking-speed fallback code and its misleading claim were removed. Frequencies remain uncalibrated, and the existing minimum supporting-session requirement remains enforced.

## 14. Notebook and UI workflow

After application deployment: **ML Ops → Advanced tools → Dataset and model evidence → Inspect existing database data**. Select cameras, period and projection, then preview the returned records or download the inspection notebook. Existing dataset/model stage controls remain in place.

The first notebook cell contains and displays the actual exported records; full returned data is in `dataset`. It verifies the embedded payload checksum. The second cell shows bounds, grain, coverage and unavailable measurements; the third checks relevant source-key/target invariants. No database credentials or network-dependent extraction are embedded. Saved-model notebooks retain their existing workflow and now make feature grain/unsupported motion measurements explicit.

New notebooks use the existing HTML display approach and standard library rather than requiring pandas in Jupyter. The saved Parquet explorer verifies a recorded SHA-256 before preview; legacy artifacts without a recorded file hash are explicitly unverified.

## 15. Live validation results

On 2026-09-29, final sources were loaded into a separate short-lived audit interpreter and used against the existing application database role. The running API/worker files were not changed. Connection defaults and the transaction enforced read-only mode. A SQL listener permitted only SELECT/SET/SHOW. All nine projections completed without truncation for two configured cameras over the selected trailing seven days.

| Projection | Returned rows |
|---|---:|
| Recognition observations | 225 |
| Sightings | 145 |
| Camera windows | 84 |
| Co-occurrence source pairs, selected anchor | 438 |
| Period edges, selected anchor | 6 |
| Current cached edges in selected population | 48 |
| Assessment/alert events | 12 |
| Existing assessments with review/prediction fields | 4 |
| Valid next-camera examples, selected anchor | 0 |

The zero next-camera result is an honest empty evidence population, not proof that nobody moved. Recognition/sighting/assessment totals reconcile with independent source counts in the camera windows; period-edge source-pair totals reconcile with co-occurrence evidence. Audit notebooks record precise UTC bounds, capture time, camera IDs and query-source SHA-256 hashes.

## 16. Tests and execution evidence

- **172 isolated intelligence tests passed**, including 20 new analytics regressions. Disposable PostgreSQL fixtures tested half-open bounds, camera scoping, fanout prevention, canonical pair IDs, source-count agreement, cross-camera policy, ties/duplicates, historical cutoffs/late arrival, ROI prerequisites, empty/partial populations, review separation, artifact integrity, SELECT-only execution and endpoint UUID/UTC/notebook serialization.
- **Nine actual-data notebooks executed all three cells: 27 executed cells, no errors.** The execution report is included beside the notebooks in Jupyter. Execution occurred in the existing notebook image with external networking disabled.
- **14 existing notebook tests passed** in that image.
- **26 existing JavaScript workflow/prerequisite scenarios passed**; modified workflow JavaScript passed syntax validation.
- `git diff --check` passed. No migration tests are applicable because no migrations were introduced.

Two environment differences were found during validation: the worker lacks IPython display, and the notebook runtime lacks pandas. The final export renders without adding those dependencies. A pre-existing SQLAlchemy `declarative_base()` deprecation warning remains; no test failed because of it.

Fixtures created/truncated records only in the disposable `intelligence_test` database. The test container and network were removed. They never targeted production.

## 17. Performance and query budgets

Requests require 1–30 cameras, a nonempty period no longer than seven days, 1–2,000 output rows and 1–3,600-second aggregation windows. Sparse camera windows avoid a dense camera×time expansion. Queries use existing indexed camera/time and identity/time populations. SQL statements have a 10-second timeout and two-second lock timeout; the overall analytical operation has a 25-second timeout.

Existing pair computation remains bounded at 500 anchor appearances and 1,000 candidates per window; optional source evidence is capped at 5,000 and reports partial results. It still uses per-window queries, so large histories may require a narrower request. Partial results are not complete period totals. No new indexes or background aggregation jobs were added.

This is not a 30-camera throughput benchmark. The earlier hypothetical per-frame position volume estimates remain in the design report; this implementation neither captures that volume nor proves GPU/NVIDIA decode capacity.

## 18. Data-quality boundaries

Source-row duplicates remain traceable rather than being deleted. Camera/time duplicate sightings are collapsed only for sequence direction inference. Record timestamps and current identity ownership cannot reconstruct arbitrary pre-merge/pre-review database history. Missing original frame dimensions remain missing. Observations represent selected saved evidence, so no-saved-observation windows cannot establish an empty scene.

Reviews/caches report current mutable values; model artifacts retain their recorded historical contracts. Crops, embeddings and server filesystem paths are not exported through the new endpoint. Source UUIDs and IDs provide lineage.

## 19. Remaining limitations

Continuous trajectory/next-position/next-zone modeling remains blocked by missing source grain. The new next-camera export is inspection evidence, not a fitted or automatically registered model. The current source population is too limited to establish production accuracy or 30-camera capacity. Review data may be empty or unlinked, and scope/limits are explicit. Query caps and timeouts protect responsiveness but are not evidence of complete analysis over larger deployments.

Application build/deployment and a browser smoke test against the deployed endpoint are still separate release steps; this report does not claim they happened.

## 20. Recommended next steps

Review the executed notebooks and this code change, then deploy through the existing release workflow. After deployment, smoke-test the read-only inspector and map warnings. Keep existing feature/model versions pinned. Continue the existing managed dataset → evaluation → approval → service-connection workflow; do not bypass its held-out evaluation or label requirements. When representative cameras are available, separately measure end-to-end load at the requested camera count/frame rate.

# Advanced Features hardening and deployment — 2026-09-28

Status: application fixes deployed to the VAS API and ML worker. This closes the defects documented in ADVANCED_FEATURES_AUDIT_20260928.md. It is not certification of prediction accuracy, calibration, or 30-camera throughput.

## Changes

- Advanced capability states now honor their settings and distinguish availability from evidence sufficiency. Threshold availability also checks worker heartbeat. Correlation's disable flag is enforced and registered for runtime updates.
- Threshold learning is committed to the existing PostgreSQL queue before HTTP 202. The database's existing partial unique index enforces single-flight submission across processes. An unavailable worker produces 503; concurrent work produces 409. Explicit camera selections are validated and capped at 100.
- The worker runs learning in its supervised child process. Existing leases, cancellation and crash detection apply; expired work fails instead of falsely completing. Threshold execution has a 15-minute cooperative timeout plus a supervisor deadline. Interrupted jobs are not automatically replayed; review a failed job and resubmit. No migration was required.
- The old synchronous `/api/intelligence/thresholds/learn` endpoint returns 410 with the durable replacement route. Clients using it must migrate.
- Learning errors and persistence failures propagate to failed jobs. Public threshold errors omit raw database exception details. Empty results have an explicit `insufficient_evidence` outcome; candidates are never activated automatically.
- Threshold histories are fetched once per camera, capped at the newest 2,000 records with truncation disclosed. The 30-camera fixture performs 31 history/pipeline queries rather than repeated queries for 435 pairs. Pair computation uses adjacent visits in both directions, avoiding duplicate samples from repeated detections. Zero coordinates are valid. Algorithm version: `threshold-v3`.
- Candidate version writers and activations use transaction-scoped PostgreSQL advisory locks. Concurrent activation leaves one active value for a scope/signal. Failed candidate batches roll back.
- Trajectory prediction requires outgoing transitions from the selected camera in at least three separate sessions. Unrelated sessions and repeated transitions in one session cannot satisfy this gate. Simultaneous observations do not establish direction. Responses expose transition counts, supporting sessions, history limits and `uncalibrated` status instead of deriving confidence from frequency. Version: `trajectory-v3`. The legacy `probability` field now explicitly represents historical relative frequency, not calibrated probability.
- Correlation preserves its scoring formula but computes exact aggregate statistics while retaining at most 20 example matches. A dense 500-by-500 history can count 250,000 matches without allocating that many dictionaries. The bounded CPU scan runs outside the API event loop. Direction A→B and history truncation are visible; the score is labeled heuristic.
- UI distinguishes insufficient evidence, saved candidates and active thresholds; handles cancelled and missing jobs honestly; shows observed frequencies/counts rather than “high confidence.” The new review dialog lists candidate, active and retired values and supports explicit activation/reactivation. Existing active-value caches may take up to 60 seconds to observe activation. The UI states this delay.

## Validation

- 30 isolated backend checks passed against the built worker image. Tests cover low-support predictions, disabled routes, offline workers, failed queue commits, zero coordinates, bidirectional movement samples, exact correlation scoring, bounded example retention, 30-camera query counts, queue persistence, concurrent submissions, cancellation before execution, expired leases, worker dispatch, candidate rollback, activation and concurrent activation.
- Tests used a disposable PostgreSQL container on an internal Docker network, with no production volumes, credentials or routes. The isolated test harness is opt-in via `ISOLATED_INTELLIGENCE_TESTS=1` and `--confcutdir=tests/isolated_intelligence` to avoid altering ordinary test collection.
- 50 existing worker-boundary, frontend layering and script-order checks passed against the built API image.
- Firefox fixture checks passed for empty learning, saved candidate disclosures, insufficient trajectory support, frequency/count rendering, correlation direction/truncation, availability status, candidate review and activation POST, cancellation and no JavaScript errors. Fixture mutations were never sent to production. These checks do not establish prediction accuracy on real footage.
- Source and deployed hashes matched for the changed routes, worker, threshold runner/store and HTML. Post-deployment API readiness reported database, models, cache, queue and offline policy healthy, with 34 background services and none degraded or stale. Worker heartbeat was current.
- HTTPS readiness returned 200 from VMS using its configured CA bundle; unauthenticated capabilities returned 401. An initial readiness probe timed out during model startup; the service subsequently became healthy. A generic requests probe lacked the application CA bundle; rerunning with the configured bundle passed without disabling certificate verification.
- No production learning/activation jobs or test identities were created. Production learned-threshold count remained zero.

The existing integration tests were updated for `trajectory-v3` and the retired synchronous route. Their data-seeding HTTP suite was not run on production.

## Deployment and rollback

Deployed with the existing production, GPU and GPU-allocation Compose overlays; only API and ML worker recreated. Startup included model loading before API readiness became healthy. No inference-capacity configuration or database schema changed.

- API image: `sha256:99a230b11baff7629f04947e4f8a7dc43fb1bd7320696acaf887ded24a419a35`
- Worker image: `sha256:2e69809af4ece5ca014d821eb23987b17c3ec491f6d55241a452bfc58d06f96f`
- Prior API image retained as `face_detector_prod-face_recognition:before-advanced-hardening-20260928` (d370a948d5f3).
- Prior worker image retained as `face_detector_prod-ml_worker:before-advanced-hardening-20260928` (b8280d81c224).

Rollback must restore both service images and matching frontend assets (JS/CSS are host-mounted). First inspect/drain or cancel queued threshold jobs; the old worker does not support this new task type. Do not replay an interrupted job blindly. The earlier UI-only image was no longer available when tagging was attempted; the retained API rollback image predates the last picker UI update. No database rollback is necessary for these code changes; activating a threshold is a separate administrator action with its own retired-value rollback.

## Remaining acceptance work

Real RTSP cameras and representative cross-camera identity history are still needed for sustained concurrency/latency tests, movement-quality evaluation and statistical calibration. The live audit found two cameras, 359 appearances and no identity appearing on both cameras in the preceding 90 days. Therefore a truthful live outcome may still be “Insufficient Evidence.” The UI now explains that state rather than presenting unsupported readiness or confidence.

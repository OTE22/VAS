# Advanced Features production audit — 2026-09-28

Scope: `/admin/security-intelligence`, Advanced Features. Read-only production inspection and isolated algorithm reproduction. No production settings, records, containers, or application code changed by this audit. Existing frontend changes from the earlier UI work were preserved.

## Verdict

Real backend implementations exist and read real appearance history. This panel is not yet production-ready as a dependable decision-support feature: readiness reporting, evidence confidence, feature gating, and learning-job failure handling need correction. This finding is separate from video ingestion throughput; it neither establishes nor disproves 30-camera capacity.

The inspected route and three algorithm files match the running API container byte-for-byte (SHA-256).

## Live evidence

- Two registered active pipelines; both have coordinates.
- 359 appearance records in the last 90 days, covering 32 identities and two pipelines.
- Zero identities with appearances on more than one pipeline in that period. Therefore there is currently no cross-camera identity history from which these implementations can learn camera-pair travel thresholds or predict next-camera transitions.
- Zero rows in `learned_thresholds`, including zero active thresholds.
- One recorded threshold-learning job: completed/success, zero learned pairs, zero candidates written. This proves completion of an empty run, not successful learning. It does not prove a persistence failure occurred on that run.
- All three advanced-feature flags are currently true. Minimum threshold sample setting is 10; configured cross-camera distance/time defaults are 500 metres / 10 minutes.
- VAS API, nginx, PostgreSQL, Redis, ML worker, map service, and both VMS workers report healthy.

Correlation compares two different identities and can produce associations without same-identity cross-camera history; the zero-overlap finding alone does not establish that correlation must be empty.

## Confirmed issues

1. **Misleading capability readiness.** `backend/routes/intelligence.py:get_security_capabilities` reports all three advanced features enabled/ready without inspecting their feature flags or evidence prerequisites. The frontend calls this status “backend-verified.” Operational availability, enabled configuration, and sufficient evidence must be separate states; capability availability cannot guarantee evidence for every selected identity.
2. **Incomplete feature gates.** `create_threshold_job` schedules a job while automatic learning is disabled; the learner then returns no pair results. `calculate_activity_correlation` does not check `ACTIVITY_CORRELATION_ENABLED`, although relationship analysis elsewhere honors it. Trajectory's route does check its flag. With live flags currently true, the disabled-state defects were established from code, not by changing production configuration.
3. **Threshold persistence failures can look successful.** `_run_threshold_job` catches candidate persistence exceptions and still finishes successfully. The pair learner separately catches exceptions and returns `None`, conflating infrastructure failure with insufficient evidence. Task creation returns `-1` on storage failure, but the scheduling route does not validate that return before accepting work; `mark_running` failure is likewise ignored.
4. **Learning completion does not mean activation.** The backend persists candidates for manual review/activation, and the intelligence service correctly consumes only active values through `threshold_store`. `renderThresholdJobResult` omits `candidates_written` and `activation_note` and renders a green completion state even for zero learned pairs. This conceals the distinction between no evidence, candidates saved, and active thresholds.
5. **Prediction confidence is not evidence confidence.** The predictor requires three sessions in total, not three outgoing transitions from the selected camera. The route derives “high confidence” solely from relative transition frequency. Isolated execution of the actual predictor class and route confidence function reproduced: one A→B observation plus two unrelated C→D sessions yields probability 1.0 and confidence “high.” This is an observed frequency, not validated certainty; outgoing sample support and calibration are missing.
6. **Threshold execution is still in the API process.** The endpoint uses FastAPI BackgroundTasks, not the existing leased ML-worker queue. Job history is persisted, but this execution path does not provide durable queued execution/resumption. Pair learning iterates serially: 30 geocoded cameras require 435 pair evaluations, with repeated history queries. Production execution time and API impact at that size have not been measured.
7. **Correlation is a heuristic association score.** The real implementation calculates time/distance-qualified A→B matches and a participation/consistency score. It is directional and is not a calibrated probability of coordination or wrongdoing. Each history is capped at 500 rows, but dense histories can still materialize up to 250,000 match dictionaries before returning the first 20. The bounded API wrapper does not by itself preempt a CPU loop between awaits.
8. **Valid zero coordinates rejected in learning.** Truthiness checks treat latitude/longitude 0 as missing. Check explicitly for `None` and valid ranges.

## Validation performed

- Traced UI actions to real API endpoints and algorithm implementations.
- Compared deployed/source hashes for routes, threshold learner, trajectory predictor, and correlation analyzer.
- Used aggregate read-only PostgreSQL queries; no identities or biometric records exported.
- Executed the actual trajectory class and API confidence function in an isolated Python namespace with a three-session fixture. No application bootstrap or production database connection was used in this reproduction. This tests the confidence defect, not full API integration.
- Inspected persisted job counts/outcomes and container health.
- Did not run integration suites that seed data against production, trigger learning, activate thresholds, or load-test the live service.

## Hardening order

1. Honor feature flags consistently; report availability separately from data sufficiency; expose empty learning, saved candidate counts, and pending activation honestly.
2. Propagate database/persistence errors and validate job-state transitions; use the durable worker queue with bounded execution, progress, cancellation, and restart-failure/recovery tests.
3. Add per-camera outgoing transition support, minimum-evidence gating, and honest probability/confidence semantics; validate against held-out representative movement history.
4. Bound correlation materialization and make directionality explicit; verify temporal matching and threshold learning with deterministic fixtures, including zero coordinates and failure cases.
5. Validate the complete UI/API/worker lifecycle in isolation, then measure 30-camera ingestion concurrently with analysis using representative RTSP streams and movement evidence. Camera-count capacity and analytical accuracy require separate acceptance criteria.

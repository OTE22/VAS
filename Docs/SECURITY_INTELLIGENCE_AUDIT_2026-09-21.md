# Security Intelligence audit — 2026-09-21

Scope: `/admin/security-intelligence`, its frontend requests, backend analysis and assessment persistence. Audit only; no application fixes or deployment in this pass. Existing changes from previous page fixes were preserved.

## Confirmed findings

1. **High — displayed threat can disagree with the assessment being resolved.** `backend/routes/intelligence.py:1435–1517` computes a fresh assessment but returns the ID of the existing row when `backend/core/assessment_service.py` deduplicates. Score, severity, factors and calculation time come from the new calculation; persisted provenance comes from the old row. A disposable-database probe called the real route and persistence service with controlled decision outputs: first score 10, then 90 under the same model/version and dedup window. The second response displayed 90 with the same assessment ID whose stored score was 10. Recording an outcome uses that ID. Fix direction: make the displayed assessment and persisted row consistent, including all score metadata, or explicitly create a new assessment when appropriate.

2. **Medium — network date range does not constrain cached counts.** `backend/core/security_intelligence_service.py:303` filters cached relationships by their most recent coappearance, then returns accumulated counts and first-seen dates. Node appearance counts at line 379 also have no cutoff. With one coappearance today and two older than ten days, a one-day query returned three coappearances and three appearances per node. The uncached fallback applies the cutoff, so cache availability changes semantics. Fix direction: calculate range-specific counts and percentages consistently for both paths.

3. **Medium — request timeouts silently abandon loading and saving states.** `frontend/js/admin-security-intelligence.js:266` labels every AbortError as cancellation; callers ignore cancelled requests. A JavaScript runtime probe triggered the actual timeout and observed `aborted=true`, message `Request cancelled`. This affects analysis loading, threshold polling, and outcome recording; the latter can remain disabled on “Recording…” with unknown save status. The fallback without `AbortSignal.any` also ignores the timeout when a caller signal exists. Fix direction: distinguish timeout from intentional cancellation, surface a retry/error state, and maintain timeout support with caller cancellation.

4. **Medium — the blind-review flag forgets an earlier reveal.** `frontend/js/admin-security-intelligence.js:2096` resets `mlObservationRevealed` on every threat render. Runtime reproduction: render assessment, click its actual reveal handler, then render the same assessment ID again; the flag changes from true to false. `recordOutcome` sends this flag to the server, so a subsequent outcome can be labelled blind after the reviewer already saw the ML observation. Fix direction: retain reveal state per assessment and define its persistence across reloads.

## Additional source-level concern

`backend/core/security_intelligence_service.py:1259–1275`: the uncached network fallback sorts identity IDs but keeps a percentage computed from the original target identity. This can attach a percentage to the wrong denominator and differ from the recently corrected stored-relationship calculation. This concern was identified in code review; no separate runtime reproduction was performed in this audit.

## Request and storage trace

- Startup loads pipelines, capabilities and paginated identity search. Photo selection calls `/api/search/by-image`.
- Network, patterns and anomaly panels call `/api/security/network`, `/patterns`, and `/anomalies/{id}`. Advanced panels call threshold jobs, trajectory prediction and correlation endpoints; maps use the shared identity-map component.
- Threat loading calls `GET /api/security/threat/{id}`. Despite GET, it persists a threat assessment and its signal records (deduplicated within a time bucket); this side effect is explicitly documented in the backend. The canonical creation endpoint is POST `/api/security/assessments`.
- Outcome recording calls POST `/api/security/assessments/{id}/resolve`. It changes workflow status and can create a manual, unreviewed outcome label with the supplied blind/revealed flag. History reads stored assessments.
- This page does not enroll a face image. Its photo-search endpoint reads and decodes the upload, searches embeddings, and writes search audit metadata including a SHA-256 image hash. The reviewed handler does not save the uploaded image as an enrolled identity photo. Displayed snapshots refer to existing stored images.

## Validation and limits

- `tests/test_security_intelligence_system.py`: **38 passed** in an isolated stack.
- `tests/test_risk_platform.py` plus the custom database probe: **24 passed, 1 failed**. The failure is `test_migration_applied_and_indexed`, which hard-codes historical revision `fcc3d4e5f6a7`; the isolated database successfully migrated to current head `ff17b8c9d0e1`. Its later assertions were not reached.
- Two JavaScript runtime reproductions confirmed timeout misclassification and reveal-state reset. These use a mocked DOM, not a full browser.
- Custom database probe assertions deliberately verify the faulty behavior; passing these reproductions does not mean the bugs are fixed. Controlled decision outputs isolate the real route/persistence mismatch; no claim that production currently contains this exact 10/90 example.
- All synthetic database writes ran in disposable regression stacks, which were torn down. No production threat generation, outcome resolution or threshold-learning job was triggered for this audit.
- No full browser interaction, large-scale load test, or external security penetration test was performed. Passing existing tests does not establish that every page flow is correct.

Evidence: `logs/security-intelligence-audit-20260921/test_probes.py`, `ui-probe.cjs`; regression logs `logs/regression/regression_413378_ced0c147.log` and `regression_428514_494be9c0.log`. Logs/probes under `logs/` are local ignored artifacts.

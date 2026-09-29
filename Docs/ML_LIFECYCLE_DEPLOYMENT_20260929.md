# ML lifecycle fixes — deployed 29 September 2026

Release: `a1dff754987b6ba44fe45793035e964d7cb98520`.

API and ML worker were rebuilt with sealed clean-source provenance and recreated using the existing production/GPU Compose overlays. Only these two services were restarted. No model was activated, no training was started, and no database migration was needed.

Both running containers verified the same build:
`58d377d3367043974a4e70efeea17d2856109290fc5dfdd2215d75386eca716b`.

Validation:

- 152 isolated backend tests passed against the rebuilt worker image and disposable PostgreSQL, before rollout.
- Both running containers passed `backend/ml/build_provenance.py check`.
- The deployed notebook exporter exposes feature-selection, tuning, metrics, drift and service-connection source references; its first cell remains the actual-data preview.
- HTTPS `/health/ready` returned HTTP 200, `status=ready`, at `2026-09-29T07:01:51.800543` UTC. Database, models, cache, queue and offline policy were healthy. All 34 background services had no degraded or stale entries.
- Both Docker health checks passed. No models are selected for shadow/approved/production use. Automatic drift and Optuna remain disabled.

Rollback image tags retained:

- `face_detector_prod-face_recognition:before-lifecycle-20260929`
- `face_detector_prod-ml_worker:before-lifecycle-20260929`

This deployment supersedes the pre-deployment status in the lifecycle audit and the previously exported notebook introduction. The existing executed notebook outputs remain historical snapshot/preprocessing checks; deployment does not establish predictive accuracy for candidates with empty validation/test sets.

The full audit is [ML_LIFECYCLE_AUDIT_20260929.md](ML_LIFECYCLE_AUDIT_20260929.md).

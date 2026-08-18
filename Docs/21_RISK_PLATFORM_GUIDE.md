# Risk Platform Guide

The unified risk engine, persisted threat assessments, learned thresholds,
timezone-aware analytics, rate limiting, and multi-worker locking.

## Score interpretation — read this first

**Scores are NOT probabilities.** Every risk number in this product is a
weighted heuristic on a normalized 0–100 range. A score of 80 does **not**
mean "80% probability of threat". Every API payload says so explicitly:

```json
{
  "score_type": "heuristic",
  "is_probability": false,
  "calibration_status": "uncalibrated",
  "model_version": "risk-engine-v1",
  "limitations": ["..."]
}
```

`risk_model_versions.calibration_status` / `calibration_data` is the
interface for a FUTURE validated calibration. It stays `uncalibrated` until a
real calibration study is recorded — never fabricate one.

## Unified severity bands

One band map for every score (`backend/core/risk_engine.py`):

| Score  | Severity  |
|--------|-----------|
| 0–24   | low       |
| 25–49  | moderate  |
| 50–74  | high      |
| 75–100 | critical  |

Three scoring **profiles** share the engine: `identity_threat` (the threat
tab), `network_node` (graph node risk), `movement_map` (map overlay).
Weights/thresholds live in the `risk_model_versions` table (active row per
profile, cached ~60 s) — editing weights is a data change, not a deploy.
Compiled defaults in `risk_engine.DEFAULT_MODELS` are the documented
fallback and mirror the seeded rows.

## Persisted assessments

Every generated assessment lands in `threat_assessments` (signals also
normalized into `risk_signal_results`). Duplicate prevention is a DB unique
constraint on the idempotency key
`{subject_type}:{subject_id}:{model_version}:{time bucket}` — bucket width
`ASSESSMENT_DEDUP_WINDOW_MINUTES` (default 5). Concurrent workers collapse
onto one row.

Endpoints (admin + CSRF on mutations, rate-limited):

- `POST /api/security/assessments` — create/recalculate (201 new, 200 dedup)
- `GET  /api/security/assessments` — list; filters: `person_id`,
  `pipeline_id`, `location_name`, `severity`, `status`, `date_from`,
  `date_to`; pagination `page`/`page_size`
- `GET  /api/security/assessments/{id}`
- `GET  /api/security/assessments/history/identity/{identity_id}`
- `POST /api/security/assessments/{id}/acknowledge` → status `acknowledged`
- `POST /api/security/assessments/{id}/resolve` (`resolution_status`, `notes`)
- `POST /api/security/assessments/{id}/reopen` (resolved only)
- `GET  /api/security/risk-model` — model + threshold versions + bands

`GET /api/security/threat/{identity_id}` (unchanged URL) now also persists
its result and returns `assessment_id`, `severity`, `confidence`, the engine
payload, and the honesty labels. `threat_level` values are now the unified
severities (`moderate` replaces `medium`; `minimal` no longer occurs).

## Learned thresholds

The learning job writes **candidates** to `learned_thresholds` — nothing is
consumed until an admin activates it:

- `GET  /api/security/learned-thresholds` — list (filter by signal/status)
- `POST /api/security/learned-thresholds/{id}/activate` — refuses candidates
  under `THRESHOLD_MIN_SAMPLES_FOR_ACTIVATION` samples; retires the previous
  active row (re-activate it to roll back)

Consumption (co-appearance analysis) resolves with precedence:

```
location-specific → pipeline-specific → global learned → static config default
```

Each persisted assessment records the provenance in `threshold_version`,
e.g. `multi_camera_time_window_minutes=pipeline:cam-3@v2,...`.

## Timezones

- **Storage is UTC, always.** Nothing in the DB changes.
- Business-hour questions (off-hours patterns, anomaly hour buckets) convert
  through the camera's IANA timezone: `pipelines.timezone` (e.g.
  `Asia/Beirut`), falling back to `DEFAULT_SITE_TIMEZONE` (default `UTC`).
- Conversions use `zoneinfo` — daylight-saving is handled by the timezone
  database. Fixed numeric offsets are never used.
- Anomaly items return both `utc_time` and `local_time` plus the timezone.

## Anomaly context

`anomaly-context-v3`: local hour is judged against the matching day bucket —
workday / weekend (`WEEKEND_DAYS`, Monday=0) / holiday (`ANOMALY_HOLIDAYS`,
ISO dates). Thin buckets fall back to the overall baseline at reduced,
reported confidence; too little history returns an explicit
insufficient-baseline result, never a fake score. The baseline is rolling
(`ANOMALY_BASELINE_MAX_DAYS`). The heuristic lives in
`time_context.BucketedHourBaseline` — replace that class to plug in a
statistical/ML model.

## Rate limiting

Redis-backed fixed-window counters per scope, per user AND per IP.
Exceeding either returns `429` with `Retry-After`. Config:
`API_RATE_LIMIT_ENABLED`, `RATE_LIMIT_DEFAULT_PER_MINUTE` (300),
`RATE_LIMIT_HEAVY_PER_MINUTE` (60 — recalculation, graph/anomaly analysis,
threat, correlation, trajectory, exports, chatbot conversation writes).
**Redis unavailable → fail-open** with an approximate per-worker in-memory
fallback and a warning log/metric; do not rely on the fallback for hard
guarantees.

## Multi-worker operation

Cross-worker single-flight for the intelligence jobs (relationship
calculation, threshold learning) and assessment computation uses Redis locks
(`SET NX PX` + ownership-token Lua release, bounded TTL, renewal helper) —
`backend/core/distributed_lock.py`. Assessment WRITES are idempotent at the
database level regardless of locks.

**`WORKERS>1` requires Redis.** Without it, startup logs a warning and locks
degrade to per-process guards. Other subsystems (runtime settings apply,
webhook dedup, FAISS autosave, training single-flight) remain process-local
— the config guard still flags `WORKERS>1` unless `ALLOW_MULTI_WORKER=true`
is set deliberately.

## Observability

- `fr_assessments_total{result}` — created / deduplicated / persist_error /
  acknowledged / resolved / reopened
- `fr_assessment_scoring_seconds` — scoring + persistence duration
- `fr_distributed_lock_contention_total{lock}` — lock conflicts
- `fr_rate_limit_rejections_total{scope}` — 429s
- Structured logs: `[ASSESSMENT]`, `[RISK_AUDIT]`, `[LOCK]`, `[RATE_LIMIT]`
  — ids, durations and outcomes only; never embeddings, tokens or images.

## Migration

```bash
# apply (additive; no data deleted)
bash docker/run_alembic_migration.sh upgrade
# or directly:
docker exec -w /app/alembic face_recognition_api python -m alembic upgrade head
# verify
docker exec -w /app/alembic face_recognition_api python -m alembic current   # → a7b8c9d0e1f2
```

New tables: `threat_assessments`, `risk_signal_results`,
`risk_model_versions` (seeded with `risk-engine-v1` per profile),
`learned_thresholds`. New column: `pipelines.timezone`.

## New environment variables

See `.env.example` — `DEFAULT_SITE_TIMEZONE`, `WEEKEND_DAYS`,
`ANOMALY_HOLIDAYS`, `ANOMALY_BASELINE_MAX_DAYS`,
`ASSESSMENT_DEDUP_WINDOW_MINUTES`, `THRESHOLD_MIN_SAMPLES_FOR_ACTIVATION`,
`API_RATE_LIMIT_ENABLED`, `RATE_LIMIT_DEFAULT_PER_MINUTE`,
`RATE_LIMIT_HEAVY_PER_MINUTE`. All are also live-tunable via the settings
page (registry `_DYN`).

## Running with multiple workers

```bash
# 1. Ensure Redis is configured (REDIS_URL) — required for cross-worker locks
# 2. Acknowledge the remaining process-local caveats:
WORKERS=4 ALLOW_MULTI_WORKER=true docker compose -f docker/docker-compose.cpu.yml up -d
# 3. Watch startup logs: a WARNING appears if WORKERS>1 without Redis
```

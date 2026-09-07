# Background Jobs Verification Report

**Date:** 2026-09-04  **Target:** production (`https://face-detector.internal`)
**Method:** read-only. Every job was checked through the same API the admin
page (`/admin/background-tasks`) uses, plus the task-history table, the ML
worker heartbeat table, and the process logs since the last restart (15:13).
**No code was changed and no job was triggered** for §1–§7. The findings were
then fixed the same day — §8 records what changed and the evidence that it works.

## Verdict

**Every background job is running, on schedule, under admin control, with a
100 % success record.** 34 runs in the last 30 days, 34 completed, 0 failed,
0 overdue, 0 stuck, 0 retries. Two findings are worth knowing (§5); neither
is a job failing.

| Admin-page counters (`/api/tasks/stats`, 30-day window) | |
|---|---|
| total / completed / failed | 34 / 34 / **0** |
| running / scheduled / overdue / cancelled | 0 / 0 / 0 / 0 |
| success rate | **100 %** |
| task-history rows with `success=false`, `error_message`, `is_overdue`, `retry_count>0` | **0 / 0 / 0 / 0** |
| `status` vs `effective_status` mismatches (stuck-detection) | none |

---

## 1. The jobs, one by one

### Scheduled jobs (in the API process, `worker=api-pid1`)

| Job | Schedule | Last run | Next run | Status |
|---|---|---|---|---|
| **Data retention** | 1 min after boot, then every 24 h (`DATA_RETENTION_DAYS=30`, `CLEANUP_INTERVAL_HOURS=24`) | 15:15:48 today, completed, 0.04 s | 2026-09-05 15:15 (exposed by `/api/admin/retention/status`) | ✅ working |
| **Log cleanup** | 1 min after boot, then every `LOGS_LIFE_TIME_HOURS=48` cycle | 15:24 today, completed | next cycle | ✅ working |
| **Identity retention** | 1 min after boot, then every `IDENTITY_CLEANUP_INTERVAL_HOURS=24` (snapshots 90 d, embeddings 12 mo) | 11:47 today, completed | ~11:47 tomorrow | ✅ working |
| **Identity clustering** | `CLUSTER_STARTUP_DELAY_HOURS=7.0` after boot, then every `CLUSTER_INTERVAL_HOURS=24` | 2026-09-03 18:49, completed | **~22:13 tonight** (7 h after the 15:13 restart) | ✅ working — see §5.1 (**fixed, §8.1: now due 18:49**) |
| **Task-history pruning** | inside data retention (`TASK_HISTORY_RETENTION_DAYS=30`) | with data retention | with data retention | ✅ working |

Every one of these logged a clean start at 15:13:48 after the last restart, with
the parameters above, and no WARNING or ERROR of its own since.

### Durable jobs (the `ml_worker` container, `worker=ml-worker-production-primary`)

| Job | Schedule | Last run | Next run | Status |
|---|---|---|---|---|
| **ML drift check** | every `ML_DRIFT_CHECK_INTERVAL_HOURS=24`; the worker re-checks "is one due?" every `ML_JOB_MAINTENANCE_SECONDS=30` | 05:24 today, completed, 1.39 s | ~05:24 tomorrow | ✅ working |
| **ML feature computation / ML training** | on demand from the ML-Ops page | never requested yet | — | ✅ registered, idle |

The worker itself is alive: `ml_worker_heartbeats` shows `status=idle`, heartbeat
**2 s old** against a 10 s cadence and a 60 s lease. The drift schedule is
**durable** — it is derived from the last completed drift row in the database,
so a worker restart does not reset the 24 h clock (unlike the in-process jobs,
§5.1).

### Continuous loops (no history rows by design — they never "complete")

| Loop | Evidence since restart | Status |
|---|---|---|
| Map availability refresh | `refresh loop started (every 300.0s)`; map gate `PRODUCTION READY: all 13 rules pass` | ✅ |
| Face tracker | `started (window: 0s, max_memory: 2000MB)` | ✅ — see §5.2 (**fixed, §8.4: now 30s**) |
| pgvector index autosave + reconcile | `pgvector_index is available and will be used` | ✅ |
| Production cache manager + cache metrics | started, 0 warnings | ✅ |
| Batch writer / flush | started, 0 warnings | ✅ |
| Event-loop lag monitor | started | ✅ |
| Queue workers | **15/15 started** | ✅ |
| Backup loop (`backup` container) | `interval=86400s retention=14d`; `backup succeeded` at 14:53 today (676 K dump, checksummed) | ✅ |

### On-demand jobs (not timers)

| Job | Triggered by | Status |
|---|---|---|
| Relationship calculation | Intelligence page / `POST` via the intelligence API, with a job id | never requested on this install — idle, ✅ |

---

## 2. Admin control — are the jobs "held by the administrator"?

Yes. Every control path is admin-only (`require_role(["admin"])`).

| The admin can… | How |
|---|---|
| See every run, status, duration, worker, error, progress | `/admin/background-tasks` → `GET /api/tasks/history`, `/stats`, `/alerts` |
| Cancel a running task | `POST /api/tasks/{id}/cancel` |
| Retry a failed task | `POST /api/tasks/{id}/retry` |
| Run data retention now (with dry-run) | `POST /api/admin/retention/run` (button on the page) |
| Run log cleanup now | `POST /api/logs/cleanup` |
| Run a manual cleanup | `POST /api/cleanup/manual` |
| Run an ML drift check now | `POST /api/ml/drift/run` |
| Tune every interval / retention window | Settings page — all listed in §1 are `next_job_run` or `api_restart` settings |

**What the admin could not do from the UI (fixed, §8.2):** trigger identity
clustering or identity retention *on demand*. Both ran only on their timers.
Now `POST /api/clustering/run` and `POST /api/identity-retention/run` exist,
admin-only, and show up on the background-tasks page like every other run.

---

## 3. Health checks that back the verdict

- `deploy.sh doctor` → no problems found
- ML worker container healthy; API container healthy; GPU on CUDA after restart
- 0 `application_failed` rows; 0 HTTP 5xx on task endpoints
- Every WARNING/ERROR in the API log since restart is accounted for (§4)

---

## 4. Log noise that is NOT a job problem

| Message | What it actually is |
|---|---|
| `⚠️ KNOWN: No pgvector embeddings found` | fresh install; no identities enrolled yet |
| `⛔ Refused runtime change to security-critical setting …` (×4) and `N admin-modified setting(s) need the container recreated` | **false alarm** — the four rows (`DATABASE_URL`, `JWT_SECRET_KEY`, `POSTGRES_PASSWORD`, `REDIS_URL`) are seeded mirrors *equal* to their env values (`overridden=false`, `valid`). The boot check keyed on `source=database` rather than on an actual difference. **Fixed, §8.3** — the boot log is now clean. |
| `[MAP_AVAILABILITY] boot verification failed (Read-only file system)` | the boot verifier trying to write its cache into the read-only `map-data` mount; the verdicts file already exists and the map gate passes 13/13 |

---

## 5. Two findings worth knowing

### 5.1 Frequent restarts starve identity clustering — fixed, §8.1

Clustering waits **7 hours after every boot** before its first run, and its timer
is in-memory. The API was restarted about six times today (deploys, GPU fix,
credential change, verification), so clustering **never reached its 7-hour mark
today** — its last run is 2026-09-03 18:49. It will run ~22:13 tonight if the
API stays up.

This is the designed behaviour (the delay avoids clustering during a fresh
boot's load), but during a day of maintenance it means the job silently does
not run, and there is no "run now" button to compensate. Data retention, log
cleanup and identity retention are unaffected — they run one minute after boot.

### 5.2 The face tracker's dedup window is 0 seconds — fixed, §8.4

`FACE_TRACKING_WINDOW_SECONDS` is **0** in production (the `config.py` default;
compose does not set it), while the tracker's own docstring describes the
default as 30 s. With 0, the expiry test `now - last_seen > window` is true for
every re-sighting, so in-memory duplicate suppression is effectively off and
every detection is written. The tracker is running correctly for that value —
but it is probably not the value intended. Setting it is an `api_restart`
change on the Settings page.

---

## 6. Why `/api/tasks/upcoming` is empty

By design. The notifier writes a "scheduled" row only in the
`BACKGROUND_TASK_NOTIFICATION_LEAD_TIME_SECONDS=60` window before a run starts
(then it becomes "running"). Between 24-hour runs there is nothing in that
window, so the list is empty. The next-run *times* are known (§1); they are just
not pre-registered as rows. The one job that publishes its next run directly is
data retention (`next_run_at` in `/api/admin/retention/status`).

---

## 7. What was not tested, and why

- No job was triggered on demand: the request was to observe, not change state.
  Every job has either run on its own schedule today (or yesterday, for
  clustering) with a recorded completed row, or is on-demand and unused.
- Long-interval behaviour (the 24 h and 48 h cycles firing *without* a restart
  in between) is inferred from the schedule logic and the drift job's durable
  design, not observed end to end today — the API was not up for 24 h
  uninterrupted.

---

## 8. Fixes applied after this report (2026-09-04, same day)

All five recommendations were implemented with the smallest change that makes
each one true, deployed with `sudo ./deploy.sh upgrade --yes` (PASS, 14/14
stages, 31/31 health checks), and verified live. Nothing else in the job code
was touched.

| # | Finding | Change | Evidence after deploy |
|---|---|---|---|
| 8.1 | §5.1 restarts reset the 7 h clustering delay | `durable_initial_delay()` in `backend/core/service_supervisor.py`: reads the job's last *completed* row from task history and shortens the boot delay to "time until the next 24 h slot" (floor 60 s, never longer than the configured delay). Used by `identity_clustering.start()` and `identity_retention.start()` — 1 line each. | Last run 2026-09-03 18:49 → boot delay **2.89 h** (due 18:49 today) instead of 7 h (22:56). No history → the configured delay, unchanged behaviour on a fresh install. |
| 8.2 | §2 no "run now" for clustering / identity retention | `backend/routes/management.py`: `POST /api/clustering/run` and `POST /api/identity-retention/run` (admin router, `202 Accepted`, background task, per-job `asyncio.Lock` → `409` while one is already running, `409` if clustering is disabled). They call the existing `_run_cycle()`, so the notifier/history/error handling are the scheduled path's, not a copy. | Unauthenticated → 401. Both → 202; second clustering call while running → 409. Both rows `completed` at 15:58:46 (the 60 s notification lead is inside the cycle by design) — clustering "0 identities, not enough to cluster" on the empty DB, retention "0 deleted". |
| 8.3 | §4 false "need the container recreated" / "Refused runtime change" at boot | `backend/core/runtime_settings.py` `hydrate_from_db`: skip a DB row whose value already **equals** the effective value (a seeded mirror), before the security-critical / apply-mode branches. One `if … continue`. | Boot log: `Hydration complete: no admin-modified settings in database`; 0 × "need the container recreated", 0 × "Refused runtime change" (was 5). |
| 8.4 | §5.2 tracker dedup window 0 s | `docker/docker-compose.prod.yml` (+ cpu) set `FACE_TRACKING_WINDOW_SECONDS: "30"`; documented in `.env.example`. A stale seeded row (0) in `application_settings` was corrected to 30 through the Settings API so it does not shadow the new env value. | `Face tracker started (window: 30s` on every boot since. On a fresh production install there is no stale row. |
| 8.5 | ML worker liveness not visible on the ML-Ops page | `backend/routes/ml_ops.py`: `GET /api/ml/overview` gains `worker` — `{available, lease_seconds, alive_count, workers:[{worker_id, hostname, status, current_job_id, heartbeat_at, heartbeat_age_seconds, alive}]}` from `ml_worker_heartbeats`, using `ML_JOB_LEASE_SECONDS` as the aliveness bound. | `worker.available=true, alive_count=1, heartbeat_age_seconds≈1.5`. |

**One regression found and fixed during the same deploy:** moving the database
credentials to secret files left `scripts/backup/backup.sh` without a
`PGPASSWORD` when run through `deploy.sh backup` (`fe_sendauth: no password
supplied`). It now resolves the password from `/run/secrets/backup_db_password`
itself, exactly as `backup-loop.sh` already did. `deploy.sh backup` → PASS.

**Contract suites after the change:** 177 passed, 0 failed (env/volume/restart/
dead-knob/config-single-source). Not committed — per the standing rule.

---

## 9. Configuration change (2026-09-04, 16:12 UTC): data retention 30 → 365 days

Operator request: keep data for one year, not 30 days. Changed, not fixed —
the job itself was working.

| Setting | Was | Now | Where |
|---|---|---|---|
| `DATA_RETENTION_DAYS` | 30 | **365** | `docker-compose.prod.yml`, `docker-compose.cpu.yml`, `.env.example`; live via the Settings API (`next_job_run`) |
| `TASK_HISTORY_RETENTION_DAYS` | 30 (config default, not set in compose) | **365** | same three files; live via the Settings API |

Evidence: a manual **dry run** (`POST /api/admin/retention/run?dry_run=true`,
deletes nothing) logged `cutoff=2025-09-04 (days=365) candidate_rows=0`. After
the container recreate: `Data retention started (keep: 365 days)`, both settings
`stored=365 env=365 effective=365 source=environment`, hydration clean. Contract
suites 30 passed / 0 failed. Next scheduled run 2026-09-05 16:03 UTC.

**Visible on the home page (2026-09-04):** a read-only *Data retention* panel
shows every window above — detections/events, task history, search history,
audit log, identity snapshots, identity embeddings, application logs, backups —
with the cleanup cadence, fed by the `retention` block of `GET /api/stats`
(`_retention_policy()` in `backend/routes/stats.py`). Contract:
`tests/test_home_retention_panel.py`.

Unchanged on purpose (not asked): identity retention (snapshots 90 d, embeddings
12 months), backup retention 14 d, log cleanup 48 h, and the `config.py`
defaults (still 30 — compose is the production value; a fresh install reads it).


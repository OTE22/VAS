# Background Tasks Monitor & Retention Testing

**Updated: 2026-07-25.** How background jobs are tracked, monitored and tested.

## Task lifecycle

```
scheduled -> running -> completed
scheduled -> running -> failed
scheduled -> cancelled            (atomic UPDATE for scheduled tasks;
                                   cooperative flag honored between batches
                                   for running jobs)
```

`overdue` is **virtual**, never stored: `status='scheduled' AND scheduled_time < now`.
`upcoming` is the complement (`scheduled_time >= now`). An old scheduled row is
NEVER shown as upcoming.

Table `background_task_history` (migration `d1e2f3a4b5c6`) carries:
`job_id` (unique), `progress_percent`, `result` (JSONB, final), `details`
(JSONB, live progress), `retry_count`/`max_retries`, `error_code`/`error_message`,
`created_by_user_id`, `request_id`, `correlation_id`, `worker_name`, `hostname`,
`updated_at`. Indexed: status, task_type, scheduled_time, created_at, job_id,
correlation_id.

## APIs

```
GET  /api/tasks/stats                       SQL aggregates (admin) — no row scans
GET  /api/tasks/history?page=1&page_size=20&task_type=&status=&date_from=&date_to=&search=&sort_by=&sort_order=
     -> {items, total, page, page_size, total_pages}     (server-side everything)
     status also accepts virtual filters: upcoming | overdue
GET  /api/tasks/alerts                      starting <=60s; stable alert_instance_id
GET  /api/tasks/{id}                        full detail (non-admins get safe error refs)
POST /api/tasks/{id}/cancel                 admin; atomic or cooperative
POST /api/tasks/{id}/retry                  admin; failed/cancelled data_retention only

GET  /api/admin/retention/status            flattened + per-key stored/effective/source
POST /api/admin/retention/run               BODY {"dry_run": true}                       -> 202 {job_id, task_id}
POST /api/admin/retention/run               BODY {"dry_run": false,
                                                  "confirmation": "DELETE_EXPIRED_DATA"} -> 202
     wrong/missing confirmation -> 400; concurrent run -> 409
```

All monitoring endpoints send `Cache-Control: no-store, no-cache, must-revalidate`.

Retention runs execute as **background jobs** (`backend/core/retention_job.py`),
never inside the HTTP request. The API returns 202 immediately; the frontend
polls `GET /api/tasks/{task_id}` (2s, capped 5min) for progress and result.

## Overlap protection (three layers)

1. synchronous in-loop reservation in `retention_job.py` (closes the
   check-then-spawn race in the handler)
2. `retention_manager._run_lock` (asyncio, in-process)
3. Postgres advisory lock 823451 — held on a **dedicated engine connection**
   for the whole run. (Advisory locks are connection-bound; the ORM session's
   connection rotates through the pool across per-batch commits, so locking
   through the session could unlock on the wrong connection and leak the lock.
   Closing the dedicated connection always releases it.)

## Retention run safety

- selection identical for dry-run and real run; dry run deletes NOTHING and
  reports candidate_rows/candidate_files/existing_files/missing_files/
  estimated_freed_bytes/sample_candidate_ids
- 24h safety floor (records newer than 1 day never touched regardless of config)
- batched deletes (1000/batch) with per-batch progress + cancel checks
- file deletion in an executor; path-traversal guard (must resolve under
  STORAGE_DIR, never FACES_DIR); missing files counted, never fatal
- Redis `map:*` cache invalidated after real deletions (bounded SCAN)
- settings (`DATA_RETENTION_DAYS`, `CLEANUP_INTERVAL_HOURS`, …) read at RUN
  START — admin changes apply at the next job run without restart

## Docker logs

```bash
docker logs -f face_recognition_api | grep -E "\[TASK\]|\[RETENTION\]"
docker logs --since=10m face_recognition_api | grep "\[TASK\]"
```

```
[TASK] job_id=retention-ab12cd34 task_type=data_retention status=running progress=50
[RETENTION] job_id=retention-ab12cd34 dry_run=false cutoff=... candidate_rows=15
            deleted_rows=15 deleted_files=14 missing_files=1 freed_bytes=18450320
            duration_seconds=3.2 failures=0 status=completed
```

Never logged: passwords, JWTs, cookies, auth headers, API keys, embeddings
(enforced by the SensitiveDataFilter in utils/logging.py + tests).

## Manual retention test

```bash
TOKEN=$(curl -s -X POST http://localhost/api/auth/login -H 'Content-Type: application/json' \
  -d '{"username":"admin","password":"***"}' | jq -r .access_token)

# 1. Status
curl -s -H "Authorization: Bearer $TOKEN" http://localhost/api/admin/retention/status | jq

# 2. Dry run (deletes nothing)
curl -s -X POST -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"dry_run": true}' http://localhost/api/admin/retention/run | jq
# -> poll the returned task_id:
curl -s -H "Authorization: Bearer $TOKEN" http://localhost/api/tasks/<task_id> | jq .result

# 3. Real run (typed confirmation REQUIRED)
curl -s -X POST -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"dry_run": false, "confirmation": "DELETE_EXPIRED_DATA"}' \
  http://localhost/api/admin/retention/run | jq
```

Automated equivalents (backdated seeds, missing-file tolerance, overlap 409,
confirmation rejection, progress lifecycle):

```bash
docker exec face_recognition_api python -m pytest tests/test_background_tasks.py tests/test_settings_system.py -q
```

## Frontend (`frontend/js/admin-background-tasks.js?v=tasks-2`)

- recursive-setTimeout pollers with in-flight locks (tasks+stats 30s,
  alerts 15s — ONE alert poller, retention status 60s); no setInterval anywhere
- AbortController on every request; all timers/controllers cleaned on pagehide
- polling slows 4x while `document.hidden`, refreshes instantly on return
- server-side pagination only (never downloads 500 rows)
- tabs: All / Upcoming / Running / Completed / Failed / Cancelled / Overdue
- accessible task-details modal (focus management, Escape, aria) — no alert()
- status CSS classes allowlisted (`VALID_STATUSES`); DOM built with
  createElement/textContent — no innerHTML
- "Enable Task Alerts" button -> ONE shared AudioContext (autoplay-policy
  compliant), preference in localStorage; alert dedup by alert_instance_id
  in sessionStorage
- retention panel: status grid, Dry Test, Run Now (typed DELETE_EXPIRED_DATA
  confirmation modal), single-job progress monitor

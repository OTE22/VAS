# Scoped Read_From update — 2026-09-15

## Included

Imported selected changes from `/home/itdirect-ai/Desktop/Read_From`:

- Background job monitoring, durable schedules across restarts, expiry maintenance and task links.
- Log-source selection, cleanup previews, recorded cleanup outcomes and complete-record retention.
- `/admin/intelligence`: filter validation, automatic refresh on change/Enter, and backend queries that honor the requested thresholds instead of returning unfiltered cached relationships.
- Alerts: trigger-history request ordering, modal state, acknowledgement button state, explanatory text and styles. Date-expiry updates are atomic.

Existing chatbot/SSO integration, enrollment decisions, detection replay fixes, alert API validation and offline/intranet deployment changes were preserved. Older source files that would revert those changes were excluded. The incoming trigger loading text was corrected from `Loading triggers?` to `Loading triggers…`.

## Verification

- 100 backend tests passed in a disposable development container against a fresh temporary PostgreSQL database.
- 9 frontend tests passed using the local Node 24 image.
- No test container had access to production database volumes or the external network.
- Imported Python files passed syntax parsing; `git diff --check` passed.
- Hash checks confirmed protected chatbot/SSO files were unchanged.

Backend suites: durable_task_schedules, expiry_maintenance, job_monitoring, log_record_retention, optional_log_sources, retention_schedule, logging_pipeline, related_identity_filters, background_job_fixes.
Frontend suites: admin_logs_frontend, admin_intelligence_filters, live_alert_trigger_modal.

The API and ML worker were rebuilt and redeployed on 2026-09-15. See deployment verification below.

## Git runtime files

`.gitignore` now also covers downloaded model weights and generated map binary formats. Removed 33 already-ignored runtime paths from tracking; every file remains on disk for offline operation.

After refreshing remote refs and receiving approval, removed only `map-data/source/`, `map-data/production/` and `map-data/metadata/content_verdicts.json` from seven unpublished local commits. Commit metadata/messages and all other tree contents were preserved. Existing working changes were preserved.

Original history is available on local branch `backup/local-before-runtime-cleanup-20260915`. Keep that branch local: it intentionally retains the large files. Do not push it with `--all` or `--mirror`.

The cleaned main history has no outgoing blob over 50 MiB. No push was performed. At verification, `main` had seven local commits and `origin/main` had four separate commits; those remote changes still need integration before a normal push. History cleanup does not resolve that divergence.

## Imported files

- `backend/core/background_maintenance.py`
- `backend/core/data_retention.py`
- `backend/core/enrollment_service.py`
- `backend/core/identity_clustering.py`
- `backend/core/identity_retention.py`
- `backend/core/job_monitoring.py`
- `backend/core/live_alert_service.py`
- `backend/core/log_cleanup.py`
- `backend/core/log_records.py`
- `backend/core/log_retention.py`
- `backend/core/service_supervisor.py`
- `backend/core/watchlist_service.py`
- `backend/core/intelligence_service.py`
- `backend/ml/worker.py`
- `backend/routes/logs.py`
- `backend/routes/retention.py`
- `backend/utils/migrations.py`
- `backend/services/image_processing.py`
- `utils/logging.py`
- `gunicorn.conf.py`
- `frontend/admin/background-tasks.html`
- `frontend/js/admin-background-tasks.js`
- `frontend/admin/logs.html`
- `frontend/js/admin-logs.js`
- `frontend/css/logs.css`
- `frontend/admin/settings.html`
- `frontend/js/admin-settings.js`
- `frontend/admin/intelligence.html`
- `frontend/js/admin-intelligence.js`
- `scripts/dev/db_tunnel.py`
- `tests/test_durable_task_schedules.py`
- `tests/test_expiry_maintenance.py`
- `tests/test_job_monitoring.py`
- `tests/test_log_record_retention.py`
- `tests/test_optional_log_sources.py`
- `tests/test_retention_schedule.py`
- `tests/admin_logs_frontend.test.cjs`
- `tests/test_logging_pipeline.py`
- `tests/test_intelligence_system.py`
- `tests/admin_intelligence_filters.test.cjs`
- `tests/test_related_identity_filters.py`
- `Docs/central-logging.md`
- `frontend/admin/live-alerts.html`
- `frontend/js/admin-live-alerts.js`
- `frontend/css/admin-live-alerts.css`
- `tests/live_alert_trigger_modal.test.cjs`

## Deployment verification — 2026-09-15

Rebuilt production GPU API and ML worker images, plus the CPU migration image, using the existing production Compose files and GPU allocation. Recreated only the API and ML worker and reloaded Nginx. No migration job was run for this code-only update. Persistent volumes, VMS and chatbot containers were preserved.

- API and ML worker Docker health: healthy.
- API readiness: ready; database, models, cache, queue and offline policy healthy.
- ML worker database heartbeat: healthy.
- Trusted HTTPS checks through Nginx: health endpoints and updated intelligence, alerts and logs JavaScript returned 200.
- Protected admin pages redirect unauthenticated requests; background monitoring returns 401 without authentication.
- Authenticated page checks could not be completed: available administrator credentials were rejected. No password or account settings were changed.
- VMS and chatbot containers remain healthy.

Rollback image tags: `face_detector_prod-face_recognition:before-scoped-updates-20260915` and `face_detector_prod-ml_worker:before-scoped-updates-20260915`.

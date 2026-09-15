# Central application logs

The Docker `LOG_DIR` (`/var/log/face-recognition`) is mounted here in development.
Production uses the shared `logs_data` volume at the same container path.

| File/directory | Owner/purpose |
| --- | --- |
| `app.log`, `app.log.1` etc. | API worker; numbered files are rotations, not different log systems |
| `server.log` | Gunicorn master, separate from the API writer |
| `ml-worker.log`, `ml-job.log` | Single ML scheduler and its serialized child |
| `migrations.log`, `image-processing.log` | Standalone application entry points |
| `audit/`, `regression/`, `smoke/` | Test and audit diagnostics |
| `deploy/`, `map_builds/`, `diagnostics/` | Deployment, map-build and host-tool diagnostics |
| `legacy/` | Relocated historical logs without an active writer |

Use `/admin/logs` for operational sources, all-file inventory and the latest cleanup result.
Use `/admin/background-tasks?task_type=log_cleanup` for cleanup history.

ML job, migration and image-processing CLI logs are optional until their
respective process runs. Their absence returns `source_available=false` with
an explanation; the viewer displays “No logs available yet” and checks again
on refresh. Required API/scheduler/server files, unreadable files and missing
log directories remain errors. Existing rotated history can still be viewed
when an optional active file is absent.

API record retention uses `LOGS_LIFE_TIME_HOURS`: the writer locks and rotates
its active file, then removes expired complete records from closed rotations.
Tracebacks stay with their records. Recent records remain visible, so successful
cleanup does not mean zero pages. Unknown leading text is preserved.
Shared multi-worker active files are refused by this operation rather than rewritten.
Other processes retain ownership of their active files; their closed rotations
are handled by the central cleanup. Size-based rotation limits still apply.

Diagnostic files use `DIAGNOSTIC_LOG_RETENTION_DAYS`; legacy server access/error
logs use `LEGACY_LOG_RETENTION_DAYS`. Audit datasets/models/reports are preserved.
Cleanup previews are read-only (`GET /api/logs/cleanup-preview`). Cleanup outcomes
report records removed, files removed, space freed and failures separately.

Infrastructure container stdout/stderr (PostgreSQL, Redis, nginx, etc.) remains
Docker-managed with its configured rotation; it is not an application `.log`
file and must not be moved from Docker's storage directories.

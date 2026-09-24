# VAS — face recognition and camera operations

VAS ingests camera frames, detects and matches faces, stores identity/appearance
evidence, and provides a browser console for review, search, monitoring and local
analysis. Offline operation requires the installed images, models, maps and
local services to be present.

## Start here

- [Move this same server to the offline intranet](Docs/offline-deployment.md)
- [Page-by-page documentation](Docs/README.md)
- [End-to-end operator demo](Docs/demo.md)
- [Backup and recovery, including current restore limitations](Docs/backup-and-recovery.md)

The page guides use their webpage filenames: `dashboard.md`, `home.md`,
`known.md`, `unknown.md`, `ml-ops.md`, and so on. Each explains features,
controls, permissions and a demo with expected results.

## Production layout

The production base is [docker/docker-compose.prod.yml](docker/docker-compose.prod.yml).
GPU deployments layer [docker/docker-compose.prod.gpu.yml](docker/docker-compose.prod.gpu.yml)
and may have installation-specific overrides. Preserve the configuration actually
used by the installed server. The production project name is `face_detector_prod`;
development uses a separate project and volumes.

Nginx provides HTTPS access. PostgreSQL, Redis, local inference/model services,
ML worker, map server, backups and monitoring support the application.
Production secrets are mounted from `secrets/`; Compose substitutions use the
installation's environment configuration, including `docker/.env`. Defaults and
validation live in [config.py](config.py). [Settings](Docs/settings.md) explains
runtime edits and the distinction between saved and effective values.

For the same-server move, use the offline checklist rather than reinstalling.
Do not delete named volumes or run `docker compose down -v` on real data.
VMS and the LAF-AI chatbot are separate deployments; the same-server checker
includes their expected containers and HTTPS endpoints.

## Development and maintenance

Development Compose files are [CPU](docker/docker-compose.cpu.yml) and its
[GPU overlay](docker/docker-compose.gpu.yml). They are not substitutes for the
production files. Inspect `bash deploy.sh --help` for the deployment wrapper's
commands before using an installation or maintenance action.

Production disables `/docs` and `/redoc`. API declarations are in
`backend/routes/` and `sql_agent/api/`; generated explorers are available only
where explicitly enabled. Frontend files are under `frontend/`.

Run data-changing tests against an isolated test stack. The regression entry
point is [scripts/run_regression_isolated.sh](scripts/run_regression_isolated.sh);
review its help/configuration before execution. Do not point the full suite at
production data.

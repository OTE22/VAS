# September 14 deployment and feature update

This records the changes deployed on 2026-09-14 and their operational impact.
The source was `/home/itdirect-ai/Desktop/Read_From`; the unreadable USB copy was
not deployed. Changes were merged with the existing SSO, GPU and network work.
Credentials, model weights and runtime databases were not imported. The original
application deployment applied 136 files; subsequent chatbot localization was a
separate deployment in `/home/itdirect-ai/vas-assistant`.

## Features and implementation

| Area | Current behavior | Details |
|---|---|---|
| Known Faces | `/admin/known` lists known identities with search, status filtering, sorting and pagination. Administrators can rename, activate/deactivate, add enrollment photos, and review deletion impact before confirming deletion. Merged profiles cannot be edited. | `backend/routes/known_faces.py`, `backend/core/known_face_lifecycle.py`, `frontend/js/admin-known.js` |
| Enrollment | Target identity review protects against enrolling a photo into the wrong person. Photo management remains part of enrollment. | `backend/core/enrollment_service.py`, `backend/routes/enrollment_review.py` |
| Quick Search | Searches the largest detected face, explains group-photo handling, ranks distinct people using compatible embeddings, and applies camera/date filters before limiting results. Search does not enroll or merge people. | [Quick Search](QUICK_SEARCH.md) |
| Promotion | Preserves identity ID and appearance history; similar faces or names require review. Refusal audits capture actor values before rollback so the audit survives ORM expiration. | [Promotion reliability](PROMOTION_RELIABILITY.md) |
| Merging and suggestions | Shared mutation locking, stale-state review and notifications keep identity changes and operator views consistent. | [Merge reliability](MERGE_RELIABILITY.md), [suggestion workflow](SUGGESTION_WORKFLOW.md) |
| Detection durability | Batches are spooled before acceptance; replay deduplicates by detection UUID. Duplicate detection is checked before identity mutation. Pending evidence is protected from cleanup. | [Background reliability](BACKGROUND_JOBS_RELIABILITY.md), [storage layout](DETECTION_STORAGE_LAYOUT.md) |
| Appearance history | Exact event/detection provenance and timestamp source are retained alongside historical appearances. | [Event timestamps](UNKNOWN_PERSON_EVENT_TIMESTAMPS.md) |
| Background jobs and backups | Supervised consumers, expiry sweeps, safer cleanup, log retention and backup health checks expose failures to operators. | [Background reliability](BACKGROUND_JOBS_RELIABILITY.md), [backup and restore](11_BACKUP_AND_RESTORE.md) |
| SQL agent | Updated reasoning, catalog/database handling, diagnostics and evaluation tools are included in the VAS source update. | [Agent architecture](57_AGENT_ARCHITECTURE.md), [SQL audit brief](99_SQL_AGENT_AUDIT_BRIEF.md) |

Known-face mutations require administrator access and CSRF validation. Rename
records an audit in the same transaction and does not change vectors or camera
timestamps. Permanent deletion requires both a confirmation name and a preview
token; follow the UI impact preview before proceeding.

## Database and runtime

The deployed Alembic head is `fdd4e5f6a7b8`, following `fcc3d4e5f6a7`.
The migration adds `event_id`, `detection_id`, `detection_uuid`, `location_name`
and `timestamp_source` to `identity_appearances`, a unique event constraint,
a detection foreign key with `ON DELETE SET NULL`, and a camera/latest index.
Legacy rows retain their IDs and times; only exact, unambiguous surviving
matches receive detection links. Database-head verification remains fail-closed.

Production API and ML worker use GPU runtimes. The base production Compose file
uses `docker/Dockerfile.cpu`; `docker/docker-compose.gpu.yml` overrides the API
and worker build definitions. The migration service can use the CPU image because
schema migration does not require GPU inference. Seeing `requirements-cpu.txt`
in a migration build does not mean the API uses CPU inference. GPU builds install
`requirements-gpu.txt`; both hardware manifests include `requirements-base.txt`.

This deployment reused existing runtime dependencies because the manifests
matched. Code-update images were built, production migrated, API/worker/backup
containers recreated, and nginx validated and reloaded. SCRFD and ArcFace startup
logs confirmed CUDA execution on this host. New frontend files are served by the
updated application; refresh cached pages after deployment.

## Tracking People and client access

The active navigation destination remains `/api/sso/laf-ai/launch`, handing off
to `https://armyeye-chatbot`. The legacy `/tracking-people` application and its SQL
agent remain in the repository; editing those files alone does not update the
separately deployed LAF-AI chatbot.

On the server and other computers, both `face-detector.internal` and
`armyeye-chatbot` must resolve to the server's reachable LAN address. Configure
LAN DNS, or hosts entries on each client, and trust the internal CA on each client.
A hostname resolution failure happens before application login; rebuilding the
application cannot fix missing client DNS. Use the configured HTTPS hostname for
SSO so its origin and certificate agree. A static server IP can be the target of
these DNS records; raw-IP browsing is not a substitute for the configured SSO URL.

The separate chatbot repository provides `tools/set-static-ip.sh`. Fill the
required interface, CIDR, gateway and DNS fields in `tools/static-ip.env`, then
run `sudo ./tools/set-static-ip.sh --dry-run` from that repository before running
`sudo ./tools/set-static-ip.sh`. This migrates an already working wired deployment;
it does not provision a fresh installation. Static-IP migration was not performed
as part of this update.

The script snapshots configuration/certificates under `/var/lib/vas-static-ip`,
preserves the previous NetworkManager profile and chatbot containers, updates
the address/certificate/proxy, and requires successful HTTPS login and gate-health
checks. Failure or handled termination restores the previous network profile,
files and containers. SIGKILL, power loss, or unavailable Docker/NetworkManager
can require manual recovery from the retained snapshots. It does not create DNS
records or update other computers' hosts files. No image rebuild is needed for
that address migration.

## Chatbot language fixes

The installed data-agent plugin shipped a Chinese preset name and persona.
English deployment templates now live in the chatbot repository at
`src/deploy/data-agent/preset.yml` and `agent.cordis.yml`. The startup script
`src/deploy/localize-data-preset.sh`, called by `vas-entrypoint.sh`, installs them
into `$DSH_HOME/.agent-presets/data-agent` when absent or when the recognized
original Chinese preset is present. Existing customized English files are kept.
The original preset files are backed up once under
`$DSH_HOME/backups/data-agent-before-english`.

The mode label is **Data Mode**. The persona defaults to English, supports Arabic
for Arabic requests or an explicit language request, and retains database/tool
constraints. The `vas-assistant:english-ui` image was built and tagged as
`vas-assistant:local`; the assistant and gate were restarted and checked healthy.

Previously saved conversation titles are independent of the preset. The Chinese
title shown in the screenshot was renamed through `session/rename` to
**Database Registration Count**, and verified through `session/list`. Refreshing
the browser updates its sidebar, header and tab. Renaming needs no rebuild and
does not translate or delete existing messages. Future title generation follows
the language of the messages. Use the supported rename operation for old titles;
do not rewrite durable session logs. No authentication tokens belong in docs.

## Validation and recovery

The deployment record is [RESULT.md](../logs/deployment-update-20260914/RESULT.md).
It records 579 passing tests in the initial expanded run with seven failures,
333 passing tests in a follow-up with two remaining audit failures, and 12 passing
focused tests after the final audit fix. All observed failures were resolved in
focused reruns; these counts overlap and are not a single full-suite pass.
JavaScript and shell syntax checks passed; no production browser visual test was
run. Feature notes imported from the source may describe earlier validation;
RESULT.md is the record for this host's deployment.

Fresh isolated databases migrated to the new head and schema parity checks
passed. Production health, authenticated known-face/privilege routes and TLS
served frontend assets passed. Chatbot localization was checked for fresh and
existing presets, idempotence, backups and preservation of custom English labels;
the saved title rename was verified separately through the API.

Before the database migration, backup `/backups/20260914T083334Z` in the backup
service passed database/storage/artifact checksum verification. Previous VAS
images are retained with tag `before-read-from-20260914`; original code and
protected configuration are retained in `logs/deployment-update-20260914`.
For recovery, follow [backup and restore](11_BACKUP_AND_RESTORE.md) and the
[deployment runbook](04_DEPLOYMENT_RUNBOOK.md). An older application image alone
is not a complete rollback across a schema change: restore a compatible backup,
images and configuration together. Restoring this backup discards subsequent
writes; preserve current data first. The migration downgrade removes provenance
columns, so it is not a lossless restoration of the deployed state.

The chatbot's prior image is `vas-assistant:before-language-fix-20260914`.
Its preset files persist independently of its image: restoring the image alone
will not undo localization. A language rollback also needs the backed-up preset
files and a restart with the matching entrypoint. A conversation-title rollback
uses `session/rename`; neither image rollback nor rebuilding changes saved titles.

## Documentation maintenance

The route index was regenerated from source after the deployment; its generator
now closes each Markdown code block. The OpenAPI reference was regenerated during
the application update. The settings-consumer document remains explicitly dated
September 6 and is a live-settings snapshot, not a September 14 configuration
export. New cleanup settings and their restart behavior are documented in
[background reliability](BACKGROUND_JOBS_RELIABILITY.md).

For this documentation update, 21 offline assertions from
`tests/test_documentation_consistency.py` passed using the workspace paths.
The live OpenAPI comparison was not rerun. Documentation changes require no
application rebuild or service restart.

# Documentation Index

**Face Recognition / Multi-Camera Surveillance System**
**ITDIR-AI DEPARTMENT**

86 files (this index plus 85 documents), all listed below. Start at the repository root
[`README.md`](../README.md) if you have never run this system.

Documents that are superseded are marked. **Do not follow operational
instructions from a document marked superseded.**

---

## The five documents that matter for running it

| Document | Use it for |
|---|---|
| [`61_DEPLOYMENT_RUNBOOK.md`](61_DEPLOYMENT_RUNBOOK.md) | **The production authority.** Server prep through go-live. Anything that contradicts it is wrong. |
| [`72_ADMIN_CHEAT_SHEET.md`](72_ADMIN_CHEAT_SHEET.md) | Day-to-day commands on one page. |
| [`73_TROUBLESHOOTING.md`](73_TROUBLESHOOTING.md) | Something broke — decision tree with exact commands. |
| [`74_SECURITY_CHECKLIST.md`](74_SECURITY_CHECKLIST.md) | Hardening before go-live. |
| [`75_API_REFERENCE.md`](75_API_REFERENCE.md) | Every endpoint, generated from the running code. |

Supporting: [`60_BACKUP_AND_RESTORE.md`](60_BACKUP_AND_RESTORE.md),
[`36_CONFIGURATION_GUIDE.md`](36_CONFIGURATION_GUIDE.md),
[`46_MAP_SERVICE_GUIDE.md`](46_MAP_SERVICE_GUIDE.md).

---

## ⚠️ Corrections that override older documents

**1. Vector search is pgvector, not FAISS.** Many documents here still describe
FAISS as the live index; they predate the migration. The binding contract is
[`70_VECTOR_INDEX_CONTRACT.md`](70_VECTOR_INDEX_CONTRACT.md) — PostgreSQL is
authoritative, the index is a disposable acceleration layer. It supersedes 30,
33 and the FAISS sections of 12 and 37.

**2. Interactive API docs are disabled in production.** `/docs`, `/redoc` and
`/openapi.json` are served only in development. Use
[`75_API_REFERENCE.md`](75_API_REFERENCE.md), or extract the spec from the
production process without exposing a route — see
[`74_SECURITY_CHECKLIST.md`](74_SECURITY_CHECKLIST.md) §9.

**3. Four `MAP_*` names are query parameters, not settings.**
`MAP_ENABLE_SECURITY_FEATURES`, `MAP_DETECT_PATTERNS`, `MAP_SHOW_RISK_HEATMAP`
and `MAP_SHOW_TIMELINE` do not exist as environment variables. They are
per-request parameters on `GET /api/identities/{id}/map`. Setting them in
`.env` does nothing.

**4. Two compose files, two project names.** Development is
`face_detector_dev`, production `face_detector_prod`. The GPU files are
**overrides**, layered with a second `-f`, never run alone.

**5. Password rotation is enforced, not advisory.** An account holding a
password somebody else chose — the deployment seed, or one an administrator
typed when creating or resetting the account — can sign in but can do nothing
else. Login succeeds with `rotation_required: true` and
`redirect_url: "/change-password"`; every other endpoint returns
`403 PASSWORD_ROTATION_REQUIRED`. Only `GET /api/auth/me`,
`POST /api/auth/logout`, `POST /api/auth/change-password` and
`GET /change-password` work until the password is changed, and changing it ends
every other session for that account. The authority is
[`61_DEPLOYMENT_RUNBOOK.md`](61_DEPLOYMENT_RUNBOOK.md) §7; it supersedes the
first-login instructions in **03** and **05**, which described the change as
something to do afterwards through the admin panel — a route that is now closed
to a pending account.

---

## ⚠️ API clients: start here (v6.0.0)

Before upgrading any client or script, read
[`50_API_DOCUMENTATION.md`](50_API_DOCUMENTATION.md) → *Platform-Wide
Conventions* and the **Migration Checklist (v5 → v6)**, plus the in-app
tutorial (Admin → Tutorial), which always matches the running build.

**Headlines:** cookie-authenticated mutations require an
`X-Requested-With: XMLHttpRequest` header · a login can succeed and still leave
the session gated — check `rotation_required` and handle
`403 PASSWORD_ROTATION_REQUIRED` · expensive operations return
`202 + job_id` instead of blocking · model training produces a reviewable
*candidate* rather than replacing the live model · watchlist deletion is
reversible · the social-network graph is always bounded · generated map HTML
must be embedded in a sandboxed iframe.

---

## Getting started (01–05)

- **[01_QUICK_START.md](01_QUICK_START.md)** — fastest path to a running dev system
- **[02_DOCKER_QUICK_START.md](02_DOCKER_QUICK_START.md)** — Docker quick start
- **[03_ADMIN_SETUP_GUIDE.md](03_ADMIN_SETUP_GUIDE.md)** — creating admin users, bootstrap admin, forced first-login password change. ⚠️ `admin123` is the **dev CPU stack only** and is rejected by the production config guard
- **[04_SETUP_NVIDIA_DOCKER.md](04_SETUP_NVIDIA_DOCKER.md)** — GPU / NVIDIA container toolkit
- **[05_MIGRATION_GUIDE.md](05_MIGRATION_GUIDE.md)** — database migrations

## Using the system (06–11)

- **[06_PROMOTE_AND_MERGE_GUIDE.md](06_PROMOTE_AND_MERGE_GUIDE.md)** — promoting and merging identities
- **[07_UNKNOWN_FACES_CENTER_COMPLETE_GUIDE.md](07_UNKNOWN_FACES_CENTER_COMPLETE_GUIDE.md)** — the Unknown Faces Center, in full
- **[08_IDENTITY_API_FRONTEND_GUIDE.md](08_IDENTITY_API_FRONTEND_GUIDE.md)** — identity features in the frontend
- **[09_HOW_MERGE_SUGGESTIONS_WORK.md](09_HOW_MERGE_SUGGESTIONS_WORK.md)** — how merge suggestions are produced
- **[10_AUTO_CLEAN_AND_CLUSTER_JOBS_GUIDE.md](10_AUTO_CLEAN_AND_CLUSTER_JOBS_GUIDE.md)** — automated background jobs
- **[11_SYSTEM_CAPABILITIES.md](11_SYSTEM_CAPABILITIES.md)** — capability overview
- **[11_GRAPH_BASED_CLUSTERING.md](11_GRAPH_BASED_CLUSTERING.md)** — graph clustering *(duplicate `11_` prefix)*

## Technical reference (12–17)

- **[12_README.md](12_README.md)** — executive / architecture overview. Contained the whole document twice (798 duplicated lines, plus a stray shell heredoc); deduplicated and its SQLite-era stack claims corrected to PostgreSQL + pgvector. Operational detail lives in the root README and 61.
- **[13_README_GPU.md](13_README_GPU.md)** — GPU support
- **[15_PERFORMANCE_OPTIMIZATION.md](15_PERFORMANCE_OPTIMIZATION.md)** — tuning
- **[16_PERSISTENCE_STATUS.md](16_PERSISTENCE_STATUS.md)** — what is persisted where
- **[87_DATABASE_RELATIONSHIPS.md](87_DATABASE_RELATIONSHIPS.md)** — how all 59 tables connect: master inventory, physical-FK vs logical relationships, complete 90-row FK matrix (verified against the live database at head `f6a7b8c9d0e1`), Mermaid ERDs per domain, workflow traces, cascade/orphan analysis, §O = every 2026-08-15 concern and how the corrective pass resolved it. Read before touching any FK or writing a cross-table join.
- **[88_CORRECTIVE_PASS_REPORT.md](88_CORRECTIVE_PASS_REPORT.md)** — the 2026-08-16 corrective implementation pass: what changed (migrations `c2d3e4f5a6b7`…`f6a7b8c9d0e1`, single detection write path, exact embedding lineage, merge/watchlist transfer, one search audit writer, ML-Ops lineage, Alembic-only boot), the acceptance matrix with PASS/FAIL per check, regression/isolation results, OpenAPI diff (`api-snapshots/`), browser smoke matrix, config inventory, remaining limitations.
- **[17_CAPACITY_VERIFICATION.md](17_CAPACITY_VERIFICATION.md)** — capacity analysis for 50+ cameras

## Operations, access and webhooks (18–28)

- **[18_AUDIT_LOGGING_GUIDE.md](18_AUDIT_LOGGING_GUIDE.md)** — audit logging
- **[19_BLOCKED_USERS.md](19_BLOCKED_USERS.md)** — blocking users
- **[20_NAVBAR_COMPONENT_GUIDE.md](20_NAVBAR_COMPONENT_GUIDE.md)** — navbar component
- **[21_WEBHOOK_TROUBLESHOOTING.md](21_WEBHOOK_TROUBLESHOOTING.md)** — webhook problems
- **[21_RISK_PLATFORM_GUIDE.md](21_RISK_PLATFORM_GUIDE.md)** — risk scoring platform *(duplicate `21_` prefix)*
- **[22_WEBHOOK_DEBUG.md](22_WEBHOOK_DEBUG.md)** — webhook debugging
- **[23_CLEANUP_UNKNOWN_IDENTITIES_GUIDE.md](23_CLEANUP_UNKNOWN_IDENTITIES_GUIDE.md)** — cleanup procedures
- **[24_SETTINGS_MANAGEMENT_GUIDE.md](24_SETTINGS_MANAGEMENT_GUIDE.md)** — managing settings from the web UI
- **[25_API_AUTHENTICATION_GUIDE.md](25_API_AUTHENTICATION_GUIDE.md)** — API authentication and tokens
- **[26_USER_PIPELINE_ACCESS_GUIDE.md](26_USER_PIPELINE_ACCESS_GUIDE.md)** — per-user pipeline access
- **[27_HOW_TO_GRANT_UNKNOWN_FACES_ACCESS.md](27_HOW_TO_GRANT_UNKNOWN_FACES_ACCESS.md)** — granting Unknown Faces access
- **[28_MULTI_IDENTITY_MERGE_GUIDE.md](28_MULTI_IDENTITY_MERGE_GUIDE.md)** — multi-identity merge
- **[29_PROMOTION_FLOW_EXPLAINED.md](29_PROMOTION_FLOW_EXPLAINED.md)** — the UNKNOWN → KNOWN promotion flow, step by step

## Recognition pipeline and vector search (31–35, 44, 68, 70–71, 76–77)

- **[31_DYNAMIC_PROMOTION_FLOW.md](31_DYNAMIC_PROMOTION_FLOW.md)** — dynamic promotion
- **[32_50_CAMERAS_SCALABILITY_ANALYSIS.md](32_50_CAMERAS_SCALABILITY_ANALYSIS.md)** — 50-camera scalability
- **[34_SCRFD_ARCFACE_INTEGRATION_PIPELINE.md](34_SCRFD_ARCFACE_INTEGRATION_PIPELINE.md)** — SCRFD + ArcFace integration
- **[35_IDENTITY_RECOGNITION_DEBUG_GUIDE.md](35_IDENTITY_RECOGNITION_DEBUG_GUIDE.md)** — debugging recognition failures
- **[35_PGVECTOR_INTEGRATION.md](35_PGVECTOR_INTEGRATION.md)** — pgvector integration *(duplicate `35_` prefix)*
- **[44_BACKEND_PATH_NORMALIZATION_BEST_PRACTICE.md](44_BACKEND_PATH_NORMALIZATION_BEST_PRACTICE.md)** — path normalization
- **[70_VECTOR_INDEX_CONTRACT.md](70_VECTOR_INDEX_CONTRACT.md)** — **the current** vector-index contract
- **[71_IMAGE_INGESTION_WORKFLOW.md](71_IMAGE_INGESTION_WORKFLOW.md)** — what happens when an image arrives, across all three paths, and why multiple photos per person improve recall
- **[68_KNOWN_FACES_STARTUP_FLOW.md](68_KNOWN_FACES_STARTUP_FLOW.md)** — how known faces are loaded at startup
- **[76_DETECTION_DATABASE_WRITES.md](76_DETECTION_DATABASE_WRITES.md)** — the exact rows one detection creates, table by table
- **[87_DATABASE_RELATIONSHIPS.md](87_DATABASE_RELATIONSHIPS.md)** — the whole schema as one connected model (relationship-oriented companion to 76)
- **[77_UNKNOWN_FACES_ARCHITECTURE.md](77_UNKNOWN_FACES_ARCHITECTURE.md)** — how unknown faces are stored, searched and promoted (the internals behind 07)

## Configuration (24, 36)

- **[36_CONFIGURATION_GUIDE.md](36_CONFIGURATION_GUIDE.md)** — every setting explained. `config.py` is the source of truth.
- **[78_SETTINGS_RUNTIME_MATRIX.md](78_SETTINGS_RUNTIME_MATRIX.md)** — which settings apply at runtime vs need a restart

## Merge, search and intelligence (37–45)

- **[37_ADVANCED_MERGE_FLOW_GUIDE.md](37_ADVANCED_MERGE_FLOW_GUIDE.md)** — multi-pipeline merge, deep dive
- **[38_SEARCH_BY_IMAGE_GUIDE.md](38_SEARCH_BY_IMAGE_GUIDE.md)** — search by uploading a photo
- **[39_ADVANCED_SEARCH_INTELLIGENCE_GUIDE.md](39_ADVANCED_SEARCH_INTELLIGENCE_GUIDE.md)** — watchlists, live alerts, intelligence. **The canonical advanced-search document** (two status snapshots that duplicated it were removed).
- **[40_LIVE_ALERTS_GUIDE.md](40_LIVE_ALERTS_GUIDE.md)** — live search alerts
- **[40_ML_INTEGRATION.md](40_ML_INTEGRATION.md)** — ML integration *(duplicate `40_` prefix)*
- **[41_PIPELINE_AWARE_ML_CLUSTERING_GUIDE.md](41_PIPELINE_AWARE_ML_CLUSTERING_GUIDE.md)** — pipeline-aware ML clustering
- **[41_ML_CLUSTERING.md](41_ML_CLUSTERING.md)** — ML clustering *(duplicate `41_` prefix)*
- **[42_ML_SIMILARITY_MODEL_GUIDE.md](42_ML_SIMILARITY_MODEL_GUIDE.md)** — trainable similarity model
- **[42_ML_PGVECTOR_INTEGRATION.md](42_ML_PGVECTOR_INTEGRATION.md)** — ML + pgvector *(duplicate `42_` prefix)*
- **[45_SECURITY_INTELLIGENCE_GUIDE.md](45_SECURITY_INTELLIGENCE_GUIDE.md)** — security intelligence and threat analysis

## Maps and cross-camera tracking (46, 48, 57–59, 67, 85–86, 89)

The map stack is MapLibre GL JS in the browser over a local Martin tile
server. Everything about it — architecture, styles, availability, content
verification, settings — is documented **once**, in 46.

- **[46_MAP_SERVICE_GUIDE.md](46_MAP_SERVICE_GUIDE.md)** — **canonical map documentation.** Supersedes the retired 47/49/55, which described a server-side Folium renderer that no longer exists.
- **[48_SECURITY_INTELLIGENCE_MAP_FEATURES.md](48_SECURITY_INTELLIGENCE_MAP_FEATURES.md)** — pattern detection and risk scoring (the analysis behind the overlays)
- **[49_BEHAVIORAL_ANOMALY_ML_STATE.md](49_BEHAVIORAL_ANOMALY_ML_STATE.md)** — behavioral-anomaly ML: shadow contract, engineering vs scientific readiness, evidence path to a validated signal mapping
- **[89_OFFLINE_MAP_REMEDIATION.md](89_OFFLINE_MAP_REMEDIATION.md)** — the placeholder-tile defect, what it proved about structural checks, and the fail-closed content ledger that replaced them
- **[85_MAP_MIGRATION_INVENTORY.md](85_MAP_MIGRATION_INVENTORY.md)** — MapLibre GL JS + Martin migration: inventory, frozen feature contract, verification results *(historical record of the cutover)*
- **[86_MAP_DATASET_ACQUISITION.md](86_MAP_DATASET_ACQUISITION.md)** — Lebanon map datasets: OSM streets (Planetiler), Sentinel-2 satellite, Copernicus DEM — acquisition, build, atomic install, licences, air-gap transfer
- **[57_MULTI_CAMERA_SOCIAL_NETWORK_ANALYSIS.md](57_MULTI_CAMERA_SOCIAL_NETWORK_ANALYSIS.md)** — cross-camera social network analysis
- **[58_CROSS_CAMERA_RESEARCH_COMPARISON.md](58_CROSS_CAMERA_RESEARCH_COMPARISON.md)** — comparison with published research
- **[59_ADVANCED_SNA_ENHANCEMENTS.md](59_ADVANCED_SNA_ENHANCEMENTS.md)** — advanced SNA enhancements
- **[67_SNA_ENHANCEMENTS_QUICK_START.md](67_SNA_ENHANCEMENTS_QUICK_START.md)** — 5-minute SNA quick start

## API and tutorials (50–51, 61–62, 75)

- **[75_API_REFERENCE.md](75_API_REFERENCE.md)** — **generated** from the running OpenAPI document: all operations, parameters, bodies and status codes. Cannot drift; a test enforces it. **Use this for the endpoint list.**
- **[50_API_DOCUMENTATION.md](50_API_DOCUMENTATION.md)** — hand-written API guide with worked examples and the v5→v6 migration checklist. Where it disagrees with 75, 75 is correct.
- **[51_TUTORIAL_GUIDE.md](51_TUTORIAL_GUIDE.md)** — step-by-step tutorials
- **[83_API_ENHANCEMENTS_GUIDE.md](83_API_ENHANCEMENTS_GUIDE.md)** — API enhancements *(renumbered from 61 so the deployment runbook owns that number unambiguously)*
- **[62_HOW_TO_USE_ENHANCEMENTS.md](62_HOW_TO_USE_ENHANCEMENTS.md)** — using the enhancements

## Avatars (52–54)

- **[52_ANIMATED_AVATAR_GUIDE.md](52_ANIMATED_AVATAR_GUIDE.md)** — animated avatar feature
- **[53_ANIMATED_AVATAR_ROUTE_VERIFICATION.md](53_ANIMATED_AVATAR_ROUTE_VERIFICATION.md)** — how to verify the avatar routes
- **[54_AVATAR_VISIBILITY_AND_TIMING.md](54_AVATAR_VISIBILITY_AND_TIMING.md)** — visibility and timing

## Production operations (60–63, 69, 72–74, 93)

- **[91_ML_JOB_WORKER_ARCHITECTURE.md](91_ML_JOB_WORKER_ARCHITECTURE.md)** — durable ML job queue and the ml_worker service
- **[92_RELATIONAL_ML_MODELS.md](92_RELATIONAL_ML_MODELS.md)** — the four governed model families; rules stay authoritative
- **[93_PRODUCTION_RUNBOOK.md](93_PRODUCTION_RUNBOOK.md)** — orientation map: every artifact (secrets, certs, volumes, DB roles, JWT) in order of use
- **[61_DEPLOYMENT_RUNBOOK.md](61_DEPLOYMENT_RUNBOOK.md)** — **the production authority**
- **[60_BACKUP_AND_RESTORE.md](60_BACKUP_AND_RESTORE.md)** — backup, restore, disaster-recovery drill
- **[63_REDIS_CACHING_GUIDE.md](63_REDIS_CACHING_GUIDE.md)** — Redis caching
- **[69_CLEAR_DATABASE_GUIDE.md](69_CLEAR_DATABASE_GUIDE.md)** — ⚠️ **development only.** Clearing data and stored images.
- **[72_ADMIN_CHEAT_SHEET.md](72_ADMIN_CHEAT_SHEET.md)** — every day-to-day command
- **[73_TROUBLESHOOTING.md](73_TROUBLESHOOTING.md)** — troubleshooting decision tree
- **[74_SECURITY_CHECKLIST.md](74_SECURITY_CHECKLIST.md)** — production security checklist
- **[80_ALEMBIC_IN_DOCKER.md](80_ALEMBIC_IN_DOCKER.md)** — running Alembic inside Docker
- **[79_BACKGROUND_TASKS.md](79_BACKGROUND_TASKS.md)** — background job lifecycle, overlap protection and retention safety
- **[80_ALEMBIC_IN_DOCKER.md](80_ALEMBIC_IN_DOCKER.md)** — running Alembic by hand inside the container
- **[81_SQL_AGENT_QUERY_HISTORY.md](81_SQL_AGENT_QUERY_HISTORY.md)** — SQL agent query history, sessions and memory
- **[90_AGENT_ARCHITECTURE.md](90_AGENT_ARCHITECTURE.md)** — how the agent decides what to do: planner vs dispatcher, artifacts and their lineage, the ownership boundary
- **[82_RECOGNITION_LOGGING_WALKTHROUGH.md](82_RECOGNITION_LOGGING_WALKTHROUGH.md)** — logging walkthrough
- **[71_IMAGE_INGESTION_WORKFLOW.md](71_IMAGE_INGESTION_WORKFLOW.md)** — the unified storage layout

## Deep dives (64–66)

- **[64_IDENTITY_RECOGNITION_EXPLANATION.md](64_IDENTITY_RECOGNITION_EXPLANATION.md)** — recognition end to end
- **[65_IMAGE_QUALITY_ANALYSIS.md](65_IMAGE_QUALITY_ANALYSIS.md)** — image quality scoring
- **[66_IMAGE_SECURITY_ANALYSIS.md](66_IMAGE_SECURITY_ANALYSIS.md)** — direct URL vs Base64 image serving
- **[68_KNOWN_FACES_STARTUP_FLOW.md](68_KNOWN_FACES_STARTUP_FLOW.md)** — how known faces load at startup
- **[76_DETECTION_DATABASE_WRITES.md](76_DETECTION_DATABASE_WRITES.md)** — what detection writes to the database
- **[87_DATABASE_RELATIONSHIPS.md](87_DATABASE_RELATIONSHIPS.md)** — the whole schema as one connected model (relationship-oriented companion to 76)
- **[77_UNKNOWN_FACES_ARCHITECTURE.md](77_UNKNOWN_FACES_ARCHITECTURE.md)** — unknown-face lifecycle
- **[29_PROMOTION_FLOW_EXPLAINED.md](29_PROMOTION_FLOW_EXPLAINED.md)** — promotion flow
- **[71_IMAGE_INGESTION_WORKFLOW.md](71_IMAGE_INGESTION_WORKFLOW.md)** — multi-image enrollment
- **[81_SQL_AGENT_QUERY_HISTORY.md](81_SQL_AGENT_QUERY_HISTORY.md)** — embedding model setup
- **[81_SQL_AGENT_QUERY_HISTORY.md](81_SQL_AGENT_QUERY_HISTORY.md)** — SQL agent query history
- **[06_PROMOTE_AND_MERGE_GUIDE.md](06_PROMOTE_AND_MERGE_GUIDE.md)** — UI button workflow


---

## Where to start, by role

**New user** → [`01_QUICK_START.md`](01_QUICK_START.md) →
[`06_PROMOTE_AND_MERGE_GUIDE.md`](06_PROMOTE_AND_MERGE_GUIDE.md) →
[`07_UNKNOWN_FACES_CENTER_COMPLETE_GUIDE.md`](07_UNKNOWN_FACES_CENTER_COMPLETE_GUIDE.md)

**Administrator deploying** → [`61_DEPLOYMENT_RUNBOOK.md`](61_DEPLOYMENT_RUNBOOK.md) →
[`74_SECURITY_CHECKLIST.md`](74_SECURITY_CHECKLIST.md) →
[`60_BACKUP_AND_RESTORE.md`](60_BACKUP_AND_RESTORE.md) →
[`72_ADMIN_CHEAT_SHEET.md`](72_ADMIN_CHEAT_SHEET.md)

**Administrator with a broken system** → [`73_TROUBLESHOOTING.md`](73_TROUBLESHOOTING.md) →
[`21_WEBHOOK_TROUBLESHOOTING.md`](21_WEBHOOK_TROUBLESHOOTING.md) →
[`35_IDENTITY_RECOGNITION_DEBUG_GUIDE.md`](35_IDENTITY_RECOGNITION_DEBUG_GUIDE.md)

**Developer** → root [`README.md`](../README.md) →
[`75_API_REFERENCE.md`](75_API_REFERENCE.md) →
[`70_VECTOR_INDEX_CONTRACT.md`](70_VECTOR_INDEX_CONTRACT.md) →
[`71_IMAGE_INGESTION_WORKFLOW.md`](71_IMAGE_INGESTION_WORKFLOW.md) →
[`36_CONFIGURATION_GUIDE.md`](36_CONFIGURATION_GUIDE.md)

---

## Known state of this directory

Recorded honestly so nobody has to rediscover it:

- **89 files** (this index plus 88 documents), each linked above.
- **19 point-in-time status and fix reports were removed** — they described
  work already finished and were the largest source of contradictory
  statements. Everything still true lives in the feature guides. They remain
  recoverable from git history.
- **Six numbers are used twice** — `11`, `21`, `35`, `40`, `41`, `42`.
  Both members of each pair are listed and labelled. The dangerous one — `61`, where the deployment runbook shared a number
  with an API guide — has been resolved by renumbering the latter to `83`.
- **FAISS appears throughout as if active.** It is not; see correction 1.
- The single source of truth for the product version is `VERSION` in
  [`config.py`](../config.py).

---

## Quick links

- **Tutorial**: `/admin/tutorial` in the web interface
- **API docs**: `/docs`, `/redoc` — development deployments only (correction 2)
- **Root README**: [`../README.md`](../README.md)

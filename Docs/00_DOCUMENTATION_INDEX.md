# Documentation Index

**Face Recognition / Multi-Camera Surveillance System (VAS)**
**ITDIRECT-AI DEPARTMENT**

59 files: this index and 58 documents, **numbered in the order they are meant to be
read**. Start at the repository root [`README.md`](../README.md) if you have never run
this system, then follow Part 1 → Part 8. Each part stands on its own, so an operator
can stop after Part 4 and a developer can jump to Part 6.

Reviewed against the code on **2026-09-12**: 43 documents were removed (they described
deleted code, duplicated a document you are about to read, or were point-in-time
reports), 14 were merged into the survivors, and the rest were corrected. See
[`../DEPLOYMENT.md`](../DEPLOYMENT.md) §19 for the VAS deployment story and §18 for the
LAF-AI chatbot.

---

## If you only read four

| Document | Use it for |
|---|---|
| [`04_DEPLOYMENT_RUNBOOK.md`](04_DEPLOYMENT_RUNBOOK.md) | **The production authority.** Server preparation through go-live. Anything that contradicts it is wrong. |
| [`13_TROUBLESHOOTING.md`](13_TROUBLESHOOTING.md) | Something broke — a decision tree with exact commands. |
| [`14_ADMIN_CHEAT_SHEET.md`](14_ADMIN_CHEAT_SHEET.md) | The day-to-day commands on one page. |
| [`46_WEB_PAGES_REFERENCE.md`](46_WEB_PAGES_REFERENCE.md) | What every page and button does, end to end (API, payload, database effect). |

---

## Part 1 — Orientation (01–03)

Read these before touching anything.

- **[01_SYSTEM_OVERVIEW.md](01_SYSTEM_OVERVIEW.md)** — how the parts fit together and in
  what order they start: the mental model, the directory manifest, secrets, TLS, the four
  database roles, and where the data actually lives.
- **[02_QUICK_START_DEVELOPMENT.md](02_QUICK_START_DEVELOPMENT.md)** — bring a
  **development** stack up with Docker Compose in a few minutes. Production is Part 2.
- **[03_GPU_SETUP.md](03_GPU_SETUP.md)** — NVIDIA drivers, the Container Toolkit, the GPU
  compose overlay, and how to prove the application really chose the GPU.

## Part 2 — Deploy and operate production (04–19)

- **[04_DEPLOYMENT_RUNBOOK.md](04_DEPLOYMENT_RUNBOOK.md)** — the production authority:
  prerequisites, secrets, certificates, `deploy.sh install/upgrade/rollback`, health gates.
- **[05_ADMIN_SETUP.md](05_ADMIN_SETUP.md)** — the first administrator: bootstrap
  credential, forced rotation, creating more admins, recovery.
- **[06_CONFIGURATION_GUIDE.md](06_CONFIGURATION_GUIDE.md)** — every setting that matters,
  what it does, and which ones were removed and why.
- **[07_SETTINGS_MANAGEMENT.md](07_SETTINGS_MANAGEMENT.md)** — the admin Settings page:
  what a save really does, and which keys are deliberately read-only there.
- **[08_SETTINGS_RUNTIME_MATRIX.md](08_SETTINGS_RUNTIME_MATRIX.md)** — per setting, when a
  change takes effect (`apply_mode`): immediately, next request, or only after a restart.
- **[09_SETTINGS_CONSUMERS.md](09_SETTINGS_CONSUMERS.md)** — generated: which modules,
  compose files and scripts consume each setting (`scripts/settings_consumers.py`).
- **[10_SECURITY_CHECKLIST.md](10_SECURITY_CHECKLIST.md)** — hardening before go-live:
  credentials, secrets, TLS, exposed ports, Redis, PostgreSQL, sessions, image serving.
- **[11_BACKUP_AND_RESTORE.md](11_BACKUP_AND_RESTORE.md)** — what is backed up, the
  objectives, taking a backup, and a restore rehearsal that proves it.
- **[12_ALEMBIC_MIGRATIONS.md](12_ALEMBIC_MIGRATIONS.md)** — schema migrations in Docker.
  Alembic is the only schema initializer; there is no `create_all` path.
- **[13_TROUBLESHOOTING.md](13_TROUBLESHOOTING.md)** — the decision tree, from "the
  container will not start" to "cameras get 503" to "the map will not load".
- **[14_ADMIN_CHEAT_SHEET.md](14_ADMIN_CHEAT_SHEET.md)** — start/stop, health, logs,
  database, on one page.
- **[15_CLEARING_DATA.md](15_CLEARING_DATA.md)** — wiping data and stored images
  deliberately, with the maintained scripts, and what survives.
- **[16_REDIS_CACHING.md](16_REDIS_CACHING.md)** — what is cached, for how long, and how
  invalidation works.
- **[17_SCALABILITY.md](17_SCALABILITY.md)** — the 50-camera question answered as
  arithmetic: frames offered vs frames processed, and which knob moves which number.
- **[18_BACKGROUND_TASKS.md](18_BACKGROUND_TASKS.md)** — the task monitor: lifecycle,
  overlap protection, retention runs, and what the admin can cancel or retry.
- **[19_CLEAN_AND_CLUSTER_JOBS.md](19_CLEAN_AND_CLUSTER_JOBS.md)** — the two periodic jobs
  (clustering and retention) in detail, and how cleanup, merging and a wipe differ.

## Part 3 — From camera to identity (20–29)

How a frame becomes a detection, an embedding and a person. Read in order.

- **[20_IMAGE_INGESTION_WORKFLOW.md](20_IMAGE_INGESTION_WORKFLOW.md)** — the three things
  "upload an image" can mean (enroll, ingest, search) and what each one creates.
- **[21_WEBHOOK_TROUBLESHOOTING.md](21_WEBHOOK_TROUBLESHOOTING.md)** — the camera ingest
  endpoint: authentication, body limits, dedup, back-pressure, and how to debug silence.
- **[22_DETECTION_DATABASE_WRITES.md](22_DETECTION_DATABASE_WRITES.md)** — exactly which
  rows a single detection writes.
- **[23_VECTOR_INDEX_CONTRACT.md](23_VECTOR_INDEX_CONTRACT.md)** — **the rule that governs
  every vector**: PostgreSQL is authoritative, the index is a disposable acceleration
  layer. Includes pgvector in practice.
- **[24_IDENTITY_RECOGNITION.md](24_IDENTITY_RECOGNITION.md)** — recognition explained,
  including the two models (SCRFD detection, ArcFace embedding) and why alignment matters.
- **[25_KNOWN_FACES_STARTUP_FLOW.md](25_KNOWN_FACES_STARTUP_FLOW.md)** — what happens to
  known faces when the service starts.
- **[26_RECOGNITION_LOGGING_WALKTHROUGH.md](26_RECOGNITION_LOGGING_WALKTHROUGH.md)** — a
  real recognition traced line by line through the logs.
- **[27_RECOGNITION_DEBUG_GUIDE.md](27_RECOGNITION_DEBUG_GUIDE.md)** — when a known person
  is not recognised: how to find out why.
- **[28_IMAGE_QUALITY_ANALYSIS.md](28_IMAGE_QUALITY_ANALYSIS.md)** — quality scoring, and
  what the pipeline does and does not do to the original image.
- **[29_DATABASE_RELATIONSHIPS.md](29_DATABASE_RELATIONSHIPS.md)** — the 63 tables as one
  connected model, cross-checked against the ORM and the migrations.

## Part 4 — Operator features (30–41)

- **[30_UNKNOWN_FACES_CENTER.md](30_UNKNOWN_FACES_CENTER.md)** — the operator's main
  workspace: reviewing unknown faces, promoting, merging, searching.
- **[31_UNKNOWN_FACES_ARCHITECTURE.md](31_UNKNOWN_FACES_ARCHITECTURE.md)** — why unknown
  faces are handled the way they are, and the production practices behind it.
- **[32_MERGE_SUGGESTIONS.md](32_MERGE_SUGGESTIONS.md)** — how candidates are found: graph
  clustering, pipeline-aware clustering, and where the ML model fits.
- **[33_MULTI_IDENTITY_MERGE.md](33_MULTI_IDENTITY_MERGE.md)** — merging three or more
  identities: the preview, what a merge preserves, the risk gate, and unmerge.
- **[34_PROMOTION_FLOW.md](34_PROMOTION_FLOW.md)** — unknown → known, step by step.
- **[35_SEARCH_BY_IMAGE.md](35_SEARCH_BY_IMAGE.md)** — searching the system with a photo.
- **[36_ADVANCED_SEARCH.md](36_ADVANCED_SEARCH.md)** — multi-face search with quality
  scoring, watchlist checks, exclusions, batch mode and export.
- **[37_LIVE_ALERTS.md](37_LIVE_ALERTS.md)** — being told when a tracked person reappears:
  alerts, windows, channels, triggers and acknowledgement.
- **[38_SECURITY_INTELLIGENCE.md](38_SECURITY_INTELLIGENCE.md)** — the network, patterns,
  anomalies and threats tooling, plus cross-camera co-appearance, activity correlation,
  trajectory prediction and learned thresholds.
- **[39_RISK_PLATFORM.md](39_RISK_PLATFORM.md)** — the risk engine: how a score is built,
  what it does and does not mean, and the assessment lifecycle.
- **[40_MAP_SERVICE.md](40_MAP_SERVICE.md)** — the offline map stack (MapLibre + Martin),
  and the five rules that keep a bad basemap from ever serving.
- **[41_MAP_DATASET_ACQUISITION.md](41_MAP_DATASET_ACQUISITION.md)** — building and
  installing the Lebanon basemaps on a connected preparation machine.

## Part 5 — Users, access and audit (42–45)

- **[42_USER_PIPELINE_ACCESS.md](42_USER_PIPELINE_ACCESS.md)** — camera-scoped access, and
  how granting it turns on the pages a user can see.
- **[43_AUDIT_LOGGING.md](43_AUDIT_LOGGING.md)** — what identity operations record, and
  how to read the trail.
- **[44_BLOCKED_USERS.md](44_BLOCKED_USERS.md)** — how an account gets blocked by the SQL
  agent's guard, and how an administrator restores it.
- **[45_API_AUTHENTICATION.md](45_API_AUTHENTICATION.md)** — sessions, bearer tokens, the
  CSRF header, and the single-sign-on hand-off to the LAF-AI chatbot.

## Part 6 — Interfaces: pages and API (46–49)

- **[46_WEB_PAGES_REFERENCE.md](46_WEB_PAGES_REFERENCE.md)** — every page, every button,
  form and modal: what it triggers, the API call and payload, what the backend writes,
  what the user sees. Includes the LAF-AI chatbot integration.
- **[47_API_INDEX.md](47_API_INDEX.md)** — generated: every route with its handler, auth
  dependencies, whether it writes to the database and whether it audits
  (`scripts/generate_api_index.py`).
- **[48_API_REFERENCE.md](48_API_REFERENCE.md)** — generated from the running
  application's OpenAPI document; request and response shapes.
- **[49_NAVBAR_COMPONENT.md](49_NAVBAR_COMPONENT.md)** — the shared navigation component
  and why the backend, not the frontend, decides which links exist.

## Part 7 — The ML platform (50–54)

Rules remain the decision system; models run in shadow until an administrator promotes one.

- **[50_ML_SIMILARITY_MODEL.md](50_ML_SIMILARITY_MODEL.md)** — the model that scores merge
  suggestions: training, registry, activation and rollback.
- **[51_BEHAVIORAL_ANOMALY_ML.md](51_BEHAVIORAL_ANOMALY_ML.md)** — the anomaly model's
  shadow contract, its evidence path and every named refusal.
- **[52_ML_JOB_WORKER.md](52_ML_JOB_WORKER.md)** — the durable job architecture behind
  training and feature computation.
- **[53_RELATIONAL_ML_MODELS.md](53_RELATIONAL_ML_MODELS.md)** — the relational model
  families and what each is for.
- **[54_MLOPS_OPERATOR_WORKFLOW.md](54_MLOPS_OPERATOR_WORKFLOW.md)** — the operator's path:
  check → prepare → review → decide, with its limits stated plainly.

## Part 8 — The assistant and the chatbot (55–58)

- **[55_LOCAL_DATA_AGENT_ARCHITECTURE.md](55_LOCAL_DATA_AGENT_ARCHITECTURE.md)** — the
  local data agent: what it is, what it may reach, and the impact map.
- **[56_DATA_AGENT_CONFIGURATION.md](56_DATA_AGENT_CONFIGURATION.md)** — configuring it for
  development and for air-gapped production, with the boot guard's checks.
- **[57_AGENT_ARCHITECTURE.md](57_AGENT_ARCHITECTURE.md)** — the legacy SQL agent's
  planner, tools and artifacts (the `/tracking-people` page).
- **[58_SQL_AGENT_QUERY_HISTORY.md](58_SQL_AGENT_QUERY_HISTORY.md)** — its query history
  and per-user memory.

---

## Conventions

- **Generated documents** — `09_SETTINGS_CONSUMERS.md`, `47_API_INDEX.md` and
  `48_API_REFERENCE.md` are produced by scripts. Regenerate them after changing settings or
  routes; do not hand-edit them.
- **Absorbed sections** — where a document ends with "*Absorbed … from the former N_…*",
  that content came from a document retired on 2026-09-12. The old numbers are not coming
  back; nothing was silently dropped.
- **One source of truth for the version and the settings.** The product version and every
  setting's declared default live in [`config.py`](../config.py); no document restates a
  version number of its own. Where a document quotes a default, `config.py` wins.
- **Tests guard this index.** `tests/test_documentation_consistency.py` asserts that every
  document is linked here, that no link dangles, that container names and compose
  invocations in the prose are real, and that FAISS-era documents point at
  `23_VECTOR_INDEX_CONTRACT.md`. Add a document → add it to this index in the same commit.

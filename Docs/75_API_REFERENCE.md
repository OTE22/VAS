# API Reference

**Generated from the running application's OpenAPI document — do not edit by hand.** Regenerate with `scripts/generate_api_reference.py` after any route change; a stale copy is worse than none.

- **263 operations** across **239 paths**
- Service: `Face Recognition Service` v`5.0.0`

## How to read this

**Auth.** Almost every endpoint requires a bearer token from `POST /api/auth/login`, sent as `Authorization: Bearer <token>`, or the equivalent session cookie. The exceptions are the health endpoints, login itself, `/metrics` (restricted by source IP at nginx instead), and the camera ingest endpoints — which use an **ingest key**, not a bearer token: `X-Webhook-Key: <key>`.

**Cookie-authenticated mutations additionally require** `X-Requested-With: XMLHttpRequest`. Without it the request is rejected as cross-site (403 `CSRF_FAILED`). Bearer-token clients are unaffected.

**Expensive operations return `202 Accepted` with a `job_id`** rather than blocking — relationship calculation, threshold learning, model training and alert-channel tests. Poll the job rather than holding the connection open.

**Columns.** *Path params* are the `{braced}` segments. *Query* lists query-string parameters. *Body* marks endpoints taking a request body. *Returns* lists the documented status codes.

> The interactive version of this document is `/docs` (Swagger UI) and `/redoc`, served from vendored local assets with no internet access. Both are **disabled in production**, because they publish every admin route. This file is the production-safe substitute.

---

## Contents

- [Authentication](#authentication) — 4 operations
- [Health](#health) — 4 operations
- [Ingest (Webhook)](#ingest-webhook) — 6 operations
- [Identity Management](#identity-management) — 30 operations
- [Detections](#detections) — 2 operations
- [Search](#search) — 3 operations
- [Watchlists](#watchlists) — 16 operations
- [Live Alerts](#live-alerts) — 14 operations
- [Intelligence](#intelligence) — 21 operations
- [Security Intelligence](#security-intelligence) — 10 operations
- [ML Operations](#ml-operations) — 33 operations
- [SQL Agent](#sql-agent) — 20 operations
- [Conversations](#conversations) — 9 operations
- [Users](#users) — 12 operations
- [Settings Management](#settings-management) — 6 operations
- [Ingest Credentials](#ingest-credentials) — 3 operations
- [Upload & Enrollment](#upload-&-enrollment) — 4 operations
- [Enrollment Review](#enrollment-review) — 2 operations
- [Background Tasks](#background-tasks) — 8 operations
- [Export](#export) — 6 operations
- [Retention](#retention) — 2 operations
- [Audit](#audit) — 3 operations
- [Logs](#logs) — 4 operations
- [Statistics](#statistics) — 3 operations
- [Cache](#cache) — 6 operations
- [System Management](#system-management) — 4 operations
- [Metrics](#metrics) — 1 operations
- [Admin Tutorial](#admin-tutorial) — 2 operations
- [Admin Pages](#admin-pages) — 25 operations

---

## Authentication

Log in, inspect the current session, log out. Start here: every other bearer-token endpoint needs a token from `POST /api/auth/login`.

| Method | Path | Summary | Path params | Query | Body | Returns |
|---|---|---|---|---|---|---|
| `POST` | `/api/auth/login` | Login | — | — | yes | 200, 422 |
| `POST` | `/api/auth/logout` | Logout | — | — | — | 200 |
| `GET` | `/api/auth/me` | Get Current User Info | — | — | — | 200 |
| `GET` | `/api/auth/me/privileges` | Get User Privileges | — | — | — | 200 |

## Health

Liveness, readiness and per-component detail. `/health/live` does no I/O. `/health/ready` returns 503 **only** when the database or the models are unavailable — a degraded cache or a stalled background service deliberately does not take the API out of a load balancer. `/health/detailed` is the one to read when something is wrong.

| Method | Path | Summary | Path params | Query | Body | Returns |
|---|---|---|---|---|---|---|
| `GET` | `/health` | Health Check | — | — | — | 200 |
| `GET` | `/health/detailed` | Detailed Health Check | — | — | — | 200 |
| `GET` | `/health/live` | Liveness Check | — | — | — | 200 |
| `GET` | `/health/ready` | Readiness Check | — | — | — | 200 |

## Ingest (Webhook)

Where cameras send frames. Authenticated with an **ingest key**, not a bearer token: `X-Webhook-Key: <key>`. Returns 202 when queued, 503 only when every image in the request was rejected because the queue is full.

| Method | Path | Summary | Path params | Query | Body | Returns |
|---|---|---|---|---|---|---|
| `GET` | `/api/webhook/images/{pipeline_id}` | List Webhook Images | `pipeline_id` | — | — | 200, 422 |
| `GET` | `/api/webhook/images/{pipeline_id}/{filename}` | Get Webhook Image | `pipeline_id`, `filename` | — | — | 200, 422 |
| `GET` | `/api/webhook/test` | Webhook Test | — | — | — | 200 |
| `POST` | `/api/webhook/{pipeline_id}` | Webhook Api | `pipeline_id` | — | — | 200, 422 |
| `GET` | `/webhook/test` | Webhook Test | — | — | — | 200 |
| `POST` | `/webhook/{pipeline_id}` | Webhook | `pipeline_id` | — | — | 200, 422 |

## Identity Management

The core domain: unknown faces, promotion to a known person, merging duplicates, enrolment images, and per-identity detail.

| Method | Path | Summary | Path params | Query | Body | Returns |
|---|---|---|---|---|---|---|
| `GET` | `/api/admin/identities` | List / Search Identities | — | `limit`, `type`, `q`, `page`, `page_size`, `pipeline_id`, +3 more | — | 200, 401, 403, 404, 422, 500 |
| `GET` | `/api/admin/identities/debug/{identity_id}` | Debug Identity Recognition | `identity_id` | — | — | 200, 401, 403, 404, 422, 500 |
| `POST` | `/api/admin/identities/load-known-faces` | Load Known Faces | — | `force_reload` | — | 200, 401, 403, 404, 422, 500 |
| `POST` | `/api/admin/identities/merge` | Merge Identities | — | — | yes | 200, 401, 403, 404, 422, 500 |
| `POST` | `/api/admin/identities/merge-multiple` | Merge Multiple Identities | — | — | yes | 200, 401, 403, 404, 422, 500 |
| `POST` | `/api/admin/identities/merge-preview` | Preview Merge | — | — | yes | 200, 401, 403, 404, 422, 500 |
| `POST` | `/api/admin/identities/merges/{merge_id}/unmerge` | Unmerge Identities | `merge_id` | — | yes | 200, 401, 403, 404, 422, 500 |
| `GET` | `/api/admin/identities/search` | Search Identities | — | `query`, `limit` | — | 200, 401, 403, 404, 422, 500 |
| `GET` | `/api/admin/identities/status` | Identity Service Status | — | — | — | 200, 401, 403, 404, 500 |
| `GET` | `/api/admin/identities/verify-indexes` | Verify Indexes | — | — | — | 200, 401, 403, 404, 500 |
| `GET` | `/api/admin/identity/{identity_id}` | Get Identity Details | `identity_id` | — | — | 200, 401, 403, 404, 422, 500 |
| `GET` | `/api/admin/merge-suggestions` | Get Merge Suggestions | — | `status_filter` | — | 200, 401, 403, 404, 422, 500 |
| `POST` | `/api/admin/merge-suggestions/generate-pipeline-aware` | Generate Pipeline-Aware Merge Suggestions | — | — | — | 200, 401, 403, 404, 500 |
| `GET` | `/api/admin/merge-suggestions/model-status` | Get Model Status | — | — | — | 200, 401, 403, 404, 500 |
| `GET` | `/api/admin/merge-suggestions/models` | List Model Versions | — | `limit` | — | 200, 401, 403, 404, 422, 500 |
| `POST` | `/api/admin/merge-suggestions/models/{model_id}/activate` | Activate Candidate Model | `model_id` | `reason` | — | 200, 401, 403, 404, 422, 500 |
| `POST` | `/api/admin/merge-suggestions/models/{model_id}/reject` | Reject Candidate Model | `model_id` | `reason` | — | 200, 401, 403, 404, 422, 500 |
| `POST` | `/api/admin/merge-suggestions/models/{model_id}/rollback` | Rollback to Archived Model | `model_id` | `reason` | — | 200, 401, 403, 404, 422, 500 |
| `GET` | `/api/admin/merge-suggestions/pipeline/{pipeline_id}` | Get Merge Suggestions for Pipeline | `pipeline_id` | `status_filter` | — | 200, 401, 403, 404, 422, 500 |
| `POST` | `/api/admin/merge-suggestions/train-model` | Train Similarity Model (DEPRECATED) | — | `min_samples` | — | 200, 401, 403, 404, 422, 500 |
| `GET` | `/api/admin/merge-suggestions/training-jobs` | List Training Jobs | — | `limit` | — | 200, 401, 403, 404, 422, 500 |
| `POST` | `/api/admin/merge-suggestions/training-jobs` | Schedule Training Job | — | `min_samples` | — | 200, 401, 403, 404, 422, 500 |
| `GET` | `/api/admin/merge-suggestions/training-jobs/{job_id}` | Get Training Job | `job_id` | — | — | 200, 401, 403, 404, 422, 500 |
| `POST` | `/api/admin/merge-suggestions/training-jobs/{job_id}/cancel` | Cancel Training Job | `job_id` | — | — | 200, 401, 403, 404, 422, 500 |
| `POST` | `/api/admin/merge-suggestions/{suggestion_id}/approve` | Approve Merge Suggestion | `suggestion_id` | — | yes | 200, 401, 403, 404, 422, 500 |
| `POST` | `/api/admin/merge-suggestions/{suggestion_id}/reject` | Reject Merge Suggestion | `suggestion_id` | — | — | 200, 401, 403, 404, 422, 500 |
| `GET` | `/api/admin/unknown` | List Unknown Identities | — | `page`, `page_size`, `date_from`, `date_to`, `pipeline_id`, `status_filter`, +2 more | — | 200, 401, 403, 404, 422, 500 |
| `GET` | `/api/admin/unknown/{identity_id}/match-candidates` | Known identities this unknown face may already be | `identity_id` | — | — | 200, 401, 403, 404, 422, 500 |
| `POST` | `/api/admin/unknown/{identity_id}/promote` | Promote Unknown to Known | `identity_id` | — | yes | 200, 401, 403, 404, 422, 500 |
| `POST` | `/api/search/by-image` | Search by Image | — | — | yes | 200, 401, 403, 404, 422, 500 |

## Detections

Raw detection records, by pipeline or across all of them.

| Method | Path | Summary | Path params | Query | Body | Returns |
|---|---|---|---|---|---|---|
| `GET` | `/api/detections` | Get All Detections | — | `limit`, `offset` | — | 200, 422 |
| `GET` | `/api/detections/{pipeline_id}` | Get Pipeline Detections | `pipeline_id` | `limit` | — | 200, 422 |

## Search

Search by image and advanced multi-face search, including quality pre-checks and batch submission.

| Method | Path | Summary | Path params | Query | Body | Returns |
|---|---|---|---|---|---|---|
| `POST` | `/api/search/advanced` | Advanced Multi-Face Search | — | — | yes | 200, 422 |
| `GET` | `/api/search/config` | Get Search Configuration | — | — | — | 200 |
| `POST` | `/api/search/quality-check` | Check Image Quality | — | — | yes | 200, 422 |

## Watchlists

Watchlists and their entries. Deletion is a reversible soft delete.

| Method | Path | Summary | Path params | Query | Body | Returns |
|---|---|---|---|---|---|---|
| `GET` | `/api/identities/{identity_id}/watchlists` | Get Identity Watchlists | `identity_id` | — | — | 200, 422 |
| `GET` | `/api/watchlist-alerts` | List Watchlist Alerts | — | `watchlist_id`, `acknowledged`, `limit`, `offset` | — | 200, 422 |
| `POST` | `/api/watchlist-alerts/{alert_id}/acknowledge` | Acknowledge Alert | `alert_id` | — | yes | 200, 422 |
| `GET` | `/api/watchlists` | List / Search Watchlists | — | `include_inactive`, `page`, `page_size`, `search`, `alert_level`, `is_active`, +3 more | — | 200, 422 |
| `POST` | `/api/watchlists` | Create Watchlist | — | — | yes | 200, 422 |
| `GET` | `/api/watchlists/add-identity/{identity_id}/defaults` | Get Defaults for Adding Identity to Watchlist | `identity_id` | — | — | 200, 422 |
| `DELETE` | `/api/watchlists/{watchlist_id}` | Soft-Delete Watchlist | `watchlist_id` | `hard_delete`, `confirm`, `reason` | — | 200, 422 |
| `GET` | `/api/watchlists/{watchlist_id}` | Get Watchlist | `watchlist_id` | — | — | 200, 422 |
| `PUT` | `/api/watchlists/{watchlist_id}` | Update Watchlist | `watchlist_id` | — | yes | 200, 422 |
| `GET` | `/api/watchlists/{watchlist_id}/deletion-impact` | Deletion Impact Summary | `watchlist_id` | — | — | 200, 422 |
| `GET` | `/api/watchlists/{watchlist_id}/entries` | List Watchlist Entries | `watchlist_id` | `include_inactive`, `include_expired`, `page`, `page_size` | — | 200, 422 |
| `POST` | `/api/watchlists/{watchlist_id}/entries` | Add to Watchlist | `watchlist_id` | — | yes | 200, 422 |
| `DELETE` | `/api/watchlists/{watchlist_id}/entries/{identity_id}` | Remove from Watchlist | `watchlist_id`, `identity_id` | `hard_delete` | — | 200, 422 |
| `POST` | `/api/watchlists/{watchlist_id}/restore` | Restore Soft-Deleted Watchlist | `watchlist_id` | — | — | 200, 422 |
| `GET` | `/api/watchlists/{watchlist_id}/stats` | Get Watchlist Statistics | `watchlist_id` | — | — | 200, 422 |
| `PATCH` | `/api/watchlists/{watchlist_id}/status` | Activate / Deactivate Watchlist | `watchlist_id` | — | yes | 200, 422 |

## Live Alerts

Rules that fire when a tracked person is seen, and the trigger history they produce.

| Method | Path | Summary | Path params | Query | Body | Returns |
|---|---|---|---|---|---|---|
| `GET` | `/api/live-alerts` | List Live Alerts | — | `include_inactive` | — | 200, 422 |
| `POST` | `/api/live-alerts` | Create Live Alert | — | — | yes | 200, 422 |
| `GET` | `/api/live-alerts/defaults/{identity_id}` | Get Default Alert Settings | `identity_id` | — | — | 200, 422 |
| `GET` | `/api/live-alerts/test-jobs/{job_id}` | Get Channel-Test Job Status | `job_id` | — | — | 200, 422 |
| `POST` | `/api/live-alerts/triggers/{trigger_id}/acknowledge` | Acknowledge Trigger | `trigger_id` | — | — | 200, 422 |
| `DELETE` | `/api/live-alerts/{alert_id}` | Delete Live Alert | `alert_id` | — | — | 200, 422 |
| `GET` | `/api/live-alerts/{alert_id}` | Get Live Alert | `alert_id` | — | — | 200, 422 |
| `PUT` | `/api/live-alerts/{alert_id}` | Update Live Alert | `alert_id` | — | yes | 200, 422 |
| `GET` | `/api/live-alerts/{alert_id}/health` | Alert Health | `alert_id` | — | — | 200, 422 |
| `POST` | `/api/live-alerts/{alert_id}/pause` | Pause Live Alert | `alert_id` | — | — | 200, 422 |
| `POST` | `/api/live-alerts/{alert_id}/resume` | Resume Live Alert | `alert_id` | — | — | 200, 422 |
| `POST` | `/api/live-alerts/{alert_id}/test` | Test Notification Channels | `alert_id` | — | yes | 200, 422 |
| `GET` | `/api/live-alerts/{alert_id}/triggers` | Get Alert Triggers (paginated) | `alert_id` | `page`, `page_size`, `acknowledged`, `date_from`, `date_to`, `pipeline_id`, +2 more | — | 200, 422 |
| `POST` | `/api/live-alerts/{alert_id}/triggers/acknowledge-all` | Bulk Acknowledge Triggers | `alert_id` | — | yes | 200, 422 |

## Intelligence

Cross-camera tracking, related identities, temporal patterns and movement timelines. `GET /api/identities/{id}/cross-camera` returns **one track per calendar day**, not one flat list.

| Method | Path | Summary | Path params | Query | Body | Returns |
|---|---|---|---|---|---|---|
| `GET` | `/api/identities/{identity_id}/analyze` | Complete Identity Analysis | `identity_id` | — | — | 200, 422 |
| `GET` | `/api/identities/{identity_id}/cross-camera` | Get Cross-Camera Tracking | `identity_id` | `date`, `days_back` | — | 200, 422 |
| `GET` | `/api/identities/{identity_id}/map-data` | Get Map Data (GeoJSON) for the MapLibre map | `identity_id` | `date`, `days_back`, `show_routes`, `enable_security_features`, `detect_patterns`, `show_risk_heatmap` | — | 200, 422 |
| `GET` | `/api/identities/{identity_id}/related` | Get Related Identities | `identity_id` | `min_co_appearances`, `time_window_minutes`, `limit` | — | 200, 422 |
| `POST` | `/api/identities/{identity_id}/related/refresh` | Refresh Related Identities | `identity_id` | — | — | 200, 422 |
| `GET` | `/api/identities/{identity_id}/temporal-patterns` | Get Temporal Patterns | `identity_id` | `days_back` | — | 200, 422 |
| `GET` | `/api/identities/{identity_id}/timeline` | Get Movement Timeline | `identity_id` | `hours_back` | — | 200, 422 |
| `GET` | `/api/intelligence/correlation/calculate` | Calculate Activity Correlation (xCCA) | — | `identity_a`, `identity_b`, `days_back` | — | 200, 400, 422, 500 |
| `POST` | `/api/intelligence/relationships/calculate-all` | Calculate All Relationships | — | — | — | 200 |
| `GET` | `/api/intelligence/relationships/jobs/{job_id}` | Get Relationship Job Status | `job_id` | — | — | 200, 422 |
| `POST` | `/api/intelligence/thresholds/jobs` | Schedule Threshold Learning Job | — | `pipeline_ids` | — | 200, 422 |
| `GET` | `/api/intelligence/thresholds/jobs/{job_id}` | Get Threshold Job Status | `job_id` | — | — | 200, 422 |
| `POST` | `/api/intelligence/thresholds/learn` | Learn Optimal Thresholds | — | `pipeline_ids` | — | 200, 422, 500 |
| `GET` | `/api/intelligence/trajectory/predict` | Predict Next Camera | — | `identity_id`, `current_camera`, `top_k` | — | 200, 400, 422, 500 |
| `GET` | `/api/maps/availability` | Which basemap styles are actually usable | — | — | — | 200 |
| `POST` | `/api/maps/verify` | Re-measure the content of every installed map dataset | — | — | — | 200 |
| `GET` | `/api/security/anomalies/{identity_id}` | Detect Behavioral Anomalies | `identity_id` | `days_back` | — | 200, 422 |
| `GET` | `/api/security/capabilities` | Get Security Feature Capabilities | — | — | — | 200 |
| `GET` | `/api/security/network` | Social Network Analysis | — | `identity_ids`, `min_connections`, `days_back`, `max_nodes` | — | 200, 422 |
| `GET` | `/api/security/patterns` | Detect Suspicious Patterns | — | `days_back`, `min_group_size`, `pipeline_id` | — | 200, 422 |
| `GET` | `/api/security/threat/{identity_id}` | Threat Assessment | `identity_id` | — | — | 200, 422 |

## Security Intelligence

Risk assessment, suspicious-pattern detection and behavioural anomalies.

| Method | Path | Summary | Path params | Query | Body | Returns |
|---|---|---|---|---|---|---|
| `GET` | `/api/security/assessments` | List Threat Assessments | — | `person_id`, `pipeline_id`, `location_name`, `severity`, `status`, `date_from`, +3 more | — | 200, 422 |
| `POST` | `/api/security/assessments` | Create or Recalculate a Threat Assessment | — | — | yes | 201, 422 |
| `GET` | `/api/security/assessments/history/{subject_type}/{subject_id}` | Assessment History for a Subject | `subject_type`, `subject_id` | `page`, `page_size` | — | 200, 422 |
| `GET` | `/api/security/assessments/{assessment_id}` | Get One Threat Assessment | `assessment_id` | — | — | 200, 422 |
| `POST` | `/api/security/assessments/{assessment_id}/acknowledge` | Acknowledge an Assessment | `assessment_id` | — | — | 200, 422 |
| `POST` | `/api/security/assessments/{assessment_id}/reopen` | Reopen a Resolved Assessment | `assessment_id` | — | yes | 200, 422 |
| `POST` | `/api/security/assessments/{assessment_id}/resolve` | Resolve an Assessment | `assessment_id` | — | yes | 200, 422 |
| `GET` | `/api/security/learned-thresholds` | List Learned Thresholds | — | `signal_name`, `status` | — | 200, 422 |
| `POST` | `/api/security/learned-thresholds/{threshold_id}/activate` | Activate a Learned Threshold | `threshold_id` | — | — | 200, 422 |
| `GET` | `/api/security/risk-model` | Risk Model and Threshold Versions | — | — | — | 200 |

## ML Operations

The model lifecycle: features, labels, datasets, training jobs, candidates, drift and audit. Training produces a reviewable *candidate* rather than replacing the live model.

| Method | Path | Summary | Path params | Query | Body | Returns |
|---|---|---|---|---|---|---|
| `GET` | `/api/ml/audit` | ML Audit Log | — | `page`, `page_size` | — | 200, 422 |
| `GET` | `/api/ml/calls` | ML-Ops Call Log | — | `limit`, `errors_only`, `path_contains` | — | 200, 422 |
| `PUT` | `/api/ml/config/mode` | Change Decision Mode | — | — | yes | 200, 422 |
| `GET` | `/api/ml/datasets` | List Datasets | — | `page`, `page_size` | — | 200, 422 |
| `POST` | `/api/ml/datasets` | Build Dataset | — | — | yes | 201, 422 |
| `POST` | `/api/ml/datasets/backfill-hashes` | Verify legacy datasets and record their file hashes | — | — | — | 200 |
| `GET` | `/api/ml/datasets/definitions` | Dataset Definitions | — | — | — | 200 |
| `GET` | `/api/ml/datasets/{dataset_id}` | Dataset Detail | `dataset_id` | — | — | 200, 422 |
| `POST` | `/api/ml/datasets/{dataset_id}/archive` | Archive an unreferenced dataset (explicit, never automatic) | `dataset_id` | — | yes | 200, 422 |
| `GET` | `/api/ml/drift/reports` | Drift Reports | — | `page`, `page_size`, `model_id` | — | 200, 422 |
| `POST` | `/api/ml/drift/run` | Run Drift Check Now | — | — | — | 200 |
| `POST` | `/api/ml/features/compute` | Run Feature Collection | — | `full_rebuild` | — | 202, 422 |
| `GET` | `/api/ml/features/definitions` | Feature Definitions | — | — | — | 200 |
| `GET` | `/api/ml/labels` | List Labels | — | `label`, `label_kind`, `review_status`, `subject_id`, `page`, `page_size` | — | 200, 422 |
| `POST` | `/api/ml/labels` | Create Label | — | — | yes | 201, 422 |
| `GET` | `/api/ml/labels/stats` | Label Readiness | — | — | — | 200 |
| `POST` | `/api/ml/labels/{label_id}/review` | Review Label | `label_id` | — | yes | 200, 422 |
| `POST` | `/api/ml/labels/{label_id}/supersede` | Correct a label by supersession | `label_id` | — | yes | 200, 422 |
| `GET` | `/api/ml/models` | List Models | — | `model_type`, `stage`, `page`, `page_size` | — | 200, 422 |
| `GET` | `/api/ml/models/{model_id}` | Model Detail | `model_id` | — | — | 200, 422 |
| `POST` | `/api/ml/models/{model_id}/reject` | Reject a Model | `model_id` | — | yes | 200, 422 |
| `POST` | `/api/ml/models/{model_id}/shadow-approve` | Approve a VALIDATED model into SHADOW | `model_id` | — | yes | 200, 422 |
| `GET` | `/api/ml/overview` | ML Operations Overview | — | — | — | 200 |
| `POST` | `/api/ml/pause` | Pause ML — restore rules immediately | — | — | yes | 200, 422 |
| `GET` | `/api/ml/predictions` | List Predictions | — | `subject_id`, `fallback_only`, `page`, `page_size` | — | 200, 422 |
| `GET` | `/api/ml/retraining-policy/{model_type}` | Retraining Policy | `model_type` | — | — | 200, 422 |
| `PUT` | `/api/ml/retraining-policy/{model_type}` | Update Retraining Policy | `model_type` | — | yes | 200, 422 |
| `GET` | `/api/ml/shadow/evidence` | Shadow evidence for offline mapping review | — | `days`, `model_id` | — | 200, 422 |
| `POST` | `/api/ml/shadow/stop` | Stop Shadow (rollback) | — | — | yes | 200, 422 |
| `GET` | `/api/ml/shadow/summary` | Shadow Summary | — | `days` | — | 200, 422 |
| `POST` | `/api/ml/training-jobs` | Start Training Job | — | — | yes | 202, 422 |
| `GET` | `/api/ml/training-jobs/{job_id}` | Training Job Status | `job_id` | — | — | 200, 422 |
| `POST` | `/api/ml/training-jobs/{job_id}/cancel` | Cancel Training Job | `job_id` | — | — | 200, 422 |

## SQL Agent

Natural-language querying, executed under a read-only database role.

| Method | Path | Summary | Path params | Query | Body | Returns |
|---|---|---|---|---|---|---|
| `GET` | `/api/sql-agent/context` | Get Query Context | — | `session_id` | — | 200, 422 |
| `POST` | `/api/sql-agent/export/pdf` | Export To Pdf | — | — | yes | 200, 422 |
| `POST` | `/api/sql-agent/export/word` | Export To Word | — | — | yes | 200, 422 |
| `GET` | `/api/sql-agent/health` | Sql Agent Health | — | — | — | 200 |
| `GET` | `/api/sql-agent/history` | Get Query History | — | `page`, `page_size`, `limit`, `offset`, `session_id` | — | 200, 422 |
| `DELETE` | `/api/sql-agent/history/{query_id}` | Delete Query From History | `query_id` | — | — | 200, 422 |
| `GET` | `/api/sql-agent/history/{query_id}` | Get Query By Id | `query_id` | — | — | 200, 422 |
| `GET` | `/api/sql-agent/memory` | Get User Memories | — | `memory_type`, `min_importance` | — | 200, 422 |
| `POST` | `/api/sql-agent/memory` | Create Memory | — | — | yes | 200, 422 |
| `DELETE` | `/api/sql-agent/memory/{memory_id}` | Delete Memory | `memory_id` | — | — | 200, 422 |
| `POST` | `/api/sql-agent/query` | Sql Agent Query | — | — | yes | 200, 422 |
| `POST` | `/api/sql-agent/query/stream` | Sql Agent Query Stream | — | — | yes | 200, 422 |
| `POST` | `/api/sql-agent/requests/{request_id}/cancel` | Cancel Sql Agent Request | `request_id` | — | — | 200, 422 |
| `GET` | `/api/sql-agent/schema` | Sql Agent Schema | — | — | — | 200 |
| `POST` | `/api/sql-agent/session/load` | Sql Agent Load Session | — | — | yes | 200, 422 |
| `POST` | `/api/sql-agent/session/new` | Sql Agent New Session | — | — | — | 200 |
| `GET` | `/api/sql-agent/sessions` | Sql Agent List Sessions | — | — | — | 200 |
| `POST` | `/api/sql-agent/sessions/create` | Create Session | — | — | yes | 200, 422 |
| `GET` | `/api/sql-agent/sessions/list` | List User Sessions | — | `active_only` | — | 200, 422 |
| `PUT` | `/api/sql-agent/sessions/{session_id}` | Update Session | `session_id` | — | yes | 200, 422 |

## Conversations

Chatbot conversation history, branching and feedback.

| Method | Path | Summary | Path params | Query | Body | Returns |
|---|---|---|---|---|---|---|
| `GET` | `/api/v1/conversations` | List Conversations | — | `include_archived`, `limit`, `offset`, `q` | — | 200, 422 |
| `POST` | `/api/v1/conversations` | Create Conversation | — | — | yes | 200, 422 |
| `DELETE` | `/api/v1/conversations/{conversation_id}` | Delete Conversation | `conversation_id` | — | — | 200, 422 |
| `PATCH` | `/api/v1/conversations/{conversation_id}` | Rename Conversation | `conversation_id` | — | yes | 200, 422 |
| `GET` | `/api/v1/conversations/{conversation_id}/branches` | List Branches | `conversation_id` | — | — | 200, 422 |
| `POST` | `/api/v1/conversations/{conversation_id}/branches` | Branch From Message | `conversation_id` | — | yes | 200, 422 |
| `POST` | `/api/v1/conversations/{conversation_id}/feedback` | Record Feedback | `conversation_id` | — | yes | 200, 422 |
| `PATCH` | `/api/v1/conversations/{conversation_id}/flags` | Set Flags | `conversation_id` | — | yes | 200, 422 |
| `GET` | `/api/v1/conversations/{conversation_id}/messages` | Get Messages | `conversation_id` | `branch_id`, `limit`, `before_sequence` | — | 200, 422 |

## Users

User accounts, roles, pipeline access and password resets. Admin only.

| Method | Path | Summary | Path params | Query | Body | Returns |
|---|---|---|---|---|---|---|
| `GET` | `/api/pipelines` | Get All Pipelines | — | — | — | 200 |
| `PUT` | `/api/pipelines/{pipeline_id}/coordinates` | Update Pipeline Coordinates | `pipeline_id` | — | yes | 200, 422 |
| `PUT` | `/api/pipelines/{pipeline_id}/rename` | Rename Pipeline | `pipeline_id` | — | yes | 200, 422 |
| `GET` | `/api/users` | List Users | — | — | — | 200 |
| `POST` | `/api/users` | Create User | — | — | yes | 200, 422 |
| `GET` | `/api/users/me/pipelines` | Get My Pipelines | — | — | — | 200 |
| `POST` | `/api/users/system/restore` | Restore System Principal | — | — | — | 200 |
| `DELETE` | `/api/users/{user_id}` | Delete User | `user_id` | `reassign_admin_to` | — | 200, 422 |
| `GET` | `/api/users/{user_id}` | Get User | `user_id` | — | — | 200, 422 |
| `PUT` | `/api/users/{user_id}` | Update User | `user_id` | — | yes | 200, 422 |
| `POST` | `/api/users/{user_id}/reset-password` | Reset Password | `user_id` | — | yes | 200, 422 |
| `POST` | `/api/users/{user_id}/unblock` | Unblock User | `user_id` | — | — | 200, 422 |

## Settings Management

Runtime settings. Security-critical keys cannot be changed here — they are fixed at startup by the configuration guard.

| Method | Path | Summary | Path params | Query | Body | Returns |
|---|---|---|---|---|---|---|
| `GET` | `/api/settings` | Get All Settings | — | `category` | — | 200, 401, 403, 404, 422, 500 |
| `GET` | `/api/settings/` | Get All Settings | — | `category` | — | 200, 401, 403, 404, 422, 500 |
| `GET` | `/api/settings/audit/log` | Get Settings Audit Log | — | `setting_key`, `limit`, `offset` | — | 200, 401, 403, 404, 422, 500 |
| `GET` | `/api/settings/categories` | Get Setting Categories | — | — | — | 200, 401, 403, 404, 500 |
| `GET` | `/api/settings/{setting_key}` | Get Setting by Key | `setting_key` | — | — | 200, 401, 403, 404, 422, 500 |
| `PUT` | `/api/settings/{setting_key}` | Update Setting | `setting_key` | — | yes | 200, 400, 401, 403, 404, 422, 500 |

## Ingest Credentials

Issue and revoke per-camera ingest keys.

| Method | Path | Summary | Path params | Query | Body | Returns |
|---|---|---|---|---|---|---|
| `GET` | `/api/admin/webhook-credentials` | List Credentials | — | — | — | 200 |
| `POST` | `/api/admin/webhook-credentials` | Create Credential | — | — | yes | 201, 422 |
| `DELETE` | `/api/admin/webhook-credentials/{credential_id}` | Revoke Credential | `credential_id` | — | — | 200, 422 |

## Upload & Enrollment

Enrolling a person from images, and managing their photos.

| Method | Path | Summary | Path params | Query | Body | Returns |
|---|---|---|---|---|---|---|
| `GET` | `/api/identities/{identity_id}/images` | List a person's photos | `identity_id` | — | — | 200, 422 |
| `POST` | `/api/identities/{identity_id}/images` | Add another photo to an existing person | `identity_id` | — | yes | 200, 422 |
| `PUT` | `/api/identities/{identity_id}/images/{image_id}/primary` | Choose the primary photo | `identity_id`, `image_id` | — | — | 200, 422 |
| `POST` | `/api/upload-person` | DEPRECATED — use POST /api/identities/{identity_id}/images | — | — | yes | 200, 422 |

## Enrollment Review

Reviewing and confirming pending enrolments.

| Method | Path | Summary | Path params | Query | Body | Returns |
|---|---|---|---|---|---|---|
| `POST` | `/api/enrollment/cancel` | Discard an upload awaiting a decision | — | — | yes | 200, 422 |
| `POST` | `/api/enrollment/confirm` | Resolve a parked upload: add to an existing person, create a new one, or cancel | — | — | yes | 200, 422 |

## Background Tasks

Long-running jobs. Expensive operations return `202 + job_id`; poll the job rather than blocking.

| Method | Path | Summary | Path params | Query | Body | Returns |
|---|---|---|---|---|---|---|
| `GET` | `/api/tasks/alerts` | Get Tasks Requiring Alerts | — | — | — | 200 |
| `GET` | `/api/tasks/history` | Get Background Task History (paginated) | — | `page`, `page_size`, `task_type`, `status`, `date_from`, `date_to`, +3 more | — | 200, 422 |
| `GET` | `/api/tasks/running` | Get Running Tasks | — | — | — | 200 |
| `GET` | `/api/tasks/stats` | Get Task Statistics | — | `window_days` | — | 200, 422 |
| `GET` | `/api/tasks/upcoming` | Get Upcoming Tasks | — | `limit` | — | 200, 422 |
| `GET` | `/api/tasks/{task_id}` | Get Task Details | `task_id` | — | — | 200, 422 |
| `POST` | `/api/tasks/{task_id}/cancel` | Cancel a Task | `task_id` | — | — | 200, 422 |
| `POST` | `/api/tasks/{task_id}/retry` | Retry a Failed Task | `task_id` | — | — | 200, 422 |

## Export

Batch export of search results and identity data.

| Method | Path | Summary | Path params | Query | Body | Returns |
|---|---|---|---|---|---|---|
| `POST` | `/api/search/batch` | Batch Face Search | — | — | yes | 200, 422 |
| `POST` | `/api/search/batch/export` | Export Batch Results | — | `format` | yes | 200, 422 |
| `POST` | `/api/search/export` | Export Search Results | — | `format`, `include_images` | yes | 200, 422 |
| `DELETE` | `/api/search/history` | Clear Search History | — | — | — | 200 |
| `GET` | `/api/search/history` | Get Search History | — | `days_back`, `search_type`, `limit`, `offset` | — | 200, 422 |
| `GET` | `/api/search/history/export` | Export Search History | — | `format`, `days_back` | — | 200, 422 |

## Retention

Data-retention policy and the jobs that enforce it.

| Method | Path | Summary | Path params | Query | Body | Returns |
|---|---|---|---|---|---|---|
| `POST` | `/api/admin/retention/run` | Retention Run | — | `dry_run` | yes | 202, 422 |
| `GET` | `/api/admin/retention/status` | Retention Status | — | — | — | 200 |

## Audit

Audit trail, including chatbot query history.

| Method | Path | Summary | Path params | Query | Body | Returns |
|---|---|---|---|---|---|---|
| `GET` | `/api/audit/chatbot` | Get Chatbot Audit Logs | — | `limit`, `offset`, `user_id`, `username`, `success`, `start_date`, +1 more | — | 200, 422 |
| `GET` | `/api/audit/chatbot/stats` | Get Chatbot Audit Stats | — | `user_id`, `start_date`, `end_date` | — | 200, 422 |
| `GET` | `/api/audit/chatbot/{log_id}` | Get Chatbot Audit Log | `log_id` | — | — | 200, 422 |

## Logs

Reading and cleaning the application log. Admin only.

| Method | Path | Summary | Path params | Query | Body | Returns |
|---|---|---|---|---|---|---|
| `GET` | `/api/logs` | Read application logs | — | `page`, `page_size`, `date_from`, `date_to`, `level` | — | 200, 422 |
| `POST` | `/api/logs/cleanup` | Delete log entries past retention | — | — | — | 200 |
| `GET` | `/api/logs/config` | Log viewer configuration | — | — | — | 200 |
| `GET` | `/api/logs/stats` | Application log statistics | — | — | — | 200 |

## Statistics

Aggregate counts and dashboard summaries.

| Method | Path | Summary | Path params | Query | Body | Returns |
|---|---|---|---|---|---|---|
| `GET` | `/api/dashboard/config` | Get Dashboard Config | — | — | — | 200 |
| `GET` | `/api/dashboard/pipelines` | Get Dashboard Pipelines | — | — | — | 200 |
| `GET` | `/api/stats` | Get Stats | — | — | — | 200 |

## Cache

Cache inspection, warming and clearing. Admin only.

| Method | Path | Summary | Path params | Query | Body | Returns |
|---|---|---|---|---|---|---|
| `POST` | `/api/cache/clear` | Clear Cache | — | `pattern` | — | 200, 422 |
| `GET` | `/api/cache/health` | Cache Health | — | — | — | 200 |
| `GET` | `/api/cache/redis/stats` | Get Redis Stats | — | — | — | 200 |
| `GET` | `/api/cache/redis/test` | Test Redis Connection | — | — | — | 200 |
| `GET` | `/api/cache/stats` | Get Cache Stats | — | — | — | 200 |
| `POST` | `/api/cache/warm/{pipeline_id}` | Warm Cache For Pipeline | `pipeline_id` | `limit` | — | 200, 422 |

## System Management

Cleanup, face-tracker reset and circuit-breaker state. Admin only.

| Method | Path | Summary | Path params | Query | Body | Returns |
|---|---|---|---|---|---|---|
| `GET` | `/api/circuit-breaker/status` | Circuit Breaker Status | — | — | — | 200 |
| `POST` | `/api/cleanup/manual` | Manual Cleanup | — | — | — | 200 |
| `POST` | `/api/face-tracker/reset/{pipeline_id}` | Reset Face Tracker | `pipeline_id` | — | — | 200, 422 |
| `GET` | `/api/face-tracker/stats` | Face Tracker Stats Fixed | — | — | — | 200 |

## Metrics

Prometheus exposition. IP-restricted at nginx.

| Method | Path | Summary | Path params | Query | Body | Returns |
|---|---|---|---|---|---|---|
| `GET` | `/metrics` | Metrics | — | — | — | 200 |

## Admin Tutorial

The in-app tutorial content, which always matches the running build.

| Method | Path | Summary | Path params | Query | Body | Returns |
|---|---|---|---|---|---|---|
| `GET` | `/api/admin/tutorial` | Admin Tutorial | — | — | — | 200, 401, 403 |
| `GET` | `/api/admin/tutorial/examples` | API Examples | — | — | — | 200, 401, 403 |

## Admin Pages

Server-rendered HTML pages for the admin console. These return pages, not JSON, and are listed here only for completeness.

| Method | Path | Summary | Path params | Query | Body | Returns |
|---|---|---|---|---|---|---|
| `GET` | `/` | Root | — | — | — | 200 |
| `GET` | `/admin/audit` | Admin Audit | — | — | — | 200 |
| `GET` | `/admin/background-tasks` | Admin Background Tasks | — | — | — | 200, 422 |
| `GET` | `/admin/identity/{identity_id}` | Identity Profile | `identity_id` | — | — | 200, 422 |
| `GET` | `/admin/ingest-credentials` | Ingest Credentials Page | — | — | — | 200 |
| `GET` | `/admin/intelligence` | Admin Intelligence | — | — | — | 200 |
| `GET` | `/admin/live-alerts` | Admin Live Alerts | — | — | — | 200, 422 |
| `GET` | `/admin/logs` | Logs Page | — | — | — | 200 |
| `GET` | `/admin/ml-model` | Admin Ml Model | — | — | — | 200 |
| `GET` | `/admin/ml-ops` | Admin Ml Ops | — | — | — | 200 |
| `GET` | `/admin/pipelines` | Admin Pipelines | — | — | — | 200 |
| `GET` | `/admin/search` | Admin Search | — | — | — | 200 |
| `GET` | `/admin/search-history` | Admin Search History | — | — | — | 200 |
| `GET` | `/admin/security-intelligence` | Admin Security Intelligence | — | — | — | 200 |
| `GET` | `/admin/settings` | Admin Settings | — | — | — | 200 |
| `GET` | `/admin/tutorial` | Admin Tutorial | — | — | — | 200 |
| `GET` | `/admin/unknown` | Unknown Faces | — | — | — | 200, 422 |
| `GET` | `/admin/users` | Admin Users | — | — | — | 200 |
| `GET` | `/admin/watchlists` | Admin Watchlists | — | — | — | 200 |
| `GET` | `/dashboard` | Dashboard | — | — | — | 200 |
| `GET` | `/docs/overview` | Api Overview | — | — | — | 200 |
| `GET` | `/home` | Home | — | — | — | 200, 422 |
| `GET` | `/ping` | Ping | — | — | — | 200 |
| `GET` | `/signin` | Signin | — | — | — | 200 |
| `GET` | `/tracking-people` | Tracking People | — | — | — | 200 |

---

## Errors

Structured errors carry a machine-readable code:

```json
{"error": {"code": "RATE_LIMITED", "message": "...", "reference_id": "AUTH-1a2b3c4d", "retryable": true}}
```

Every response carries an **`X-Request-ID`** header (12 hex characters). Every log line written while serving that request carries `req=<id>`, so the header is how you find the traceback for a 500:

```bash
docker compose -f docker/docker-compose.prod.yml exec face_recognition \
  sh -c "grep req=<id> /var/log/face-recognition/app.log"
```

Auth endpoints use a second scheme, `AUTH-` plus 8 hex characters, in `error.reference_id` and in the `[AUTH_AUDIT]` log line.

Common codes: `INVALID_CREDENTIALS` (401), `RATE_LIMITED` (429, with `Retry-After`), `CSRF_FAILED` (403), `WEBHOOK_AUTH_REQUIRED` (401), `SESSION_CREATION_FAILED` (500), `AUTH_SERVICE_UNAVAILABLE` (500).

Troubleshooting each of these: [`73_TROUBLESHOOTING.md`](73_TROUBLESHOOTING.md).

# Remaining Read_From differences

Compared on 2026-09-15 against the current VAS working tree, after the approved background-job, intelligence and alert imports. This review makes no additional application changes. Line-ending-only differences and generated Python caches are excluded.

## Already imported: Gunicorn, utilities and scripts

| File | Imported change | Status |
| --- | --- | --- |
| `gunicorn.conf.py` | Gunicorn master writes to `server.log`, separate from the API worker log. | Matches source |
| `utils/logging.py` | Named log sources, writer lock for retention, and closure of inherited handlers after fork. | Matches source |
| `scripts/dev/db_tunnel.py` | Tunnel diagnostics go to `logs/diagnostics/db_tunnel.log`; creates the directory. | Matches source |
| `backend/utils/migrations.py` | Dedicated `migrations.log`. | Matches source |
| `backend/services/image_processing.py` | Dedicated `image-processing.log` for the CLI. | Matches source |

No further functional update remains in the source `utils/` directory; its other new entries are generated Python caches.

## Remaining application and script differences

The descriptions below show what copying FROM Read_From INTO VAS would do. A difference does not establish that the incoming file is newer or better.

| File | Effect / recommendation |
| --- | --- |
| `backend/core/detection_evidence.py` | Moves duplicate-frame detection AFTER identity/embedding updates. Current code checks duplicates first. Keep current replay protection. |
| `backend/core/runtime_settings.py` | Documentation filename in a comment only. |
| `backend/main.py` | Removes SSO router registration. Excluded: chatbot integration. |
| `backend/routes/__init__.py` | Removes SSO router export. Excluded: chatbot integration. |
| `backend/routes/admin_tutorial.py` | Replaces documentation references with the source folder numbering. No new tutorial behavior. |
| `backend/routes/audit.py` | Removes the chatbot audit endpoint and its security-policy integration. Excluded. |
| `backend/routes/auth.py` | Removes SSO-aware navigation and chatbot session revocation on logout. Excluded. |
| `backend/routes/identities.py` | Removes saved scalar actor details used for audit after rollback; reuses ORM objects that rollback can expire. Keep current fix. |
| `backend/routes/live_alerts.py` | Removes time-window/URL validation and identity-existence checks. Alert enhancements were already imported separately; keep current API validation. |
| `backend/routes/settings.py` | Removes conflict-safe settings seeding, potentially restoring concurrent-insert failures; also changes documentation links. Keep current behavior. |
| `backend/routes/upload.py` | Marks the create-person endpoint deprecated and recommends an endpoint for adding photos to an existing identity. Keep current create-person documentation. |
| `backend/routes/users.py` | Removes username, email, password and full-name field validation. Keep current validation. |
| `backend/security/config_guard.py` | Removes SSO-related configuration protections. Excluded: chatbot integration. |
| `backend/security/redaction.py` | Removes SSO secret names from redaction. Excluded: chatbot integration. |
| `frontend/admin/tutorial.html` | Documentation links only; use only if adopting the alternate documentation numbering. |
| `frontend/components/admin-navbar.html` | Replaces chatbot SSO launch link with old tracking route. Excluded. |
| `frontend/components/navbar.html` | Replaces chatbot SSO navigation with old route. Excluded. |
| `frontend/home.html` | Replaces chatbot SSO launch link with old tracking route. Excluded. |
| `frontend/js/admin-live-alerts.js` | Only the corrected loading text differs: current Loading triggers… versus incoming Loading triggers?. Keep current text. |
| `scripts/README.md` | Map documentation links only. |
| `scripts/backup/backup.sh` | Backup documentation link in a comment only; backup behavior is identical. |
| `scripts/generate_api_reference.py` | Changes generated document destination from Docs/48_API_REFERENCE.md to Docs/75_API_REFERENCE.md and related links. |
| `scripts/map_data/build_dem.sh` | Documentation filename in an error message only; no map-building change. |
| `scripts/map_data/build_satellite.sh` | Documentation filename in an error message only; no map-building change. |
| `scripts/map_data/install_dataset.sh` | Documentation filename in a comment only; no installation change. |
| `scripts/prepare_offline_bundle.sh` | Removes same-server checking, --same-server and help handling. Keep current intranet workflow. |
| `scripts/settings_consumers.py` | Changes generated document destination from Docs/09_SETTINGS_CONSUMERS.md to Docs/98_SETTINGS_CONSUMERS.md. |
| `scripts/setup/start_production.sh` | Changes a documentation link and commented-out legacy log paths from storage/logs to logs. No active startup change. |

## Remaining test differences

| File | Effect / recommendation |
| --- | --- |
| `tests/test_admin_tutorial.py` | Expects alternate documentation filenames. |
| `tests/test_auth_security.py` | Expects older signin.js cache version (3 instead of 4). Keep current test. |
| `tests/test_documentation_consistency.py` | Expects alternate document numbering and removes recognition of valid /api/health/... routes. Keep current checks. |
| `tests/test_enrollment_decision_gate.py` | Changes expected outcome for a same-name photo from review (202) to direct saving (200). This is a test expectation change, not a complete enrollment feature update; do not import alone. |
| `tests/test_identity_merge_smoke.py` | Expects an older migration head. |
| `tests/test_migration_schema_parity.py` | Expects 26 features rather than 38 and removes relational-feature verification. |
| `tests/test_ml_foundation.py` | Expects an older migration head. |
| `tests/test_no_dead_knobs.py` | Documentation filename in a comment only. |
| `tests/test_promote_merge_integrity.py` | Older fixtures, audit-field expectations and promotion error/cache checks. Keep tests aligned with the current implementation. |
| `tests/test_risk_platform.py` | Expects an older migration head. |
| `tests/test_vector_index_migration.py` | Expects an older migration head. |

## Documentation and reports available to choose

The source contains a different document numbering scheme. Files marked **new path** below are absent at that exact current path; that does not mean their content is new. Many correspond to existing documents under different names. Copying the full set would need a separate content and link reconciliation.

Useful candidates for a documentation-only follow-up:

- Background-task, scheduling and logging guides and the background-job audit/verification reports.
- The related-identity-filter report, explaining the intelligence behavior already imported.
- Broader operational, ML, maps and tutorial documentation, if you want those reviewed separately.

Source audit/verification reports describe the source author's checks, not tests performed on this installation. Chatbot/agent documentation remains outside the approved import scope.

107 document/report paths still differ or are absent:

- **changed**: `Docs/00_DOCUMENTATION_INDEX.md`
- **new path**: `Docs/01_QUICK_START.md`
- **new path**: `Docs/02_DOCKER_QUICK_START.md`
- **new path**: `Docs/03_ADMIN_SETUP_GUIDE.md`
- **new path**: `Docs/04_SETUP_NVIDIA_DOCKER.md`
- **new path**: `Docs/05_MIGRATION_GUIDE.md`
- **new path**: `Docs/06_PROMOTE_AND_MERGE_GUIDE.md`
- **new path**: `Docs/07_UNKNOWN_FACES_CENTER_COMPLETE_GUIDE.md`
- **new path**: `Docs/08_IDENTITY_API_FRONTEND_GUIDE.md`
- **new path**: `Docs/09_HOW_MERGE_SUGGESTIONS_WORK.md`
- **new path**: `Docs/10_AUTO_CLEAN_AND_CLUSTER_JOBS_GUIDE.md`
- **new path**: `Docs/11_GRAPH_BASED_CLUSTERING.md`
- **new path**: `Docs/11_SYSTEM_CAPABILITIES.md`
- **new path**: `Docs/12_README.md`
- **new path**: `Docs/13_README_GPU.md`
- **new path**: `Docs/15_PERFORMANCE_OPTIMIZATION.md`
- **new path**: `Docs/16_PERSISTENCE_STATUS.md`
- **new path**: `Docs/17_CAPACITY_VERIFICATION.md`
- **new path**: `Docs/18_AUDIT_LOGGING_GUIDE.md`
- **new path**: `Docs/19_BLOCKED_USERS.md`
- **new path**: `Docs/20_NAVBAR_COMPONENT_GUIDE.md`
- **new path**: `Docs/21_RISK_PLATFORM_GUIDE.md`
- **changed**: `Docs/21_WEBHOOK_TROUBLESHOOTING.md`
- **new path**: `Docs/22_WEBHOOK_DEBUG.md`
- **new path**: `Docs/23_CLEANUP_UNKNOWN_IDENTITIES_GUIDE.md`
- **new path**: `Docs/24_SETTINGS_MANAGEMENT_GUIDE.md`
- **new path**: `Docs/25_API_AUTHENTICATION_GUIDE.md`
- **new path**: `Docs/26_USER_PIPELINE_ACCESS_GUIDE.md`
- **new path**: `Docs/27_HOW_TO_GRANT_UNKNOWN_FACES_ACCESS.md`
- **new path**: `Docs/28_MULTI_IDENTITY_MERGE_GUIDE.md`
- **new path**: `Docs/29_PROMOTION_FLOW_EXPLAINED.md`
- **new path**: `Docs/31_DYNAMIC_PROMOTION_FLOW.md`
- **new path**: `Docs/32_50_CAMERAS_SCALABILITY_ANALYSIS.md`
- **new path**: `Docs/34_SCRFD_ARCFACE_INTEGRATION_PIPELINE.md`
- **new path**: `Docs/35_IDENTITY_RECOGNITION_DEBUG_GUIDE.md`
- **new path**: `Docs/35_PGVECTOR_INTEGRATION.md`
- **new path**: `Docs/36_CONFIGURATION_GUIDE.md`
- **new path**: `Docs/37_ADVANCED_MERGE_FLOW_GUIDE.md`
- **new path**: `Docs/38_SEARCH_BY_IMAGE_GUIDE.md`
- **new path**: `Docs/39_ADVANCED_SEARCH_INTELLIGENCE_GUIDE.md`
- **new path**: `Docs/40_LIVE_ALERTS_GUIDE.md`
- **new path**: `Docs/40_ML_INTEGRATION.md`
- **new path**: `Docs/41_ML_CLUSTERING.md`
- **new path**: `Docs/41_PIPELINE_AWARE_ML_CLUSTERING_GUIDE.md`
- **new path**: `Docs/42_ML_PGVECTOR_INTEGRATION.md`
- **new path**: `Docs/42_ML_SIMILARITY_MODEL_GUIDE.md`
- **new path**: `Docs/44_BACKEND_PATH_NORMALIZATION_BEST_PRACTICE.md`
- **new path**: `Docs/45_SECURITY_INTELLIGENCE_GUIDE.md`
- **new path**: `Docs/46_MAP_SERVICE_GUIDE.md`
- **new path**: `Docs/48_SECURITY_INTELLIGENCE_MAP_FEATURES.md`
- **new path**: `Docs/49_BEHAVIORAL_ANOMALY_ML_STATE.md`
- **new path**: `Docs/50_API_DOCUMENTATION.md`
- **new path**: `Docs/51_TUTORIAL_GUIDE.md`
- **new path**: `Docs/52_ANIMATED_AVATAR_GUIDE.md`
- **new path**: `Docs/53_ANIMATED_AVATAR_ROUTE_VERIFICATION.md`
- **new path**: `Docs/54_AVATAR_VISIBILITY_AND_TIMING.md`
- **new path**: `Docs/57_MULTI_CAMERA_SOCIAL_NETWORK_ANALYSIS.md`
- **new path**: `Docs/58_CROSS_CAMERA_RESEARCH_COMPARISON.md`
- **new path**: `Docs/59_ADVANCED_SNA_ENHANCEMENTS.md`
- **new path**: `Docs/60_BACKUP_AND_RESTORE.md`
- **new path**: `Docs/61_DEPLOYMENT_RUNBOOK.md`
- **new path**: `Docs/62_HOW_TO_USE_ENHANCEMENTS.md`
- **new path**: `Docs/63_REDIS_CACHING_GUIDE.md`
- **new path**: `Docs/64_IDENTITY_RECOGNITION_EXPLANATION.md`
- **new path**: `Docs/65_IMAGE_QUALITY_ANALYSIS.md`
- **new path**: `Docs/66_IMAGE_SECURITY_ANALYSIS.md`
- **new path**: `Docs/67_SNA_ENHANCEMENTS_QUICK_START.md`
- **new path**: `Docs/68_KNOWN_FACES_STARTUP_FLOW.md`
- **new path**: `Docs/69_CLEAR_DATABASE_GUIDE.md`
- **new path**: `Docs/70_VECTOR_INDEX_CONTRACT.md`
- **new path**: `Docs/71_IMAGE_INGESTION_WORKFLOW.md`
- **new path**: `Docs/72_ADMIN_CHEAT_SHEET.md`
- **new path**: `Docs/73_TROUBLESHOOTING.md`
- **new path**: `Docs/74_SECURITY_CHECKLIST.md`
- **new path**: `Docs/75_API_REFERENCE.md`
- **new path**: `Docs/76_DETECTION_DATABASE_WRITES.md`
- **new path**: `Docs/77_UNKNOWN_FACES_ARCHITECTURE.md`
- **new path**: `Docs/78_SETTINGS_RUNTIME_MATRIX.md`
- **new path**: `Docs/79_BACKGROUND_TASKS.md`
- **new path**: `Docs/80_ALEMBIC_IN_DOCKER.md`
- **new path**: `Docs/81_SQL_AGENT_QUERY_HISTORY.md`
- **new path**: `Docs/82_RECOGNITION_LOGGING_WALKTHROUGH.md`
- **new path**: `Docs/83_API_ENHANCEMENTS_GUIDE.md`
- **new path**: `Docs/85_MAP_MIGRATION_INVENTORY.md`
- **new path**: `Docs/86_MAP_DATASET_ACQUISITION.md`
- **new path**: `Docs/87_DATABASE_RELATIONSHIPS.md`
- **new path**: `Docs/87_DATABASE_RELATIONSHIPS.pdf`
- **new path**: `Docs/88_CORRECTIVE_PASS_REPORT.md`
- **new path**: `Docs/89_OFFLINE_MAP_REMEDIATION.md`
- **new path**: `Docs/90_AGENT_ARCHITECTURE.md`
- **new path**: `Docs/91_ML_JOB_WORKER_ARCHITECTURE.md`
- **new path**: `Docs/92_RELATIONAL_ML_MODELS.md`
- **new path**: `Docs/93_PRODUCTION_RUNBOOK.md`
- **new path**: `Docs/94_MLOPS_OPERATOR_WORKFLOW.md`
- **new path**: `Docs/94_SETTINGS_PAGE_VERIFICATION.md`
- **new path**: `Docs/95_AGENT_PRODUCTION_ACCEPTANCE.md`
- **new path**: `Docs/95_BACKGROUND_JOBS_VERIFICATION.md`
- **new path**: `Docs/96_LOCAL_DATA_AGENT_ARCHITECTURE.md`
- **new path**: `Docs/97_DATA_AGENT_CONFIGURATION_GUIDE.md`
- **new path**: `Docs/98_SETTINGS_CONSUMERS.md`
- **changed**: `Docs/99_SQL_AGENT_AUDIT_BRIEF.md`
- **new path**: `Docs/mlops-guided-workflow.md`
- **new path**: `analysis-reports/background-jobs-audit-2026-09-13.md`
- **new path**: `analysis-reports/background-jobs-verification-2026-09-14.md`
- **new path**: `analysis-reports/camera-activity-daily-hourly.html`
- **new path**: `analysis-reports/iron-man-tracking.html`
- **new path**: `analysis-reports/related-identity-filters-2026-09-14.md`

## Review recommendation

Keep the already imported Gunicorn, utilities, background-job, intelligence and alert updates. No additional functional script update is needed for those features. Select documentation groups for a separate review if desired; retain the existing offline workflow, validation, migration-aware tests and chatbot integration.

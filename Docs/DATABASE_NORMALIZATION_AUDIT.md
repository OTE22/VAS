# VAS database normalization audit

> **Follow-up:** fixes are now implemented and passed 59 isolated tests, including all six previously failing cases. Production migration is still pending. See [DATABASE_INTEGRITY_FIX.md](DATABASE_INTEGRITY_FIX.md).

Date: 2026-09-15. Scope: deployed **VAS PostgreSQL public schema**, compared with `db_models.py` and migrations. The separate VMS database was not audited. Chatbot tables inside the VAS schema were inspected read-only; chatbot code was not changed.

## Decision

**Do not describe this database as fully 3NF/4NF, or certify all production readiness from this audit.** It has a substantial relational core with enforced keys, plus deliberate caches, historical snapshots, flexible JSON documents, and some operational relationships stored as lists.

Strict 4NF everywhere is not a prerequisite for a usable production application. For this system, prioritize enforceable operational relationships and consistent ownership; document intentional exceptions rather than mechanically splitting every JSON field or audit snapshot.

No production data, constraints, migrations, application files or container configuration were changed. Only this audit and its aggregate evidence were written.

## Verified live facts

- 64 public tables: 63 application tables plus `alembic_version`.
- Every table has a primary key: 64 primary-key constraints.
- 97 foreign-key constraints; no unvalidated constraints or invalid indexes found in the catalog.
- Live migration revision `fdd4e5f6a7b8` matches the sole code migration head.
- All 63 ORM table names exist in the live schema. This is a table-inventory check, not a complete column/default migration-parity test.
- 28 targeted aggregate consistency checks returned zero violations.
- **Major limitation:** pipelines, detections, identities, appearances, images, embeddings, watchlists, alerts and merge suggestions contained zero rows. Their zero-violation checks are vacuous, not evidence of correctness under load.
- There was populated task history (997 rows at count time); the tested task-status/success contradictions and missing task actors were zero. The two tested chatbot ownership comparisons also returned zero.
- Counts were read in separate bounded read-only transactions while the server was running, not one frozen database snapshot. No passwords, images, identities, questions, or individual records were exported.

## Meaning of the normal forms

| Form | What must be true | Assessment for this schema |
| --- | --- | --- |
| 1NF | In the classical relational interpretation, values represent single domain values rather than repeating groups of independent operational facts. | Many core tables use scalar attributes and child rows. Alert cameras/days/recipients and merge members are embedded lists, so a blanket classical 1NF claim for the operational model is inappropriate. JSON or vector types alone do not prove a violation: an immutable document or mathematical vector can be treated as one domain value. |
| 2NF | 1NF plus no non-prime attribute depends on only part of any candidate key. | Access, membership and watchlist junctions have sensible composite uniqueness. Surrogate IDs alone do not prove 2NF. Image content metadata presents a conditional partial-dependency concern discussed below. |
| 3NF | For each nontrivial functional dependency X → A, X is a superkey or A is a prime attribute. | Not certified globally. Current ownership and detection provenance are repeated in child tables; distinguish these from historical values before applying the dependency test. |
| 4NF | Every nontrivial multivalued dependency has a superkey determinant. | Not certified globally. Independent alert camera, weekday and recipient relationships should have separate child relations if normalized. Do not combine them into one camera × weekday × recipient table. Merely having multiple JSON arrays does not by itself establish a formal relational multivalued dependency. |

Normal forms depend on intended functional/multivalued dependencies and all candidate keys. Catalog inspection and a clean data sample cannot establish every business dependency.

References: [Microsoft database design basics](https://support.microsoft.com/en-us/access/database-design-basics), [IBM Db2 entity normalization](https://www.ibm.com/docs/en/db2-for-zos/13.0.0?topic=model-entity-normalization), [Fagin's original 4NF paper, IBM Research](https://research.ibm.com/publications/multivalued-dependencies-and-a-new-normal-form-for-relational-databases).

## Findings and priorities

### N1 — High priority: alert camera relationships lack database referential integrity

**Files:** `db_models.py`, class `LiveSearchAlert`; `backend/core/live_alert_service.py`; `backend/routes/live_alerts.py`.

`live_search_alerts.pipeline_ids` is a JSON list of operational camera identifiers, not FK-backed rows. `active_days`, `email_recipients` and `sms_recipients` hold separate repeated facts. Watchlist recipients are also lists. Ordinary foreign keys cannot protect IDs inside those JSON values. The live table has no check constraints for these arrays. The diagnostic route checks camera validity, but that does not supply database referential integrity for every writer. Current rows tested: zero.

**Before production rollout:** either normalize these operational relations or explicitly adopt and test a validation policy across create/update, rename/delete and background writers. Merely leaving a diagnostic endpoint to notice invalid configuration is weaker than preventing it.

Suggested normalized structure:

- `live_alert_pipelines(alert_id, pipeline_id)`, composite PK; FK to alerts and pipelines.
- `live_alert_weekdays(alert_id, weekday)`, composite PK; weekday CHECK 0–6.
- `live_alert_recipients(alert_id, channel, destination)`, composite uniqueness and channel validation.
- `watchlist_recipients(watchlist_id, channel, destination)`.

Preserve existing **all-camera** semantics: the service treats a missing or empty camera list as all cameras. Use an explicit scope field when migrating rather than silently interpreting zero child rows as no cameras. Verify empty-weekday behavior too. Choose camera deletion/rename rules deliberately; do not cascade away the only camera restriction and accidentally broaden an alert.

### N2 — High priority: embedding/image ownership agreement is not constrained

**Files:** `db_models.py`, classes `IdentityEmbedding` and `IdentityImage`; `backend/core/enrollment_service.py`.

An embedding holds both `image_id` and `identity_id`; the referenced image also holds its owner `identity_id`. Individual foreign keys prove both referenced rows exist, but do not prove their owners agree. Under the intended rule that an image belongs to one current identity, this repeats the dependency `image_id → identity_id` across relations. The enrollment service performs validation, but the catalogued keys alone do not enforce agreement for every writer. Current rows tested: zero.

Also, the image class documentation says one embedding per usable image, but the live embedding table has **no unique index on image_id**. This is a cardinality-contract question, distinct from normalization.

**Before production rollout:** confirm whether multiple model versions/embeddings per image are legitimate. Enforce the actual uniqueness rule (image only, or image plus model/version) and ownership agreement. If using composite foreign keys or triggers, account for image deletion's existing SET NULL behavior and transactional identity merges; a naive constraint can break both. Do not simply drop embedding.identity_id: camera embeddings legitimately have no image row and identity filtering is performance-sensitive.

### N3 — Medium priority: merge suggestion members are JSON relationships

**Files:** `db_models.py`, class `MergeSuggestion`; clustering and merge handlers.

`identity_ids` holds UUID references without member-level FKs or uniqueness constraints; `representative_snapshots` is a separate list. Pending suggestions need referential validation when applied. Zero pending suggestions were available to test.

Suggested structure: `merge_suggestion_members(suggestion_id, identity_id, position, snapshot_path)`, with membership/position uniqueness where those meanings apply. Preserve suggestion-time snapshot values. Define whether deletion invalidates a pending suggestion or preserves a historical member reference; blindly cascading historical membership would lose audit evidence. Do not assume parallel list positions are independent multivalued facts if each image belongs to a particular member.

### N4 — Review/document: detection provenance duplicated in evidence and alert rows

**Files:** `db_models.py`, classes `IdentityAppearance`, `IdentityEmbedding`, `LiveAlertTrigger`, `WatchlistAlert`.

A linked detection determines its pipeline; several child rows store `detection_id` and `pipeline_id`, and appearances also store `detection_uuid`. When these mean the same current fact, they introduce dependency/redundancy concerns. However, detection deletion intentionally sets the FK to NULL while preserving longer-lived evidence, so retained provenance is useful.

Recommendation: document these as immutable event-time snapshots where appropriate, validate agreement at creation, and test ingestion, replay, camera rename and retention. Keep data needed after detection removal. Do not classify a historical location snapshot as a copy of the camera's current location.

### N5 — Conditional 2NF concern: enrollment image metadata

**File:** `db_models.py`, class `IdentityImage`.

There is composite uniqueness on `(identity_id, file_checksum)`. If the checksum denotes immutable original bytes, facts such as byte length/dimensions depend on the checksum rather than the full ownership pair. That is a candidate partial dependency under the assumption those columns describe the original bytes; confirm whether any describe a processed derivative instead.

Optional design: `image_blobs(checksum, size, dimensions, content_type, ...)` plus identity-owned image records for ownership, display filename and primary status. Do not share physical storage or deduplicate across identities without reviewing deletion and access-control behavior. This is not an automatic production blocker.

### N6 — Review/document: task state and historical actors

**File:** `db_models.py`, class `BackgroundTaskHistory`.

`status`, `success`, timestamps and `duration_seconds` can become inconsistent; `created_by_user_id` is not a FK. These are operational integrity concerns, not automatic proof of a normal-form violation. A duration may intentionally measure work differently from wall-clock timestamps. Historical actor IDs may intentionally survive account deletion.

Recommendation: define the state contract, enforce compatible checks or derive redundant fields, and define whether the actor is a live reference or historical snapshot. Preserve retry, cancellation and legacy-history semantics. The specific tested contradictions were absent in current data.

### N7 — Preserve intentional JSON, caches and audit history

Do not normalize all 76 JSONB columns mechanically. ML feature vectors, model configuration, immutable task results, frozen pending-enrollment candidates, before/after audit documents and analytical snapshots can legitimately be compound values. Stored usernames at event time should not be replaced with joins to the user's current name. Cached appearance counts and relationship statistics need update/rebuild contracts, not necessarily new entity tables.

`user_query_embeddings.user_id` and query/session owner duplication are additional ownership dependencies inside chatbot tables. The targeted checks found no mismatches. Changes remain excluded under the instruction to leave chatbot code untouched; any later constraints must also preserve deleted-user history and current authorization behavior.

## What is already structured well

- `user_pipeline_access`: unique `(user_id, pipeline_id)` and parent FKs.
- `watchlist_entries`: unique `(watchlist_id, identity_id)` and parent FKs.
- `workspace_members`: unique `(workspace_id, user_id)`; the membership role belongs to the pair, not the user's global role.
- `message_feedback`: unique `(message_id, user_id)`.
- `identity_relationships`: unique ordered identity pair plus a CHECK preventing reversed/self pairs; derived statistics are explicitly cached.
- Primary image and trigger idempotency rules have partial unique indexes in the deployed database.

These are good structural patterns, not a proof that every attribute in those tables meets every higher normal form.

## Safe implementation sequence if remediation is approved

1. Agree camera-scope, image ownership/cardinality, suggestion-history and actor-history rules.
2. Add child tables/constraints through migrations with explicit FK deletion behavior.
3. Backfill and compare counts; retain compatibility reads/writes until verified. Never remove old JSON fields first.
4. Test actual PostgreSQL constraints with valid and invalid rows in an isolated fixture database, including concurrent requests, identity merges, deletions and retention.
5. Verify API compatibility, background jobs, indexes/query plans and offline operation; then rehearse rollback and backup restore.
6. Deploy and validate populated operational workflows. Leave unrelated chatbot code untouched.

This audit does not certify backups, restore recovery, workload capacity, high availability or all security controls. The earlier 100 backend/9 frontend passing tests covered the imported updates, not formal schema normalization or comprehensive production acceptance.

## Evidence appendix

Full catalog and aggregate-only results: [DATABASE_NORMALIZATION_EVIDENCE.json](DATABASE_NORMALIZATION_EVIDENCE.json).

| Application table | Rows at check time | PK columns | FKs | JSONB columns |
| --- | ---: | --- | ---: | --- |
| `agent_artifacts` | 0 | `(id)` | 5 | modification_meta |
| `background_task_history` | 997 | `(id)` | 0 | details, result, payload |
| `chatbot_audit_log` | 55 | `(id)` | 1 | — |
| `conversation_branches` | 5 | `(id)` | 2 | — |
| `conversations` | 5 | `(id)` | 3 | — |
| `deleted_users` | 0 | `(user_id)` | 0 | — |
| `detections` | 0 | `(id)` | 1 | — |
| `faces` | 0 | `(id)` | 2 | — |
| `identities` | 0 | `(id)` | 1 | — |
| `identity_appearances` | 0 | `(id)` | 3 | — |
| `identity_audit_log` | 0 | `(id)` | 3 | action_details, before_state, after_state |
| `identity_embeddings` | 0 | `(id)` | 4 | — |
| `identity_images` | 0 | `(id)` | 2 | — |
| `identity_merges` | 0 | `(id)` | 3 | provenance |
| `identity_relationships` | 0 | `(id)` | 2 | common_pipelines, common_time_patterns |
| `learned_thresholds` | 0 | `(id)` | 0 | extras |
| `live_alert_audit_log` | 0 | `(id)` | 0 | details |
| `live_alert_triggers` | 0 | `(id)` | 4 | — |
| `live_search_alerts` | 0 | `(id)` | 2 | pipeline_ids, active_days, email_recipients, sms_recipients |
| `merge_suggestions` | 0 | `(id)` | 1 | identity_ids, representative_snapshots |
| `message_feedback` | 0 | `(id)` | 2 | — |
| `messages` | 54 | `(id)` | 1 | content_blocks |
| `ml_audit_log` | 2 | `(id)` | 0 | before, after |
| `ml_collection_checkpoints` | 0 | `(id)` | 0 | extras |
| `ml_datasets` | 0 | `(id)` | 0 | split_config, missing_value_report, quality_report, lineage_summary, extraction |
| `ml_drift_reports` | 0 | `(id)` | 1 | baseline_stats, metrics |
| `ml_feature_definitions` | 38 | `(id)` | 0 | params, readiness_requirements |
| `ml_feature_snapshots` | 0 | `(id)` | 0 | features, unavailable_features, source_row_counts |
| `ml_labels` | 0 | `(id)` | 3 | selection |
| `ml_model_thresholds` | 0 | `(id)` | 1 | expected_metrics, cutpoints, quantiles |
| `ml_models` | 0 | `(id)` | 2 | dependency_versions, feature_names, hyperparameters, metrics, quality_gates, evaluation_report, shadow_approval, training_config |
| `ml_pipeline_versions` | 0 | `(id)` | 0 | configuration |
| `ml_predictions` | 0 | `(id)` | 6 | missing_features, unavailable_features, full_features, explanation |
| `ml_retraining_policies` | 4 | `(id)` | 0 | promotion_criteria |
| `ml_shadow_comparisons` | 0 | `(id)` | 3 | missing_features |
| `ml_tracking_runs` | 0 | `(job_id)` | 1 | manifest |
| `ml_worker_heartbeats` | 1 | `(worker_id)` | 0 | — |
| `organizations` | 1 | `(id)` | 0 | — |
| `pending_enrollments` | 0 | `(id)` | 1 | candidates |
| `pipeline_aliases` | 0 | `(old_pipeline_id)` | 1 | — |
| `pipelines` | 0 | `(id)` | 0 | — |
| `risk_model_versions` | 3 | `(id)` | 0 | weights, thresholds, calibration_data |
| `risk_signal_results` | 0 | `(id)` | 1 | raw_value |
| `search_history` | 0 | `(id)` | 1 | filters, exclude_identity_ids, exclude_watchlist_ids, input_quality_scores, results_summary |
| `settings` | 364 | `(id)` | 0 | — |
| `settings_audit_log` | 556 | `(id)` | 1 | — |
| `similarity_model_registry` | 0 | `(id)` | 2 | metrics, quality_gates, comparison |
| `similarity_training_data` | 0 | `(id)` | 3 | — |
| `system_metrics` | 18701 | `(id)` | 0 | — |
| `threat_assessments` | 0 | `(id)` | 2 | signals, limitations |
| `user_authorization_audit_log` | 0 | `(id)` | 2 | old_pipeline_ids, new_pipeline_ids |
| `user_conversation_memory` | 1 | `(id)` | 3 | memory_value |
| `user_conversation_sessions` | 1 | `(id)` | 1 | — |
| `user_pipeline_access` | 0 | `(id)` | 2 | — |
| `user_query_embeddings` | 25 | `(id)` | 2 | — |
| `user_query_history` | 27 | `(id)` | 2 | query_metadata |
| `users` | 2 | `(id)` | 0 | — |
| `watchlist_alerts` | 0 | `(id)` | 5 | — |
| `watchlist_entries` | 0 | `(id)` | 3 | — |
| `watchlists` | 0 | `(id)` | 2 | email_recipients, sms_recipients |
| `webhook_credentials` | 0 | `(id)` | 1 | — |
| `workspace_members` | 1 | `(id)` | 2 | — |
| `workspaces` | 1 | `(id)` | 1 | settings |

## Populated follow-up tests

See [DATABASE_INTEGRITY_TEST_RESULTS.md](DATABASE_INTEGRITY_TEST_RESULTS.md): 41 checks passed and six expected-failure cases reproduced four missing safeguards in an isolated copy of the deployed schema. Production data was unchanged.

# Camera analytics: existing state (Stage 1)

Inspection date: 2026-09-29. VAS source revision `ba6ba9b794`; VMS source revision `ecd8781660`. VAS running application release: `a1dff75` (later documentation commit explains the difference).

**Latest user constraint:** use only data already in the database; add nothing to the database. This stage changed documentation only. No DDL, migrations, inserts, updates, deletes, training, relationship refresh, retention jobs or deployments were performed. Stage 2 requires approval. Normal application activity was not stopped.

## Scope and evidence

Repository inventories covered 1,005 visible VAS files and 323 VMS files; ORM declarations, 53 VAS and 8 VMS migration files, routes, inference/publishing, persistence, analytics, workers, feature pipelines and storage/retention paths were mapped. This is a repository-wide component inventory with targeted source tracing, not a claim that every line or optional integration was executed.

Live inspection used PostgreSQL metadata and aggregate queries under `BEGIN READ ONLY`, a 15-second statement timeout and 2-second lock timeout. No images, names, embeddings, credentials or individual identity records are included in these reports. A bounded Redis type/TTL sample read no values. Database counts are a point-in-time observation; PostgreSQL estimated row counts were not treated as exact.

- VAS database: `face_recognition`, public schema, migration `ff17b8c9d0e1`. Extensions: pgvector 0.8.6, uuid-ossp, pg_stat_statements, plpgsql.
- VMS database: migration `0008_publisher_description`; tables cover pipeline/model/media/publisher/auth configuration, not frame or position history.
- No application SQL views, materialized views or partitioned analytical parent tables were found in VAS public schema; the two views found belong to pg_stat_statements.
- Exact observations at approximately 08:18 UTC: 2 cameras, 471 detections, 471 face-result rows, 34 identities, 359 appearances, 186 cached relationships, 1,328 feature snapshots, 5 datasets, 6 model records, 7 assessments, **0 ML predictions, 0 ML labels, 0 learned thresholds**.
- Newer candidates now include behavior v4 and pair v2. Their training counts remain 286 and 744, respectively; validation and test counts are still zero. Earlier candidate versions remain registered. Counts differ from earlier audit reports because the application continued running.

## Existing components

All VAS ORM locations below refer to `db_models.py`; detailed keys/columns/timestamps appear in the catalog appendix. “ML usefulness” distinguishes usable historical observations from physically measured trajectories.

| Existing source | Grain / primary key | Producer → consumer | Frequency; raw/aggregate | ML usefulness |
|---|---|---|---|---|
| VMS `pipelines`, `inference_engines`, `models`, representations/artifacts, `publishers` | Configuration/revision/artifact, respective declared IDs | Pipeline manager/model registry → inference/publisher | Configuration changes; metadata | Model/camera provenance; no historical positions |
| VMS Ultralytics result and tracker state | Object per processed frame; transient tracker ID | `InferenceEngine/engines/ultralytics_engine.py` → `InferenceNode/pipeline.py` | Configured inference cadence; transient raw output | Object class/confidence/pixel box available in memory; not a retained DB dataset |
| VMS selected publisher job / file outbox | One selected detection delivery; generated event ID | `pipeline._collect_ready_tracks`, `_prepare_job` → webhook | Quality/settling/cooldown driven, not every frame; selected raw event | Replay delivery, not complete trajectory history; successful outbox jobs removed |
| VMS `media_assets`, `pipeline_thumbnails` | Uploaded media asset / current thumbnail | Media and thumbnail registry → UI/inference | Upload or thumbnail update; file references | Not one retained screenshot per detection |
| VAS `pipelines`, `pipeline_aliases` | Camera/pipeline natural string ID; integer row PK / old alias | Webhook/camera management → all analytics | Configuration changes; dimension-like | Camera coordinates, location and timezone; aliases must resolve |
| `detections` | One persisted selected ingest frame/event; integer PK + UUID | `image_processing` → `detection_evidence.persist_detection` → history/UI | Accepted processed events; selected observations | Camera/time/event anchor; no frame dimensions or original object-class inventory |
| `faces` | Face-recognition result within a detection; integer PK | Same transaction → identity/search/evidence | Per persisted result; selected observations | Identity, similarity, ROI box, crop path; similarity is not object confidence |
| `identities` | Current known/unknown entity; UUID | Recognition/enrollment/merge → analytics | Entity changes; current dimension/cache | Entity key; historical status needs audit reconstruction, not current type alone |
| `identity_appearances` | Selected identity sighting; integer PK + nullable unique event ID | `persist_detection` → timelines, pair logic, feature collectors | Per accepted identity evidence; event | Main reusable behavioral history; not a track or a measured dwell interval |
| `identity_embeddings` | Stored embedding sample; integer PK | Recognition/enrollment → vector index | Per accepted quality sample; raw vector with provenance | 512-dimensional identity matching; not geometry or movement |
| `identity_images`, `pending_enrollments` | Gallery/evidence image / enrollment request | Enrollment → gallery/review | Submission/change; references | Image evidence and enrollment outcomes, not trajectory samples |
| `identity_merges`, `identity_audit_log`, `merge_suggestions`, pending merge membership | Merge operation/audit/suggestion/member | Identity mutation/review → lineage and merge UI | On mutation/review; history | Preserve original IDs, merge lineage and decisions |
| `identity_relationships` | One current canonical identity pair; UUID | `IntelligenceService.refresh_relationships` → related-person and relational ML | Explicit refresh; mutable aggregate | Counts, percentage, cameras, first/last; no encounter rows or fixed aggregation period |
| `threat_assessments`, `risk_signal_results` | Assessment / contributing signal; UUIDs | Unified risk/assessment service → review/security UI | Requested/event-driven; derived scores | Versioned score/evidence and workflow, not continuous behavior windows |
| `risk_model_versions`, `learned_thresholds` | Rule profile/version / scoped threshold version | Admin/learning candidate workflow → risk and pair logic | On reviewed change; configuration | Reuse configuration/provenance; no new threshold table needed |
| `watchlists`, entries, alerts | List/member / detection-list alert | Admin and detection matching → alert UI | Config change / matching event | Selection/context and event references, not independent anomaly labels |
| `live_search_alerts`, triggers, audit, pipeline links | Search rule / trigger / workflow audit / membership | Live search/detection → alert workflow | On request, hit or action | Existing event/review-adjacent evidence |
| `search_history`, `similarity_training_data`, `similarity_model_registry` | Search / similarity training example / model version | Search and similarity optimization → search/threshold tooling | On request/training; mixed | Separate face-match problem; do not mix these targets with behavior labels |
| `ml_feature_definitions`, `ml_feature_snapshots`, checkpoints | Feature definition / entity+schema+as-of / collector watermark | Feature collector/builders → datasets/drift/scoring | Collection runs and explicit computation; aggregates | Versioned numeric feature vectors; event and current-state populations differ |
| `ml_labels` | Reviewed/supersedable human or weak label; UUID and idempotency key | Labeling/assessment resolution → supervised train/evaluate | Human action; outcome | Human label separate from prediction; currently empty |
| `ml_datasets` + immutable Parquet | Dataset version registry / snapshot row | Dataset builder → trainers/notebook | Explicit build; immutable extracted features | Reuse saved datasets; do not duplicate operational DB tables |
| `ml_models`, pipeline versions, tracking runs | Model/version / recipe version / tracking link | ML worker → scoring/ML Ops | Training/sync jobs; artifacts | Versioned feature order, imputation, checksum, parameters and evaluation |
| `ml_model_thresholds`, `ml_predictions`, `ml_shadow_comparisons` | Threshold version / scored request / rule-model comparison | Inference/shadow → review/evaluation | On inference; derived evidence | Prediction table currently empty; pair/ranking score persistence incomplete |
| `ml_drift_reports`, retraining policies, ML audit | Model/window report / policy / action | Report worker/admin → monitoring/lineage | Explicit or configured jobs; aggregates | Monitoring metadata, not position history; scheduled retraining gated |
| `background_task_history`, ML heartbeats | Durable task/job / worker | Independent ML worker and supervised tasks → status UI | Enqueue/lease/progress/heartbeat | Job/checkpoint reuse; not camera analytics observations |
| `system_metrics`, Redis caches, WebSocket broadcasts | Host/application sample / cache item / live UI message | Metrics/supervision/events → dashboards | Scheduled/event driven; operational | Not per-camera occupancy or retained object frames |
| Conversations/messages/feedback/query embeddings/artifacts | Chat content, response feedback, text embedding, generated artifact | Assistant services → assistant UI | Request driven | Out of scope for movement labels; chat thumbs-up is not an anomaly outcome |

## Important persistence findings

1. `InferenceNode/pipeline.py::_make_track_key` combines class, tracking session and epoch **internally**. `_build_payload` preserves a detection's raw tracker ID but does not publish that complete scoped track key. Track numbers cannot be assumed globally unique across cameras/restarts.
2. VMS publishes selected best candidates. `capture_clock='application_read'` and `captured_at` represent application frame-read time; file playback can therefore use wall-clock processing time rather than original media PTS. This is not a guarantee of sensor exposure time.
3. VAS `image_processing.py` accepts person/face predictions and skips other classes. Vehicle detections are not represented by its `faces`/appearance history.
4. `backend/core/detection_evidence.py` calls `create_appearance(..., track_id=None, ...)` explicitly. All 359 live appearances indeed have NULL track IDs and NULL end times. A column named `track_id` exists, but this path does not populate it.
5. `faces.bbox_*` receives the clipped upstream ROI coordinates from `image_processing.py`, in input-image pixels. The ORM comment “relative” does not establish normalized coordinates. The crop file dimensions are not the original frame dimensions. All 471 live boxes contain coordinates above 1; this is not itself an error.
6. Face crop paths are retained, but original image dimensions, a stable per-frame source identifier, model class/confidence and a retained stream of every object position are not persisted as a complete detection contract.
7. `IntelligenceService.refresh_relationships` deletes/replaces incident cache rows. Do not call it from a read-only analytical query, and do not interpret `calculated_at` as encounter time.
8. Map “security zones” in `backend/routes/intelligence.py::_security_inputs` are generated circles around camera GPS coordinates, with names influencing display types. They are not versioned, calibrated image polygons or observed zone-entry events.

## Five-service mapping

### 1. Behavior assessment

Two related but different paths exist. Operational assessment uses `SecurityIntelligenceService.detect_anomalies` and `risk_engine.py`: identity appearances, local-hour baselines, new cameras, relationship/activity counts and configurable rule weights. Hour anomalies use circular statistics with workday/weekend/holiday context, baseline/sample floors and a configurable sigma threshold. Unified rule scores are 0–100 triage heuristics.

The optional `behavior_anomaly_model` uses `secintel-features-v2`, person-level as-of snapshots, counts/ratios/recency/cyclic-hour/history features over multi-day windows. Isolation Forest or MAD returns an anomaly score/band in shadow mode. It does **not** consume sequences of x/y positions over 5–30 seconds. Persistence: assessments/signals, and when used, predictions/comparisons; evidence is linked through appearances/detections and crop references. No measured speed, acceleration, zone dwell or walking direction is currently available.

### 2. Pair relationships

`IntelligenceService._calculate_co_appearances` matches identity appearances in same-camera time windows using interval overlap with `COALESCE(end_time,start_time)`; null-ended observations are points, not infinite stays. Cross-camera logic optionally uses camera GPS Haversine distance and time tolerances. This is camera separation, **not distance between two people**. Same-zone and walking-together measurements do not exist.

The cache enforces canonical `identity_id_1 < identity_id_2` and a unique ordered pair. Refresh uses bounded appearance scans; counts are window-match observations, not deduplicated physical encounters. Directional coappearance percentage is defined for the first identity. Pair ML uses `coappearance-features-v1`, canonical pair snapshots and separate count/rate/recency/schema evidence. Legacy cache-derived fields carrying a `90d` suffix must not be interpreted as an exact 90-day encounter count: source limitations are explicit in dataset definitions.

### 3. Social network

The requested-range graph in `SecurityIntelligenceService.build_social_network` derives bounded relationships from appearances, because the mutable cache is not period-specific. It returns nodes, edges, clusters and a heuristic risk rubric. This path is not the same as the graph ML feature collector, which uses relationship observations and readiness checks at collection time. Graph ML features include degree, log weighted degree, PageRank and other topology measures, with minimum nodes/edges/history. No live graph feature snapshots were found. Neither path establishes social intent beyond measured co-occurrences.

### 4. Analyst review queue

`threat_ranking_model` ranks identities from the same person feature schema with reviewed binary labels. It is not a generic persisted candidate inbox. An approved classifier supplies relative ordering, not calibrated threat probabilities. Existing assessments already support open/acknowledged/resolved workflow, actors, notes, model provenance and linked prediction IDs. `AssessmentService.record_outcome` creates a manual **unreviewed** label, which requires the existing ML review step. `ml_labels` preserves source, event time, reviewer, selection metadata and supersession. Face enrollment, merge suggestions and alerts have their own workflows. Do not collapse them into one target definition or overwrite original predictions.

### 5. Numeric experiments

`tabular_regression_model`, `run_spec.py`, `tabular.prepare_rows` and the existing worker use saved feature datasets, explicit numeric targets excluded from predictors, training-only imputation, XGBoost and optional bounded Optuna. Results live in model/dataset/tracking metadata and artifacts; metrics include MAE/RMSE/R² and a training-mean baseline. It currently consumes tabular snapshot rows, not temporal position sequences. No separate experiments copy of operational tables is required. Under the new no-database-write constraint, proposed Stage 2 inspection/export must not invoke training or dataset registration implicitly.

## IDs, clocks and history

Camera key: VAS `pipelines.pipeline_id` string, not the integer surrogate and not necessarily a human location name; VMS has its own configuration IDs. Identity: UUID. Detection: integer ID plus UUID; appearance: integer ID plus event ID. Pair: sorted identity UUIDs. ML snapshot uniqueness: entity type + entity ID + schema + as-of. No durable cross-camera person track ID exists.

VAS ORM dates are generally timestamp-without-time-zone with UTC application semantics. `event_time.observation_time` requires a timezone on incoming ISO timestamps, converts to naive UTC, and records a source; output `iso_utc` restores a UTC representation. Two cameras have NULL timezone; runtime fallback is **UTC**, so business-hour analysis must not assume Beirut local time. Source time and created/computed time are distinct. Future sequence design must never use upload/processing order as physical motion order.

## Queues, Redis, WebSocket and storage

- VAS ingestion uses bounded asyncio batching, executor workers, a local processed-evidence spool under storage and transactional batch writing. HTTP 202 indicates acceptance, not completion. Webhook dedup is bounded and process-local; durable detection UUID replay is separately protected.
- ML tasks use existing PostgreSQL `background_task_history`, leases/heartbeats and an independent worker; no active Celery application was found in the inspected backend/requirements. Relationship refresh is a separate existing background-task path.
- Redis backs transient caches, coordination/locks and WebSocket pub/sub (`websocket_broadcast`). Bounded live inspection found one expiring string key, no retained stream/list position dataset. Absence at one instant is not proof that a cache is never used.
- WebSocket messages broadcast committed detection/appearance/alert and job updates to clients; pub/sub is not a durable analytical source.
- VAS storage volumes include `/app/storage` (crops/evidence/spool), `/app/models/ml` (Parquet/model artifacts), database/vector-related storage and logs. Jupyter reads artifacts through its read-only mount. VMS has model/media/pipeline/data directories and a disk event outbox. No authoritative object-storage mart was found; optional plugins are not assumed deployed.
- Live vector backend is pgvector, identity vectors are 512-dimensional. FAISS is an optional rebuildable index keyed to database embedding rows, not an alternative source of positions. Text-query embeddings are separate (384-dimensional); they are not person embeddings.

## Retention and access

Runtime configuration: detections 30 days, snapshots 90 days, identity embeddings 12 months; ML predictions 180 days, feature snapshots/drift reports 365 days. Full prediction feature-vector sampling is 0.0. The data-retention implementation protects linked prediction evidence; dataset immutability and source retention must be considered separately. Retention can delete images and set detection links NULL; historical exports must tolerate explicit missing evidence. No cleanup was invoked.

`db/laf_ai_readonly.sql` already defines a SELECT allowlist and withholds sensitive columns. It does not grant every ML table. `laf_ai_readonly_drop.sql` is a revocation script, not a data mart implementation, and was not run. Stage 2 must use existing authorized readers and report insufficient access; this plan does not add grants or execute administrative mutation paths.

## Existing indexes and migration conventions

Alembic governs schema changes; PostgreSQL JSONB, UUIDs, explicit FKs/on-delete rules, partial uniqueness and some concurrent index migrations are established conventions. This plan adds none. Live indexes cover camera/time detections; identity/time and camera/time appearances; identity/camera lookup and collector `(created_at,id)`; canonical pair uniqueness; snapshot entity/as-of and computed time; assessment subject/person/camera/time; label subject/review/time; prediction model/time; vector search and embedding provenance. No track-position index can compensate for absent track-position records.

## Source catalog: keys, fields and timestamps

This appendix records declarations for relevant ORM tables; live schema presence was checked separately. A nullable tracking field does not imply a populated track grain. Types below are declarations, not sample values. Runtime pgvector resolves the conditional embedding type. Camera/identity joins follow declared references.

### VAS

#### `pipelines` — `Pipeline`

Source: `VAS/db_models.py:92`. Primary key: `id`. Timestamps: `created_at, updated_at`.

References: no column FK; see source-specific logical linkage.

Columns: `id` (Integer), `pipeline_id` (String(255)), `created_at` (DateTime), `updated_at` (DateTime), `total_detections` (Integer), `is_active` (Integer), `latitude` (Float), `longitude` (Float), `location_name` (String(255)), `timezone` (String(64)).

#### `pipeline_aliases` — `PipelineAlias`

Source: `VAS/db_models.py:121`. Primary key: `old_pipeline_id`. Timestamps: `created_at`.

References: new_pipeline_id → ForeignKey('pipelines.pipeline_id', ondelete='CASCADE').

Columns: `old_pipeline_id` (String(255)), `new_pipeline_id` (String(255)), `created_at` (DateTime).

#### `detections` — `Detection`

Source: `VAS/db_models.py:134`. Primary key: `id`. Timestamps: `timestamp`.

References: pipeline_id → ForeignKey('pipelines.pipeline_id', ondelete='RESTRICT').

Columns: `id` (Integer), `uuid` (String(36)), `pipeline_id` (String(255)), `timestamp` (DateTime), `image_size_bytes` (Integer), `processing_time_ms` (Float), `worker_id` (Integer).

#### `faces` — `Face`

Source: `VAS/db_models.py:165`. Primary key: `id`. Timestamps: `none declared`.

References: detection_id → ForeignKey('detections.id', ondelete='CASCADE'); identity_id → ForeignKey('identities.id', ondelete='SET NULL').

Columns: `id` (Integer), `detection_id` (Integer), `name` (String(255)), `similarity` (Float), `identity_id` (UUID(as_uuid=True)), `label_state` (SQLEnum(LabelState)), `face_image_path` (String(512)), `bbox_x1` (Float), `bbox_y1` (Float), `bbox_x2` (Float), `bbox_y2` (Float).

#### `system_metrics` — `SystemMetrics`

Source: `VAS/db_models.py:217`. Primary key: `id`. Timestamps: `timestamp`.

References: no column FK; see source-specific logical linkage.

Columns: `id` (Integer), `timestamp` (DateTime), `queue_size` (Integer), `processing_count` (Integer), `total_received` (Integer), `total_processed` (Integer), `total_skipped` (Integer), `avg_processing_time_ms` (Float), `active_pipelines` (Integer), `total_faces_detected` (Integer), `cpu_percent` (Float), `memory_percent` (Float), `disk_usage_gb` (Float).

#### `identities` — `Identity`

Source: `VAS/db_models.py:446`. Primary key: `id`. Timestamps: `first_seen_at, last_seen_at, created_at, updated_at`.

References: merged_into_id → ForeignKey('identities.id').

Columns: `id` (UUID(as_uuid=True)), `type` (SQLEnum(IdentityType)), `display_name` (String(255)), `status` (SQLEnum(IdentityStatus)), `person_code` (String(100)), `person_code_key` (String(100)), `first_seen_at` (DateTime), `last_seen_at` (DateTime), `created_at` (DateTime), `updated_at` (DateTime), `best_snapshot_path` (String(512)), `appearances_count` (Integer), `merged_into_id` (UUID(as_uuid=True)).

#### `identity_appearances` — `IdentityAppearance`

Source: `VAS/db_models.py:514`. Primary key: `id`. Timestamps: `start_time, end_time, created_at`.

References: detection_id → ForeignKey('detections.id', ondelete='SET NULL'); identity_id → ForeignKey('identities.id', ondelete='CASCADE'); pipeline_id → ForeignKey('pipelines.pipeline_id', ondelete='RESTRICT').

Columns: `id` (Integer), `event_id` (String(64)), `detection_id` (Integer), `detection_uuid` (String(36)), `location_name` (String(255)), `timestamp_source` (String(32)), `identity_id` (UUID(as_uuid=True)), `pipeline_id` (String(255)), `track_id` (String(255)), `start_time` (DateTime), `end_time` (DateTime), `best_snapshot_path` (String(512)), `created_at` (DateTime).

#### `identity_embeddings` — `IdentityEmbedding`

Source: `VAS/db_models.py:555`. Primary key: `id`. Timestamps: `created_at`.

References: identity_id → ForeignKey('identities.id', ondelete='CASCADE'); detection_id → ForeignKey('detections.id', ondelete='SET NULL'); pipeline_id → ForeignKey('pipelines.pipeline_id', ondelete='RESTRICT'); image_id → ForeignKey('identity_images.id', ondelete='SET NULL').

Columns: `id` (Integer), `identity_id` (UUID(as_uuid=True)), `detection_id` (Integer), `pipeline_id` (String(255)), `faiss_index_type` (String(50)), `quality` (Float), `vector_index_sync_state` (String(16)), `embedding_model_version` (String(64)), `quality_scorer_version` (String(32)), `image_id` (Integer), `created_at` (DateTime), `embedding` (ARRAY(Float)).

#### `identity_images` — `IdentityImage`

Source: `VAS/db_models.py:638`. Primary key: `id`. Timestamps: `created_at, updated_at`.

References: identity_id → ForeignKey('identities.id', ondelete='CASCADE'); created_by → ForeignKey('users.id', ondelete='SET NULL').

Columns: `id` (Integer), `identity_id` (UUID(as_uuid=True)), `storage_path` (String(512)), `original_filename` (String(255)), `file_checksum` (String(64)), `content_type` (String(100)), `file_size` (Integer), `width` (Integer), `height` (Integer), `quality_score` (Float), `quality_scorer_version` (String(32)), `is_primary` (Boolean), `source_type` (String(32)), `processing_status` (String(32)), `failure_reason` (String(255)), `created_at` (DateTime), `created_by` (Integer), `updated_at` (DateTime).

#### `pending_enrollments` — `PendingEnrollment`

Source: `VAS/db_models.py:714`. Primary key: `id`. Timestamps: `created_at, expires_at`.

References: user_id → ForeignKey('users.id', ondelete='CASCADE').

Columns: `id` (Integer), `token_hash` (String(64)), `user_id` (Integer), `display_name` (String(255)), `display_name_key` (String(255)), `storage_path` (String(512)), `original_filename` (String(255)), `file_checksum` (String(64)), `content_type` (String(100)), `file_size` (Integer), `width` (Integer), `height` (Integer), `is_face_image` (Boolean), `embedding_model_version` (String(64)), `detection_model_version` (String(64)), `decision` (String(16)), `top_similarity` (Float), `candidates` (JSONB), `created_at` (DateTime), `expires_at` (DateTime).

#### `merge_suggestions` — `MergeSuggestion`

Source: `VAS/db_models.py:861`. Primary key: `id`. Timestamps: `created_at, reviewed_at, invalidated_at`.

References: reviewed_by → ForeignKey('users.id', ondelete='SET NULL').

Columns: `id` (Integer), `cluster_id` (String(255)), `identity_ids` (JSONB), `confidence` (Float), `status` (SQLEnum(MergeSuggestionStatus)), `representative_snapshots` (JSONB), `created_at` (DateTime), `reviewed_at` (DateTime), `reviewed_by` (Integer), `invalidated_reason` (String(255)), `invalidated_at` (DateTime).

#### `identity_merges` — `IdentityMerge`

Source: `VAS/db_models.py:885`. Primary key: `id`. Timestamps: `merged_at`.

References: from_identity_id → ForeignKey('identities.id'); to_identity_id → ForeignKey('identities.id'); merged_by → ForeignKey('users.id', ondelete='SET NULL').

Columns: `id` (Integer), `from_identity_id` (UUID(as_uuid=True)), `to_identity_id` (UUID(as_uuid=True)), `merged_by` (Integer), `historical_merged_by` (Integer), `merged_at` (DateTime), `notes` (Text), `provenance` (JSONB).

#### `identity_audit_log` — `IdentityAuditLog`

Source: `VAS/db_models.py:918`. Primary key: `id`. Timestamps: `created_at`.

References: user_id → ForeignKey('users.id', ondelete='SET NULL'); identity_id → ForeignKey('identities.id', ondelete='SET NULL'); related_identity_id → ForeignKey('identities.id', ondelete='SET NULL').

Columns: `id` (Integer), `user_id` (Integer), `historical_user_id` (Integer), `username` (String(100)), `action_type` (String(50)), `identity_id` (UUID(as_uuid=True)), `related_identity_id` (UUID(as_uuid=True)), `action_details` (JSONB), `before_state` (JSONB), `after_state` (JSONB), `ip_address` (String(45)), `user_agent` (String(500)), `success` (Boolean), `error_message` (Text), `notes` (Text), `created_at` (DateTime).

#### `settings` — `Setting`

Source: `VAS/db_models.py:966`. Primary key: `id`. Timestamps: `created_at, updated_at`.

References: no column FK; see source-specific logical linkage.

Columns: `id` (Integer), `key` (String(255)), `value` (Text), `value_type` (String(50)), `category` (String(100)), `description` (Text), `is_sensitive` (Boolean), `is_readonly` (Boolean), `created_at` (DateTime), `updated_at` (DateTime).

#### `settings_audit_log` — `SettingsAuditLog`

Source: `VAS/db_models.py:987`. Primary key: `id`. Timestamps: `created_at`.

References: changed_by_user_id → ForeignKey('users.id', ondelete='SET NULL').

Columns: `id` (Integer), `setting_key` (String(255)), `old_value` (Text), `new_value` (Text), `value_type` (String(50)), `changed_by_user_id` (Integer), `changed_by_username` (String(100)), `change_reason` (Text), `action` (String(50)), `ip_address` (String(45)), `user_agent` (Text), `created_at` (DateTime).

#### `background_task_history` — `BackgroundTaskHistory`

Source: `VAS/db_models.py:1016`. Primary key: `id`. Timestamps: `scheduled_time, started_at, completed_at, lease_expires_at, heartbeat_at, cancel_requested_at, created_at, updated_at`.

References: no column FK; see source-specific logical linkage.

Columns: `id` (Integer), `job_id` (String(64)), `task_type` (String(50)), `task_name` (String(200)), `status` (String(20)), `description` (Text), `scheduled_time` (DateTime), `started_at` (DateTime), `completed_at` (DateTime), `duration_seconds` (Float), `progress_percent` (Integer), `success` (Boolean), `details` (JSONB), `result` (JSONB), `retry_count` (Integer), `max_retries` (Integer), `error_code` (String(50)), `error_message` (Text), `created_by_user_id` (Integer), `request_id` (String(64)), `correlation_id` (String(64)), `worker_name` (String(100)), `hostname` (String(100)), `notify_all_users` (Boolean), `queue_name` (String(32)), `payload` (JSONB), `lease_owner` (String(100)), `lease_expires_at` (DateTime), `heartbeat_at` (DateTime), `cancel_requested_at` (DateTime), `created_at` (DateTime), `updated_at` (DateTime).

#### `ml_worker_heartbeats` — `MLWorkerHeartbeat`

Source: `VAS/db_models.py:1074`. Primary key: `worker_id`. Timestamps: `started_at, heartbeat_at`.

References: no column FK; see source-specific logical linkage.

Columns: `worker_id` (String(100)), `hostname` (String(100)), `process_id` (Integer), `status` (String(20)), `current_job_id` (String(64)), `started_at` (DateTime), `heartbeat_at` (DateTime).

#### `similarity_training_data` — `SimilarityTrainingData`

Source: `VAS/db_models.py:1091`. Primary key: `id`. Timestamps: `created_at`.

References: identity_id_1 → ForeignKey('identities.id'); identity_id_2 → ForeignKey('identities.id'); created_by_user_id → ForeignKey('users.id', ondelete='SET NULL').

Columns: `id` (Integer), `identity_id_1` (UUID(as_uuid=True)), `identity_id_2` (UUID(as_uuid=True)), `embedding_similarity` (Float), `pipeline_overlap` (Float), `quality_score_1` (Float), `quality_score_2` (Float), `appearances_diff` (Float), `is_cross_pipeline` (Boolean), `label` (Float), `created_at` (DateTime), `created_by_user_id` (Integer).

#### `similarity_model_registry` — `SimilarityModelRegistry`

Source: `VAS/db_models.py:1138`. Primary key: `id`. Timestamps: `created_at, activated_at, archived_at`.

References: created_by → ForeignKey('users.id', ondelete='SET NULL'); activated_by → ForeignKey('users.id', ondelete='SET NULL').

Columns: `id` (Integer), `model_type` (String(50)), `version` (Integer), `status` (String(20)), `artifact_name` (String(200)), `artifact_path` (Text), `artifact_hash` (String(64)), `training_job_id` (String(64)), `dataset_version` (String(64)), `dataset_hash` (String(64)), `feature_schema_version` (String(64)), `seed` (Integer), `metrics` (JSONB), `quality_gates` (JSONB), `comparison` (JSONB), `created_at` (DateTime), `created_by` (Integer), `activated_at` (DateTime), `activated_by` (Integer), `archived_at` (DateTime), `failure_code` (String(64)), `notes` (Text).

#### `watchlists` — `Watchlist`

Source: `VAS/db_models.py:1221`. Primary key: `id`. Timestamps: `created_at, updated_at, deleted_at`.

References: created_by → ForeignKey('users.id', ondelete='SET NULL'); deleted_by_user_id → ForeignKey('users.id', ondelete='SET NULL').

Columns: `id` (UUID(as_uuid=True)), `name` (String(100)), `description` (Text), `color` (String(7)), `icon` (String(50)), `alert_level` (SQLEnum(WatchlistAlertLevel)), `notify_dashboard` (Boolean), `notify_email` (Boolean), `notify_sms` (Boolean), `notify_webhook` (Boolean), `email_recipients` (JSONB), `sms_recipients` (JSONB), `webhook_url` (Text), `is_active` (Boolean), `created_by` (Integer), `created_at` (DateTime), `updated_at` (DateTime), `version` (Integer), `deleted_at` (DateTime), `deleted_by_user_id` (Integer), `deletion_reason` (Text).

#### `watchlist_entries` — `WatchlistEntry`

Source: `VAS/db_models.py:1263`. Primary key: `id`. Timestamps: `added_at, expires_at`.

References: watchlist_id → ForeignKey('watchlists.id', ondelete='CASCADE'); identity_id → ForeignKey('identities.id', ondelete='CASCADE'); added_by → ForeignKey('users.id', ondelete='SET NULL').

Columns: `id` (UUID(as_uuid=True)), `watchlist_id` (UUID(as_uuid=True)), `identity_id` (UUID(as_uuid=True)), `priority` (SQLEnum(WatchlistEntryPriority)), `notes` (Text), `action_instructions` (Text), `added_by` (Integer), `added_at` (DateTime), `expires_at` (DateTime), `is_active` (Boolean).

#### `watchlist_alerts` — `WatchlistAlert`

Source: `VAS/db_models.py:1291`. Primary key: `id`. Timestamps: `acknowledged_at, created_at`.

References: watchlist_entry_id → ForeignKey('watchlist_entries.id', ondelete='CASCADE'); search_id → ForeignKey('search_history.id', ondelete='SET NULL'); detection_id → ForeignKey('detections.id', ondelete='SET NULL'); pipeline_id → ForeignKey('pipelines.pipeline_id', ondelete='SET NULL'); acknowledged_by → ForeignKey('users.id', ondelete='SET NULL').

Columns: `id` (UUID(as_uuid=True)), `watchlist_entry_id` (UUID(as_uuid=True)), `triggered_by` (String(50)), `search_id` (UUID(as_uuid=True)), `detection_id` (Integer), `similarity_score` (Float), `pipeline_id` (String(255)), `snapshot_path` (String(512)), `acknowledged` (Boolean), `acknowledged_by` (Integer), `acknowledged_at` (DateTime), `notes` (Text), `created_at` (DateTime).

#### `live_search_alerts` — `LiveSearchAlert`

Source: `VAS/db_models.py:1326`. Primary key: `id`. Timestamps: `expiration_date, last_triggered_at, created_at, updated_at`.

References: identity_id → ForeignKey('identities.id', ondelete='CASCADE'); created_by → ForeignKey('users.id', ondelete='SET NULL').

Columns: `id` (UUID(as_uuid=True)), `name` (String(200)), `auto_name` (Boolean), `identity_id` (UUID(as_uuid=True)), `created_by` (Integer), `historical_created_by` (Integer), `alert_level` (String(16)), `min_similarity` (Float), `pipeline_ids` (JSONB), `time_window_enabled` (Boolean), `time_window_start` (String(5)), `time_window_end` (String(5)), `active_days` (JSONB), `cooldown_minutes` (Integer), `notify_dashboard` (Boolean), `notify_email` (Boolean), `notify_sms` (Boolean), `notify_webhook` (Boolean), `email_recipients` (JSONB), `sms_recipients` (JSONB), `webhook_url` (Text), `sound_alert` (Boolean), `auto_capture_snapshot` (Boolean), `auto_record_clip` (Boolean), `clip_duration_seconds` (Integer), `expiration_type` (SQLEnum(LiveAlertExpirationType)), `expiration_date` (DateTime), `expiration_detections` (Integer), `status` (SQLEnum(LiveAlertStatus)), `triggers_count` (Integer), `last_triggered_at` (DateTime), `created_at` (DateTime), `updated_at` (DateTime).

#### `live_alert_triggers` — `LiveAlertTrigger`

Source: `VAS/db_models.py:1392`. Primary key: `id`. Timestamps: `acknowledged_at, created_at`.

References: alert_id → ForeignKey('live_search_alerts.id', ondelete='CASCADE'); detection_id → ForeignKey('detections.id', ondelete='SET NULL'); pipeline_id → ForeignKey('pipelines.pipeline_id', ondelete='SET NULL'); acknowledged_by → ForeignKey('users.id', ondelete='SET NULL').

Columns: `id` (UUID(as_uuid=True)), `alert_id` (UUID(as_uuid=True)), `detection_id` (Integer), `pipeline_id` (String(255)), `similarity_score` (Float), `snapshot_path` (String(512)), `clip_path` (String(512)), `acknowledged` (Boolean), `acknowledged_by` (Integer), `acknowledged_at` (DateTime), `created_at` (DateTime).

#### `live_alert_audit_log` — `LiveAlertAuditLog`

Source: `VAS/db_models.py:1422`. Primary key: `id`. Timestamps: `created_at`.

References: no column FK; see source-specific logical linkage.

Columns: `id` (Integer), `user_id` (Integer), `username` (String(100)), `alert_id` (UUID(as_uuid=True)), `action` (String(50)), `details` (JSONB), `result` (String(20)), `request_id` (String(64)), `ip_address` (String(45)), `created_at` (DateTime).

#### `search_history` — `SearchHistory`

Source: `VAS/db_models.py:1449`. Primary key: `id`. Timestamps: `created_at`.

References: user_id → ForeignKey('users.id', ondelete='SET NULL').

Columns: `id` (UUID(as_uuid=True)), `user_id` (Integer), `historical_user_id` (Integer), `search_type` (SQLEnum(SearchType)), `scope` (String(20)), `top_k` (Integer), `filters` (JSONB), `exclude_identity_ids` (JSONB), `exclude_watchlist_ids` (JSONB), `input_image_hash` (String(64)), `input_faces_count` (Integer), `input_quality_scores` (JSONB), `results_count` (Integer), `results_summary` (JSONB), `watchlist_alerts_count` (Integer), `unique_identities_count` (Integer), `processing_time_ms` (Integer), `ip_address` (String(45)), `user_agent` (Text), `created_at` (DateTime).

#### `identity_relationships` — `IdentityRelationship`

Source: `VAS/db_models.py:1495`. Primary key: `id`. Timestamps: `first_co_appearance, last_co_appearance, calculated_at`.

References: identity_id_1 → ForeignKey('identities.id', ondelete='CASCADE'); identity_id_2 → ForeignKey('identities.id', ondelete='CASCADE').

Columns: `id` (UUID(as_uuid=True)), `identity_id_1` (UUID(as_uuid=True)), `identity_id_2` (UUID(as_uuid=True)), `co_appearance_count` (Integer), `co_appearance_percentage` (Float), `relationship_strength` (SQLEnum(RelationshipStrength)), `common_pipelines` (JSONB), `common_time_patterns` (JSONB), `first_co_appearance` (DateTime), `last_co_appearance` (DateTime), `calculated_at` (DateTime).

#### `threat_assessments` — `ThreatAssessmentRecord`

Source: `VAS/db_models.py:1986`. Primary key: `id`. Timestamps: `source_timestamp, created_at, updated_at, acknowledged_at`.

References: person_id → ForeignKey('identities.id', ondelete='SET NULL'); ml_prediction_id → ForeignKey('ml_predictions.id', ondelete='SET NULL', use_alter=True, name='fk_threat_assessments_ml_prediction').

Columns: `id` (UUID(as_uuid=True)), `subject_type` (String(32)), `subject_id` (String(64)), `person_id` (UUID(as_uuid=True)), `pipeline_id` (String(255)), `location_name` (String(255)), `event_id` (String(64)), `total_risk_score` (Float), `severity` (String(16)), `confidence` (Float), `signals` (JSONB), `model_version` (String(64)), `threshold_version` (String(128)), `explanation` (Text), `limitations` (JSONB), `status` (String(16)), `source_timestamp` (DateTime), `idempotency_key` (String(255)), `created_at` (DateTime), `updated_at` (DateTime), `acknowledged_at` (DateTime), `acknowledged_by` (String(255)), `resolution_status` (String(32)), `resolution_notes` (Text), `decision_mode` (String(16)), `requested_mode` (String(16)), `anomaly_signal_source` (String(8)), `signal_mapping_version` (String(64)), `fallback_reason` (String(64)), `ml_prediction_id` (UUID(as_uuid=True)).

#### `risk_signal_results` — `RiskSignalResult`

Source: `VAS/db_models.py:2062`. Primary key: `id`. Timestamps: `created_at`.

References: assessment_id → ForeignKey('threat_assessments.id', ondelete='CASCADE').

Columns: `id` (UUID(as_uuid=True)), `assessment_id` (UUID(as_uuid=True)), `signal_name` (String(64)), `raw_value` (JSONB), `score` (Float), `weight` (Float), `explanation` (Text), `created_at` (DateTime).

#### `risk_model_versions` — `RiskModelVersion`

Source: `VAS/db_models.py:2093`. Primary key: `id`. Timestamps: `created_at, activated_at`.

References: no column FK; see source-specific logical linkage.

Columns: `id` (UUID(as_uuid=True)), `profile` (String(32)), `version` (String(64)), `weights` (JSONB), `thresholds` (JSONB), `status` (String(16)), `score_type` (String(16)), `calibration_status` (String(32)), `calibration_data` (JSONB), `notes` (Text), `created_at` (DateTime), `activated_at` (DateTime).

#### `learned_thresholds` — `LearnedThreshold`

Source: `VAS/db_models.py:2125`. Primary key: `id`. Timestamps: `created_at, activated_at`.

References: no column FK; see source-specific logical linkage.

Columns: `id` (UUID(as_uuid=True)), `scope_type` (String(16)), `scope_id` (String(255)), `signal_name` (String(64)), `value` (Float), `extras` (JSONB), `sample_count` (Integer), `version` (Integer), `status` (String(16)), `created_at` (DateTime), `activated_at` (DateTime), `activated_by` (String(255)).

#### `ml_feature_definitions` — `MLFeatureDefinition`

Source: `VAS/db_models.py:2170`. Primary key: `id`. Timestamps: `created_at, deactivated_at`.

References: no column FK; see source-specific logical linkage.

Columns: `id` (UUID(as_uuid=True)), `name` (String(128)), `version` (Integer), `entity_type` (String(16)), `value_type` (String(16)), `window` (String(16)), `source` (String(64)), `computation` (String(64)), `params` (JSONB), `leakage_class` (String(32)), `readiness_requirements` (JSONB), `description` (Text), `is_active` (Boolean), `created_at` (DateTime), `created_by` (Integer), `deactivated_at` (DateTime).

#### `ml_feature_snapshots` — `MLFeatureSnapshot`

Source: `VAS/db_models.py:2201`. Primary key: `id`. Timestamps: `as_of_timestamp, event_timestamp, computed_at`.

References: no column FK; see source-specific logical linkage.

Columns: `id` (Integer), `entity_type` (String(16)), `entity_id` (String(128)), `feature_set_version` (String(64)), `as_of_timestamp` (DateTime), `event_timestamp` (DateTime), `computed_at` (DateTime), `features` (JSONB), `unavailable_features` (JSONB), `features_checksum` (String(64)), `local_timezone` (String(64)), `computation_run_id` (String(64)), `source_row_counts` (JSONB).

#### `ml_collection_checkpoints` — `MLCollectionCheckpoint`

Source: `VAS/db_models.py:2235`. Primary key: `id`. Timestamps: `watermark_event_time, last_run_at, updated_at`.

References: no column FK; see source-specific logical linkage.

Columns: `id` (Integer), `collector_name` (String(64)), `watermark_event_time` (DateTime), `watermark_id` (Integer), `late_grace_minutes` (Integer), `last_run_id` (String(64)), `last_run_at` (DateTime), `rows_processed_total` (Integer), `extras` (JSONB), `updated_at` (DateTime).

#### `ml_labels` — `MLLabel`

Source: `VAS/db_models.py:2252`. Primary key: `id`. Timestamps: `event_time, reviewed_at, created_at`.

References: person_id → ForeignKey('identities.id', ondelete='SET NULL'); assessment_id → ForeignKey('threat_assessments.id', ondelete='SET NULL'); supersedes_id → ForeignKey('ml_labels.id').

Columns: `id` (UUID(as_uuid=True)), `subject_type` (String(16)), `subject_id` (String(64)), `person_id` (UUID(as_uuid=True)), `assessment_id` (UUID(as_uuid=True)), `label` (String(16)), `label_kind` (String(16)), `label_definition_version` (String(64)), `confidence` (Float), `source` (String(64)), `event_time` (DateTime), `status` (String(16)), `review_status` (String(16)), `reviewed_by` (String(255)), `reviewed_by_user_id` (Integer), `reviewed_at` (DateTime), `selection` (JSONB), `supersedes_id` (UUID(as_uuid=True)), `notes` (Text), `idempotency_key` (String(255)), `created_at` (DateTime), `created_by` (String(255)), `created_by_user_id` (Integer).

#### `ml_datasets` — `MLDataset`

Source: `VAS/db_models.py:2297`. Primary key: `id`. Timestamps: `source_cutoff, time_range_start, time_range_end, holdout_boundary, created_at`.

References: no column FK; see source-specific logical linkage.

Columns: `id` (UUID(as_uuid=True)), `name` (String(128)), `version` (Integer), `kind` (String(16)), `feature_set_version` (String(64)), `label_definition_version` (String(64)), `source_cutoff` (DateTime), `time_range_start` (DateTime), `time_range_end` (DateTime), `holdout_boundary` (DateTime), `split_config` (JSONB), `row_count` (Integer), `positive_count` (Integer), `negative_count` (Integer), `weak_count` (Integer), `missing_value_report` (JSONB), `quality_report` (JSONB), `checksum` (String(64)), `storage_path` (Text), `storage_bytes` (Integer), `code_version` (String(64)), `status` (String(16)), `build_job_id` (String(64)), `created_at` (DateTime), `created_by` (Integer), `lineage_summary` (JSONB), `definition_name` (String(128)), `definition_version` (String(64)), `extraction` (JSONB), `parquet_sha256` (String(64)), `manifest_path` (Text).

#### `ml_models` — `MLModel`

Source: `VAS/db_models.py:2350`. Primary key: `id`. Timestamps: `submitted_at, validated_at, shadow_started_at, approved_at, rejected_at, archived_at, rolled_back_at, created_at`.

References: dataset_id → ForeignKey('ml_datasets.id', ondelete='SET NULL'); previous_production_id → ForeignKey('ml_models.id', ondelete='SET NULL').

Columns: `id` (UUID(as_uuid=True)), `model_type` (String(64)), `version` (Integer), `stage` (String(16)), `algorithm` (String(64)), `model_purpose` (String(64)), `score_type` (String(32)), `is_probability` (Boolean), `calibration_status` (String(32)), `artifact_name` (String(200)), `artifact_path` (Text), `artifact_hash` (String(64)), `artifact_size_bytes` (Integer), `dependency_versions` (JSONB), `feature_set_version` (String(64)), `feature_names` (JSONB), `dataset_id` (UUID(as_uuid=True)), `training_job_id` (String(64)), `seed` (Integer), `hyperparameters` (JSONB), `training_config` (JSONB), `code_version` (String(64)), `metrics` (JSONB), `quality_gates` (JSONB), `evaluation_report` (JSONB), `submitted_at` (DateTime), `validated_at` (DateTime), `shadow_approval` (JSONB), `shadow_started_at` (DateTime), `approved_at` (DateTime), `approved_by` (String(255)), `rejected_at` (DateTime), `rejected_by` (String(255)), `rejection_reason` (Text), `archived_at` (DateTime), `rolled_back_at` (DateTime), `rollback_reason` (Text), `previous_production_id` (UUID(as_uuid=True)), `failure_code` (String(64)), `notes` (Text), `created_at` (DateTime), `created_by` (Integer).

#### `ml_pipeline_versions` — `MLPipelineVersion`

Source: `VAS/db_models.py:2436`. Primary key: `id`. Timestamps: `created_at`.

References: no column FK; see source-specific logical linkage.

Columns: `id` (UUID(as_uuid=True)), `name` (String(128)), `version` (Integer), `configuration` (JSONB), `created_at` (DateTime), `created_by` (Integer).

#### `ml_tracking_runs` — `MLTrackingRun`

Source: `VAS/db_models.py:2448`. Primary key: `job_id`. Timestamps: `updated_at`.

References: model_id → ForeignKey('ml_models.id', ondelete='SET NULL').

Columns: `job_id` (String(64)), `model_id` (UUID(as_uuid=True)), `run_id` (String(64)), `registered_name` (String(128)), `registered_version` (String(32)), `status` (String(32)), `manifest` (JSONB), `last_error` (String(500)), `attempts` (Integer), `updated_at` (DateTime).

#### `ml_model_thresholds` — `MLModelThreshold`

Source: `VAS/db_models.py:2463`. Primary key: `id`. Timestamps: `created_at, activated_at, retired_at`.

References: model_id → ForeignKey('ml_models.id', ondelete='CASCADE').

Columns: `id` (UUID(as_uuid=True)), `model_id` (UUID(as_uuid=True)), `scope_type` (String(16)), `scope_id` (String(255)), `version` (Integer), `status` (String(16)), `cutpoints` (JSONB), `quantiles` (JSONB), `source` (String(32)), `expected_metrics` (JSONB), `sample_count` (Integer), `created_at` (DateTime), `activated_at` (DateTime), `activated_by` (String(255)), `retired_at` (DateTime), `retired_by` (String(255)), `notes` (Text).

#### `ml_predictions` — `MLPrediction`

Source: `VAS/db_models.py:2511`. Primary key: `id`. Timestamps: `event_time, as_of_timestamp, outcome_recorded_at, created_at`.

References: person_id → ForeignKey('identities.id', ondelete='SET NULL'); model_id → ForeignKey('ml_models.id', ondelete='RESTRICT'); snapshot_id → ForeignKey('ml_feature_snapshots.id', ondelete='SET NULL'); threshold_id → ForeignKey('ml_model_thresholds.id', ondelete='RESTRICT'); assessment_id → ForeignKey('threat_assessments.id', ondelete='SET NULL'); outcome_label_id → ForeignKey('ml_labels.id', ondelete='SET NULL').

Columns: `id` (UUID(as_uuid=True)), `subject_type` (String(16)), `subject_id` (String(64)), `person_id` (UUID(as_uuid=True)), `pipeline_id` (String(255)), `model_id` (UUID(as_uuid=True)), `model_type` (String(64)), `model_version_label` (String(128)), `model_purpose` (String(64)), `requested_mode` (String(16)), `actual_mode_used` (String(16)), `fallback_reason` (String(64)), `snapshot_id` (Integer), `feature_set_version` (String(64)), `features_checksum` (String(64)), `missing_features` (JSONB), `unavailable_features` (JSONB), `full_features` (JSONB), `behavioral_anomaly_score` (Float), `normalized_anomaly_score` (Float), `ml_anomaly_band` (String(16)), `score_type` (String(32)), `is_probability` (Boolean), `calibration_status` (String(32)), `threshold_id` (UUID(as_uuid=True)), `threshold_version` (String(64)), `explanation` (JSONB), `assessment_id` (UUID(as_uuid=True)), `event_time` (DateTime), `as_of_timestamp` (DateTime), `latency_ms` (Float), `outcome_label_id` (UUID(as_uuid=True)), `outcome_label` (String(16)), `outcome_recorded_at` (DateTime), `idempotency_key` (String(255)), `created_at` (DateTime).

#### `ml_shadow_comparisons` — `MLShadowComparison`

Source: `VAS/db_models.py:2582`. Primary key: `id`. Timestamps: `created_at`.

References: prediction_id → ForeignKey('ml_predictions.id', ondelete='CASCADE'); model_id → ForeignKey('ml_models.id', ondelete='RESTRICT'); assessment_id → ForeignKey('threat_assessments.id', ondelete='SET NULL').

Columns: `id` (UUID(as_uuid=True)), `prediction_id` (UUID(as_uuid=True)), `model_id` (UUID(as_uuid=True)), `assessment_id` (UUID(as_uuid=True)), `subject_id` (String(64)), `pipeline_id` (String(255)), `rule_threat_score` (Float), `rule_threat_severity` (String(16)), `behavioral_anomaly_score` (Float), `ml_anomaly_band` (String(16)), `rule_would_alert` (Boolean), `ml_would_flag_anomaly` (Boolean), `operational_disagreement` (String(16)), `ml_failed` (Boolean), `failure_reason` (String(64)), `ml_latency_ms` (Float), `missing_features` (JSONB), `created_at` (DateTime).

#### `ml_drift_reports` — `MLDriftReport`

Source: `VAS/db_models.py:2630`. Primary key: `id`. Timestamps: `baseline_start, baseline_end, window_start, window_end, created_at`.

References: model_id → ForeignKey('ml_models.id', ondelete='CASCADE').

Columns: `id` (UUID(as_uuid=True)), `report_kind` (String(32)), `model_id` (UUID(as_uuid=True)), `scope_type` (String(16)), `scope_id` (String(255)), `baseline_start` (DateTime), `baseline_end` (DateTime), `baseline_stats` (JSONB), `baseline_sample_count` (Integer), `window_start` (DateTime), `window_end` (DateTime), `sample_count` (Integer), `insufficient_data` (Boolean), `metrics` (JSONB), `severity` (String(16)), `job_id` (String(64)), `created_at` (DateTime).

#### `ml_retraining_policies` — `MLRetrainingPolicy`

Source: `VAS/db_models.py:2667`. Primary key: `id`. Timestamps: `last_triggered_at, updated_at`.

References: no column FK; see source-specific logical linkage.

Columns: `id` (Integer), `model_type` (String(64)), `enabled` (Boolean), `schedule_interval_hours` (Integer), `min_new_labels` (Integer), `min_total_labels` (Integer), `cooldown_hours` (Integer), `min_drift_reports` (Integer), `promotion_criteria` (JSONB), `last_triggered_at` (DateTime), `last_trigger_reason` (String(128)), `updated_at` (DateTime), `updated_by` (String(255)).

#### `ml_audit_log` — `MLAuditLog`

Source: `VAS/db_models.py:2688`. Primary key: `id`. Timestamps: `created_at`.

References: no column FK; see source-specific logical linkage.

Columns: `id` (Integer), `action` (String(64)), `object_type` (String(32)), `object_id` (String(64)), `actor_user_id` (Integer), `actor_username` (String(100)), `before` (JSONB), `after` (JSONB), `reason` (Text), `ip_address` (String(45)), `request_id` (String(64)), `created_at` (DateTime).

### VMS

#### `pipelines` — `Pipeline`

Source: `VMS/InferenceNode/data_models.py:50`. Primary key: `id`. Timestamps: `created_at, updated_at`.

References: owner_id → ForeignKey('users.id', ondelete='SET NULL').

Columns: `id` (Integer), `pipeline_id` (String(255)), `owner_id` (Integer), `owner_username` (String(100)), `name` (String(255)), `description` (Text), `config` (JSON), `status` (String(32)), `model_id` (String(255)), `node_id` (String(255)), `created_at` (DateTime), `updated_at` (DateTime).

#### `models` — `ModelRecord`

Source: `VMS/InferenceNode/data_models.py:134`. Primary key: `id`. Timestamps: `created_at, updated_at`.

References: uploader_id → ForeignKey('users.id', ondelete='SET NULL').

Columns: `id` (Integer), `model_id` (String(255)), `uploader_id` (Integer), `uploader_username` (String(100)), `name` (String(255)), `engine_type` (String(100)), `filename` (String(255)), `path` (String(512)), `meta` (JSON), `created_at` (DateTime), `description` (Text), `task` (String(64)), `framework` (String(64)), `version` (String(64)), `status` (String(16)), `validation_status` (String(32)), `reason` (String(64)), `updated_at` (DateTime).

#### `model_representations` — `ModelRepresentation`

Source: `VMS/InferenceNode/data_models.py:170`. Primary key: `id`. Timestamps: `created_at, last_verified_at`.

References: model_id → ForeignKey('models.id', ondelete='CASCADE').

Columns: `id` (Integer), `model_id` (Integer), `format` (String(32)), `kind` (String(16)), `required` (Boolean), `precision` (String(16)), `device_family` (String(32)), `runtime` (String(32)), `manifest_sha256` (String(64)), `status` (String(16)), `validation_status` (String(32)), `reason` (String(64)), `created_at` (DateTime), `last_verified_at` (DateTime).

#### `model_artifacts` — `ModelArtifact`

Source: `VMS/InferenceNode/data_models.py:203`. Primary key: `id`. Timestamps: `created_at, last_verified_at`.

References: model_id → ForeignKey('models.id', ondelete='CASCADE'); representation_id → ForeignKey('model_representations.id', ondelete='CASCADE').

Columns: `id` (Integer), `model_id` (Integer), `representation_id` (Integer), `relative_path` (String(1024)), `sha256` (String(64)), `size_bytes` (BigInteger), `status` (String(16)), `validation_status` (String(32)), `reason` (String(64)), `created_at` (DateTime), `last_verified_at` (DateTime), `meta` (JSON), `verified_size_bytes` (BigInteger), `verified_mtime_ns` (BigInteger), `verified_ctime_ns` (BigInteger), `verified_inode` (BigInteger), `verified_device` (BigInteger).

#### `inference_engines` — `InferenceEngineRecord`

Source: `VMS/InferenceNode/data_models.py:242`. Primary key: `id`. Timestamps: `created_at, updated_at, last_verified_at`.

References: created_by → ForeignKey('users.id', ondelete='SET NULL').

Columns: `id` (Integer), `engine_key` (String(128)), `class_name` (String(255)), `display_name` (String(255)), `engine_type` (String(64)), `version` (String(64)), `origin` (String(16)), `status` (String(16)), `validation_status` (String(32)), `reason` (String(64)), `enabled` (Boolean), `relative_path` (String(1024)), `sha256` (String(64)), `size_bytes` (BigInteger), `shipped_version` (String(64)), `shipped_sha256` (String(64)), `created_by` (Integer), `created_at` (DateTime), `updated_at` (DateTime), `last_verified_at` (DateTime), `meta` (JSON), `verified_size_bytes` (BigInteger), `verified_mtime_ns` (BigInteger), `verified_ctime_ns` (BigInteger), `verified_inode` (BigInteger), `verified_device` (BigInteger).

#### `publishers` — `Publisher`

Source: `VMS/InferenceNode/data_models.py:291`. Primary key: `id`. Timestamps: `created_at, updated_at`.

References: created_by → ForeignKey('users.id', ondelete='SET NULL').

Columns: `id` (Integer), `publisher_id` (String(36)), `name` (String(255)), `description` (Text), `type` (String(64)), `kind` (String(32)), `enabled` (Boolean), `config` (JSON), `created_by` (Integer), `created_at` (DateTime), `updated_at` (DateTime).

#### `media_assets` — `MediaAsset`

Source: `VMS/InferenceNode/data_models.py:329`. Primary key: `id`. Timestamps: `created_at, last_verified_at`.

References: created_by → ForeignKey('users.id', ondelete='SET NULL').

Columns: `id` (Integer), `media_id` (String(36)), `relative_path` (String(1024)), `original_filename` (String(255)), `media_type` (String(32)), `sha256` (String(64)), `size_bytes` (BigInteger), `duration` (Integer), `width` (Integer), `height` (Integer), `status` (String(16)), `validation_status` (String(32)), `reason` (String(64)), `created_by` (Integer), `created_at` (DateTime), `last_verified_at` (DateTime), `verified_size_bytes` (BigInteger), `verified_mtime_ns` (BigInteger), `verified_ctime_ns` (BigInteger), `verified_inode` (BigInteger), `verified_device` (BigInteger).

#### `pipeline_thumbnails` — `PipelineThumbnail`

Source: `VMS/InferenceNode/data_models.py:365`. Primary key: `id`. Timestamps: `created_at, last_verified_at`.

References: pipeline_id → ForeignKey('pipelines.id', ondelete='CASCADE').

Columns: `id` (Integer), `pipeline_id` (Integer), `relative_path` (String(1024)), `sha256` (String(64)), `size_bytes` (BigInteger), `status` (String(16)), `validation_status` (String(32)), `reason` (String(64)), `created_at` (DateTime), `last_verified_at` (DateTime), `verified_size_bytes` (BigInteger), `verified_mtime_ns` (BigInteger), `verified_ctime_ns` (BigInteger), `verified_inode` (BigInteger), `verified_device` (BigInteger).

### Association tables verified in live schema

`live_alert_pipeline_links`: `alert_id` (uuid), `pipeline_id` (character varying). Reuse existing membership linkage; no analytical copy.

`pending_merge_members`: `suggestion_id` (integer), `identity_id` (uuid). Reuse existing membership linkage; no analytical copy.

Association integrity is maintained by the existing `fee5f6a7b8c9_operational_integrity.py` migration and synchronization triggers. `live_alert_pipeline_links` has primary key `(alert_id,pipeline_id)` and references live search alerts (CASCADE) and cameras (RESTRICT). `pending_merge_members` has primary key `(suggestion_id,identity_id)` and references merge suggestions (CASCADE) and identities (RESTRICT). These were verified against live constraints; they were not created by this audit.

### Live analytical index inventory

Existing definitions below were read from pg_indexes. No index was created or changed.

```sql
CREATE UNIQUE INDEX identity_appearances_pkey ON public.identity_appearances USING btree (id);
CREATE INDEX idx_appearance_identity_pipeline ON public.identity_appearances USING btree (identity_id, pipeline_id);
CREATE INDEX idx_appearance_identity_start ON public.identity_appearances USING btree (identity_id, start_time);
CREATE INDEX idx_appearance_pipeline ON public.identity_appearances USING btree (pipeline_id, start_time);
CREATE INDEX ix_identity_appearances_start_time ON public.identity_appearances USING btree (start_time);
CREATE INDEX idx_appearance_created_at_id ON public.identity_appearances USING btree (created_at, id);
CREATE UNIQUE INDEX uq_appearance_event_id ON public.identity_appearances USING btree (event_id);
CREATE INDEX idx_appearance_camera_latest ON public.identity_appearances USING btree (identity_id, pipeline_id, start_time, id);
CREATE UNIQUE INDEX identity_relationships_pkey ON public.identity_relationships USING btree (id);
CREATE INDEX idx_relationship_identity1 ON public.identity_relationships USING btree (identity_id_1);
CREATE INDEX idx_relationship_identity2 ON public.identity_relationships USING btree (identity_id_2);
CREATE UNIQUE INDEX idx_relationship_pair ON public.identity_relationships USING btree (identity_id_1, identity_id_2);
CREATE UNIQUE INDEX detections_pkey ON public.detections USING btree (id);
CREATE INDEX idx_detection_pipeline_timestamp ON public.detections USING btree (pipeline_id, "timestamp");
CREATE INDEX idx_detection_timestamp ON public.detections USING btree ("timestamp");
CREATE UNIQUE INDEX ix_detections_uuid ON public.detections USING btree (uuid);
CREATE UNIQUE INDEX faces_pkey ON public.faces USING btree (id);
CREATE INDEX idx_face_detection ON public.faces USING btree (detection_id, name);
CREATE INDEX idx_face_name ON public.faces USING btree (name);
CREATE INDEX ix_faces_identity_id ON public.faces USING btree (identity_id);
CREATE INDEX ix_faces_label_state ON public.faces USING btree (label_state);
CREATE UNIQUE INDEX ml_feature_snapshots_pkey ON public.ml_feature_snapshots USING btree (id);
CREATE INDEX idx_ml_snapshot_computed ON public.ml_feature_snapshots USING btree (computed_at);
CREATE INDEX idx_ml_snapshot_entity_asof ON public.ml_feature_snapshots USING btree (entity_type, entity_id, as_of_timestamp);
CREATE INDEX idx_ml_snapshot_run ON public.ml_feature_snapshots USING btree (computation_run_id);
CREATE UNIQUE INDEX uq_ml_snapshot_identity ON public.ml_feature_snapshots USING btree (entity_type, entity_id, feature_set_version, as_of_timestamp);
CREATE UNIQUE INDEX identity_embeddings_pkey ON public.identity_embeddings USING btree (id);
CREATE INDEX idx_embedding_identity_created ON public.identity_embeddings USING btree (identity_id, created_at);
CREATE INDEX ix_identity_embeddings_created_at ON public.identity_embeddings USING btree (created_at);
CREATE INDEX ix_identity_embeddings_detection_id ON public.identity_embeddings USING btree (detection_id);
CREATE INDEX ix_identity_embeddings_pipeline_id ON public.identity_embeddings USING btree (pipeline_id);
CREATE INDEX idx_embedding_vector_hnsw ON public.identity_embeddings USING hnsw (embedding vector_cosine_ops) WITH (m='32', ef_construction='128');
CREATE INDEX ix_identity_embeddings_image_id ON public.identity_embeddings USING btree (image_id);
CREATE INDEX idx_embedding_sync_state ON public.identity_embeddings USING btree (vector_index_sync_state);
CREATE UNIQUE INDEX threat_assessments_pkey ON public.threat_assessments USING btree (id);
CREATE UNIQUE INDEX uq_assessment_idempotency ON public.threat_assessments USING btree (idempotency_key);
CREATE INDEX ix_threat_assessments_subject_type ON public.threat_assessments USING btree (subject_type);
CREATE INDEX ix_threat_assessments_subject_id ON public.threat_assessments USING btree (subject_id);
CREATE INDEX ix_threat_assessments_person_id ON public.threat_assessments USING btree (person_id);
CREATE INDEX ix_threat_assessments_pipeline_id ON public.threat_assessments USING btree (pipeline_id);
CREATE INDEX ix_threat_assessments_severity ON public.threat_assessments USING btree (severity);
CREATE INDEX ix_threat_assessments_status ON public.threat_assessments USING btree (status);
CREATE INDEX ix_threat_assessments_created_at ON public.threat_assessments USING btree (created_at);
CREATE INDEX idx_assessment_subject ON public.threat_assessments USING btree (subject_type, subject_id, created_at);
CREATE INDEX idx_assessment_person_created ON public.threat_assessments USING btree (person_id, created_at);
CREATE INDEX idx_assessment_severity_status ON public.threat_assessments USING btree (severity, status);
CREATE INDEX idx_assessment_pipeline_created ON public.threat_assessments USING btree (pipeline_id, created_at);
CREATE UNIQUE INDEX ml_labels_pkey ON public.ml_labels USING btree (id);
CREATE UNIQUE INDEX ml_labels_idempotency_key_key ON public.ml_labels USING btree (idempotency_key);
CREATE INDEX idx_ml_label_created ON public.ml_labels USING btree (created_at);
CREATE INDEX idx_ml_label_review ON public.ml_labels USING btree (label, label_kind, review_status);
CREATE INDEX idx_ml_label_subject ON public.ml_labels USING btree (subject_type, subject_id, status);
CREATE INDEX ix_ml_labels_person_id ON public.ml_labels USING btree (person_id);
```

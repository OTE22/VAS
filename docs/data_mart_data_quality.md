# Data mart data quality — Stage 1

Read-only observation on 2026-09-29 at approximately 08:18 UTC. No data was repaired, deleted, relabeled or backfilled. These checks concern the existing data, not a new mart implementation. The production application remained active, so results are not a permanent database invariant.

## Results

| Check | Observed result | Interpretation / treatment |
|---|---:|---|
| Camera, detection, face, identity, appearance, relationship counts | 2 / 471 / 471 / 34 / 359 / 186 | Exact SQL counts; not PostgreSQL row estimates |
| Appearances missing track ID | **359 / 359** | Position/track reconstruction unavailable; source writer explicitly supplies None |
| Appearances missing end time | **359 / 359** | Treat as sighting points; duration, occupancy and continuous overlap unknown |
| Appearances missing detection or event reference | 0 / 0 | Existing joinable lineage; not proof of upstream exactly-once delivery |
| Appearances missing image reference | **52 / 359** | Preserve NULL; no evidence image fabricated |
| Face rows missing image reference | **52 / 471** | Evidence coverage incomplete |
| Missing original frame dimensions | Absent from persisted detection/face schema | Cannot validate or produce normalized positions from retained rows |
| Face boxes missing a coordinate | 0 | All retained boxes have numeric columns |
| Box nonpositive width/height | 0 | All stored boxes pass ordering check |
| Nonfinite box coordinate (NaN/infinity) | 0 | Additional read-only numeric check |
| Negative box origin | 0 | Input path clips into image bounds before storing |
| Box coordinate above 1 | **471 / 471** | Pixel coordinates; not proof of invalid normalized values |
| Face similarity outside [0,1] | 0 | These are recognition similarities, not detector confidences |
| Face rows without identity | **112 / 471** | Nullable/retained history; do not classify all as “unknown person” or orphan IDs |
| Duplicate non-null identity within one detection | 0 groups | Source-level grouping check; different detections can repeat an identity |
| Duplicate identity/camera/start-time groups | **1 group** | Inspect original event IDs before dedup policy; no automatic removal |
| Negative appearance duration | 0 | Most ends are missing; passing this check does not establish valid dwell data |
| Appearances more than 5 minutes in future | 0 | Query-time diagnostic only; no capture-clock accuracy certification |
| Appearance time span | Sept 16 05:24:33 to Sept 28 10:56:43 UTC | Sparse recent retained history, not a continuous stream |
| Timestamp source | 359 `camera_reported` | VMS uses application-read timestamp; distinguish receiver source label from native capture clock |
| Face → detection orphans | 0 | FK-backed join is intact for inspected rows |
| Appearance → identity/camera orphans | 0 / 0 | No broken non-null joins found |
| Self-pairs or reversed canonical identity pair | 0 | Database CHECK and unique pair constraint already exist |
| Negative pair count / span | 0 / 0 | Basic aggregate validity only |
| Pair percentage outside [0,100] | 0 | Stored percentage is directional; avoid symmetric reinterpretation |
| Relationship calculation times | Sept 21 08:50:44–46 UTC | Cache predates latest sightings by days; do not treat it as a current period snapshot |
| Identity cached count differs from appearance count | **3 identities** | Use source aggregates for analytics; no counter repair performed |
| Cameras missing GPS | 0 / 2 | Coordinates exist; calibration/location accuracy not measured |
| Cameras missing IANA timezone | **2 / 2** | Live default is UTC; do not silently assume local hour |
| Feature populations | 398 person/v2; 930 pair/v1; no graph rows | Dataset definitions must preserve schema/entity population and event vs current-state distinctions |
| Reviewed labels / persisted ML predictions | **0 / 0** | Outcome-based model evaluation/feedback dataset currently empty |
| Behavior and pair fitted candidates | 286 / 744 training rows; **0 validation, 0 test** | No held-out accuracy claim possible |
| Durable position and zone records | No such retained grain found | Motion/zone quality tests are unavailable, not passed |

## Required validations for proposed read-only transformations

- **Duplicates:** detection UUID and appearance event ID uniqueness already help. Preserve both source IDs for pair matches; canonicalize sides once. Same identity/time is not automatically duplicate evidence.
- **Timestamps:** UTC input contract; explicit as-of upper bound; stable tie policy. Never divide by zero/nonpositive `dt`. No current track population exists on which to validate monotonic positions.
- **Coordinates:** retain pixel coordinate-space metadata. Without original frame dimensions, normalization checks must return unavailable. A crop cannot supply the original frame scale; camera GPS cannot supply image coordinates.
- **Motion:** do not infer velocity/acceleration from separate unidentified ROIs. Do not declare an “impossible speed” threshold in metres/second without calibrated physical coordinates. Actual irregular intervals, not configured FPS, determine `dt` if suitable source data ever exists.
- **Tracks:** absent scoped track IDs mean no track-position/summary output. Identity associations do not prove continuous tracking; camera/session resets must break sequences.
- **Camera windows:** count each source independently; guard against fanout joins. Validate bucket boundaries and interval limits. Empty source windows are no saved observations, not confirmed empty scenes.
- **Pair matches:** no self-pairs; no duplicate A–B/B–A; require both source appearance IDs; tolerance windows are not physical encounter duration. Aggregate distinct evidence, not duplicated join rows.
- **Graph periods:** source cutoff/period and population/caps must be visible. A query-cap shortfall is insufficient evidence, not a disconnected or low-risk person.
- **Labels:** preserve prediction/model version independently of human outcomes. Only actually reviewed labels satisfy supervised evidence rules. NULL outcomes remain unreviewed; no synthetic labels from rule scores.
- **Late arrivals:** filter by event time for analytical meaning and record query/processing time. A later read may include late-ingested older events; reuse recorded immutable dataset snapshots when historical reproducibility is required. No new checkpoint rows may be written under the current constraint.
- **Missing references:** report NULL/missing evidence and retained-source limitations rather than throwing away the entire observation. Respect FKs, merges and retention; never delete legacy rows during query normalization.

## Inspection limits

No full image-content or filesystem-reference integrity scan was performed; image-reference NULL counts do not establish that every non-null path still exists. Embedding values and image contents were not read. No real-time camera acquisition, 30-camera load test, capture-clock synchronization test, geometry calibration or model training/evaluation was run. Positions, zones, physical speed and next-zone targets cannot be validated from absent data.

The live checks used SQL aggregate queries and catalog inspection. No new application tests were added or executed in Stage 1. Prior release regression tests do not validate a future mart implementation. Stage 2 test work remains subject to approval and the no-database-change boundary.

## Reproducible inspection evidence

The following SQL is the read-only audit query used here. It is not a migration, scheduled job or mutation. Do not run refresh/cleanup/learning functions as part of this inspection.

```sql
BEGIN READ ONLY;
SET LOCAL statement_timeout = '15s';
SET LOCAL lock_timeout = '2s';
SELECT json_build_object('check','counts','observed_at',now(),'pipelines',(SELECT count(*) FROM pipelines),'detections',(SELECT count(*) FROM detections),'faces',(SELECT count(*) FROM faces),'identities',(SELECT count(*) FROM identities),'appearances',(SELECT count(*) FROM identity_appearances),'relationships',(SELECT count(*) FROM identity_relationships),'feature_snapshots',(SELECT count(*) FROM ml_feature_snapshots),'datasets',(SELECT count(*) FROM ml_datasets),'models',(SELECT count(*) FROM ml_models),'predictions',(SELECT count(*) FROM ml_predictions),'labels',(SELECT count(*) FROM ml_labels),'assessments',(SELECT count(*) FROM threat_assessments),'thresholds',(SELECT count(*) FROM learned_thresholds));
SELECT json_build_object('check','appearances','missing_track',count(*) FILTER(WHERE track_id IS NULL),'missing_end',count(*) FILTER(WHERE end_time IS NULL),'missing_detection',count(*) FILTER(WHERE detection_id IS NULL),'missing_event',count(*) FILTER(WHERE event_id IS NULL),'missing_snapshot',count(*) FILTER(WHERE best_snapshot_path IS NULL),'negative_duration',count(*) FILTER(WHERE end_time < start_time),'future_start',count(*) FILTER(WHERE start_time > now() + interval '5 minutes'),'first',min(start_time),'last',max(start_time)) FROM identity_appearances;
SELECT json_build_object('check','appearance_duplicate_timestamp_groups','count',count(*)) FROM (SELECT identity_id,pipeline_id,start_time FROM identity_appearances GROUP BY 1,2,3 HAVING count(*)>1) s;
SELECT json_build_object('check','timestamp_sources','source',timestamp_source,'count',count(*)) FROM identity_appearances GROUP BY timestamp_source;
SELECT json_build_object('check','faces','missing_bbox',count(*) FILTER(WHERE bbox_x1 IS NULL OR bbox_y1 IS NULL OR bbox_x2 IS NULL OR bbox_y2 IS NULL),'invalid_bbox_order',count(*) FILTER(WHERE bbox_x2<=bbox_x1 OR bbox_y2<=bbox_y1),'negative_bbox_origin',count(*) FILTER(WHERE bbox_x1<0 OR bbox_y1<0),'bbox_coordinates_above_one',count(*) FILTER(WHERE greatest(bbox_x1,bbox_y1,bbox_x2,bbox_y2)>1),'missing_identity',count(*) FILTER(WHERE identity_id IS NULL),'missing_image',count(*) FILTER(WHERE face_image_path IS NULL),'similarity_outside_unit',count(*) FILTER(WHERE similarity<0 OR similarity>1)) FROM faces;
SELECT json_build_object('check','face_same_detection_identity_groups','count',count(*)) FROM (SELECT detection_id,identity_id FROM faces WHERE identity_id IS NOT NULL GROUP BY 1,2 HAVING count(*)>1) s;
SELECT json_build_object('check','relationships','self_or_reversed',count(*) FILTER(WHERE identity_id_1>=identity_id_2),'percentage_outside_range',count(*) FILTER(WHERE co_appearance_percentage<0 OR co_appearance_percentage>100),'negative_count',count(*) FILTER(WHERE co_appearance_count<0),'negative_span',count(*) FILTER(WHERE last_co_appearance<first_co_appearance),'oldest_calculated',min(calculated_at),'latest_calculated',max(calculated_at)) FROM identity_relationships;
SELECT json_build_object('check','orphan_relations','face_detection',(SELECT count(*) FROM faces f LEFT JOIN detections d ON d.id=f.detection_id WHERE d.id IS NULL),'appearance_identity',(SELECT count(*) FROM identity_appearances a LEFT JOIN identities i ON i.id=a.identity_id WHERE i.id IS NULL),'appearance_camera',(SELECT count(*) FROM identity_appearances a LEFT JOIN pipelines p ON p.pipeline_id=a.pipeline_id WHERE p.id IS NULL));
SELECT json_build_object('check','camera_metadata','missing_coordinates',count(*) FILTER(WHERE latitude IS NULL OR longitude IS NULL),'missing_timezone',count(*) FILTER(WHERE timezone IS NULL)) FROM pipelines;
SELECT json_build_object('check','identity_counter_mismatch','count',count(*)) FROM identities i WHERE i.appearances_count<>(SELECT count(*) FROM identity_appearances a WHERE a.identity_id=i.id);
SELECT json_build_object('check','feature_population','entity_type',entity_type,'schema',feature_set_version,'rows',count(*),'first',min(as_of_timestamp),'last',max(as_of_timestamp)) FROM ml_feature_snapshots GROUP BY entity_type,feature_set_version;
SELECT json_build_object('check','model_population','family',model_type,'version',version,'stage',stage,'splits',evaluation_report->'splits') FROM ml_models ORDER BY model_type,version;
SELECT json_build_object('check','relations','table',c.relname,'kind',c.relkind,'partitioned',c.relispartition) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='public' AND c.relkind IN ('v','m','p');
SELECT json_build_object('check','migration','version',version_num) FROM alembic_version;
COMMIT;
```

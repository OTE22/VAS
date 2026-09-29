# Data mart gap analysis — Stage 1

**Binding scope:** read existing database data only. No new tables, SQL views, materialized views, columns, indexes, grants, extensions, ETL writes or analytical row inserts. “Logical dataset” below means an application SELECT/CTE or in-memory transformation, not a database object. No implementation has begun.

## Required grains

| Required grain | Existing? | Existing source | Missing fields / semantic gaps | Recommendation; new DB object? |
|---|---|---|---|---|
| A. Detection: object in one frame | Partial | VAS `detections` + `faces`; transient VMS predictions | No complete frame stream, stored frame dimensions, generic object class/confidence, model/tracker session; face matching is selected | Reuse as **selected recognition observations**, keyed by face ID with detection linkage. Do not call it a complete detection census. None. |
| B. Track position: object at time | Missing as retained grain | VMS transient boxes; optional unused appearance track column | All 359 appearance tracks NULL; no per-frame positions/dimensions/scoped track ID | Unavailable under present data constraint. Do not invent tracks by sorting sightings or write a new position table. None. |
| C. Track summary | Missing | Identity appearances have start/end columns | All observed ends NULL; no measured track lifespan, distance, velocity, stop time | Offer observed sighting-session summaries only, with explicit session-gap rule; do not label their span dwell/track duration. None. |
| D. Camera interval (default 10s) | Derivable, not stored | Detections, appearances, alerts, assessments | No true person/vehicle counts, occupancy, entrances/exits, active tracks or speed | Read-only `camera_observation_window`: saved event count and distinct observed identities, separately aggregated source counts. None. |
| E. Pair interaction / encounter window | Partial temporal proxy | Identity appearances; existing pair logic | Point events rather than continuous overlap; no shared-zone/spatial distance/walking interval | Read-only canonical **temporal co-occurrence** dataset retaining both appearance IDs and window policy. Never name tolerance overlap physical interaction duration. None. |
| F. Relationship edge per period | Current edge exists; historical period grain missing | Mutable `identity_relationships`; requested-range appearance calculations | No cache period bounds/encounter provenance; snapshot feature suffixes are not historical cache guarantees | Reuse cache for current summary only; derive requested-period edges from bounded temporal co-occurrences. None. |
| G. Analytical/security event | Exists across several tables | Appearances, assessments/signals, watchlist alerts, live triggers | Heterogeneous meanings and IDs; most pattern detections only returned in APIs; no retained zone events | Read-only union/projection keyed by `(source_type,source_id)`; distinguish rule score, ML score and measured event. None. |
| Behavior trajectory window | Missing | Multi-day person feature snapshots exist | 5–30s x/y sequence, speeds, stops, zone trajectory absent | Retain existing behavioral-history model; report trajectory-window capability unavailable. None. |
| Review candidate/outcome | Partial generic coverage, existing workflow usable | Assessments, labels, predictions; enrollment/merge/alerts | No universal candidate registry; no live labels or ML predictions; no common priority semantics | Project existing reviewable items and separately join actual labels. Do not create candidates or label rows in read-only mart. None. |
| Numeric experiment input | Exists | Saved `ml_datasets`/Parquet, model evaluation/training config | Fine-grained position predictors unavailable; no held-out rows in current fitted candidates | Reuse existing explorer/notebook and targets supported by recorded snapshots. Do not register new datasets implicitly. None. |
| Next camera sequence | Derivable | Ordered identity appearances; existing Markov predictor | Sparse/sampled visits, ambiguous simultaneous sightings, limited sessions; no ground-truth residence | Read-only observed camera-transition sequences with history/coverage warnings. Not next-zone or next-coordinate prediction. None. |
| Next position / future trajectory / next zone | Missing | No durable positions or real zone assignments | Full input and future target sequence absent | Unavailable; no model/predictor or fabricated target. None. |

## Dimensions and proposed fact names

The names in the original request are evaluated as conceptual mappings, **not approved migrations**.

| Candidate name | Reuse decision |
|---|---|
| `dim_camera` | Existing VAS `pipelines` + aliases; authorized fields only. No copied camera table. |
| `dim_identity` | Existing `identities` + merge/audit lineage. Current vs historical status must be distinguished. |
| `dim_model` | Existing `ml_models`, risk model versions and VMS registry metadata; namespace model families rather than merging incompatible keys. |
| `dim_zone` | No real observed/calibrated zone dimension. Unavailable; camera labels/circles do not substitute. |
| `dim_object_class` | Generic historical classes absent. Keep recognition observation kind explicit, do not infer vehicles. |
| `dim_event_type` | Application projection discriminators from existing sources, not a new table. |
| `fact_detection` | Existing joined recognition observations, with pixel-box/dimension limitations. |
| `fact_track_position`, `fact_track` | Not satisfiable with current database; no implementation proposed. |
| `fact_camera_window` | SELECT aggregation only, renamed to observation window to avoid occupancy claims. |
| `fact_pair_interaction` | SELECT-derived temporal matches with exact source IDs and canonical order; explicitly a proxy. |
| `fact_relationship_edge` | Existing current cache or period-bound derived query; do not silently mix both definitions. |
| `fact_behavior_assessment` | Existing assessments/signals and separately joined predictions. |
| `fact_event` | Read-only heterogeneous source projection. |
| `fact_review_candidate` | Existing assessment/review records projected for inspection, not newly persisted candidates. |

## Field-level capability decisions

**Available or calculable:** camera/identity/detection identifiers, sighting time, observed camera sequence, source reference, existing crop link, raw pixel ROI center, raw ROI width/height, exact selected-observation counts, canonical identity pair, co-occurrence time gap, period-bound co-occurrence count, model/version, existing rule/ML scores and actual human decisions.

**Only with explicit qualifications:**

- Raw pixel ROI center is `((x1+x2)/2,(y1+y2)/2)` in the stored ROI's coordinate space. It is not a new tracked body position or a valid cross-image trajectory.
- Time between sightings is a gap, not dwell or walking time. NULL end time remains unknown for duration metrics.
- Camera GPS distance measures camera separation, not person proximity or observed travel distance.
- Current identity type cannot automatically label a historical observation as unknown/known; reuse audited point-in-time reconstruction where recorded history suffices.
- A zero saved-event count means no retained observations in that interval, not an empty camera scene. Unknown coverage must remain visible.
- A bounded pair scan is a bounded sample. Truncation/caps and tolerance settings must accompany output.

**Unavailable:** normalized positions (missing original dimensions), velocity/acceleration/heading, physical distance, stops/dwell, walking together, zone residence/transitions, distance to exits, vehicle counts, active track count, occupancy, next coordinate and next zone.

## Risks found

- Selected-frame/quality/cooldown sampling creates selection bias; recognition success further filters the population.
- Tracker ID/session information is not persisted by the VAS sighting writer.
- All current appearances carry `camera_reported`, but VMS sets application-read time; that label is not proof of native camera timing.
- Cache rows were last calculated on September 21, while appearances extend through September 28. Refreshing the cache is a database mutation and is outside this task.
- One duplicate identity/camera/timestamp group exists; it requires source-event examination, not automatic deletion.
- Three cached identity appearance counters differ from their source rows; use aggregate queries for analysis, not automatic repairs.
- Both camera timezones are NULL; runtime default is UTC. Do not silently relabel to local time.
- Identity NULLs and missing crop paths exist and can be consistent with deletion/retention policy. Nullable identity does not mean a reviewed “unknown person” label.
- Current fitted model candidates have no validation/test rows; refined mart queries alone cannot establish accuracy.

See [data-quality results](data_mart_data_quality.md) and [proposed read-only architecture](data_mart_design.md).

# Behavioral Anomaly ML — Current State, Shadow Contract, Evidence Path

_Last updated: 2026-08-22. Applies to `behavior_anomaly_model` (IsolationForest / MAD baseline),
feature set `secintel-features-v2`, dataset definitions `behavior_anomaly_person@v2/v3`._

## The contract in one block

```
Dataset:                 VALID_FOR_EXPERIMENTATION
Feature set:             ACTIVE            (secintel-features-v2)
Model:                   SHADOW_APPROVED   (when a v2-compatible model is in shadow)
Engineering readiness:   PASS              (per model; recorded at training time)
Scientific validity:     INSUFFICIENT_EVIDENCE
Signal mapping:          REQUIRES_VALIDATION  (SIGNAL_MAPPING_UNVALIDATED)
ML decision authority:   DISABLED
Rules:                   AUTHORITATIVE     (risk-engine-v1 computes every score)
Fallback:                RULES
Evidence collection:     ACTIVE            (while a compatible model is in shadow)
```

`GET /api/ml/overview` → `system.ml_contract` reports these values live; the ML-Ops page shows them
at the top. Three different questions are kept apart on purpose:

| question | answered by | today |
|---|---|---|
| Does the pipeline work mechanically? | **Engineering gate** (schema, checksums, reload, split integrity, non-degenerate scores, seed stability, inference contract, code revision) | PASS for v5 |
| Does the model mean anything behaviourally? | **Scientific gate** (history span, repeat observations/entity, feature availability and its stability across the temporal split, train→test score shift, reviewed-outcome coverage, mapping status) | INSUFFICIENT_EVIDENCE |
| May ML change an operational decision? | **Decision authority** (ML mode + validated, scope-matched signal mapping) | DISABLED |

A model can — and today does — pass the first while failing the second. Passing the first only permits
SHADOW.

## Why scientific validity is insufficient (measured on dev, 2026-08-22)

* History span ≈ **34 days**; median **3 appearances per identity**; 17 % of dataset rows dropped for
  entity group integrity (an entity never straddles a split boundary).
* History-dependent features are mostly unavailable: `baseline_hour_deviation_last` 96 %,
  `new_pipeline_flag_7d` 89 %, `hour_sin/cos/std_30d` 70 %, most 30-day ratios 30 %. The model trained
  on **6 usable features** (gate minimum 5) — it largely learns observation frequency and cold-start
  status, not behaviour.
* Temporal shift: train p90 ≈ 0.48 vs test p90 ≈ 0.93; 19 % of the untouched test period lands in
  `highly_unusual`. Part of that is population maturity (features becoming available in the newer
  period), which the evaluation now reports separately from score drift
  (`evaluation_report.temporal_shift`, `feature_availability_by_split`).
* Evidence: ~11 reviewed manual labels, ~10 of 248 predictions with a reviewed outcome (4 %).

The quality report of every dataset now carries `population`, `feature_availability_by_split` and
`maturity` (`pipeline_technically_usable: true`, `behavioral_population_maturity: INSUFFICIENT_EVIDENCE`
with the facts).

## What SHADOW means

```
observation ─► features v2 ─► RULES ──────────────► operational result (score, severity, alerts)
                      └──────► ML v5 (shadow) ─► observation persisted (score, band, threshold, lineage)
                                                 NO effect on the operational result
ML failure ─► failure observation persisted (reason) ─► rules continue, application continues
```

Predictions are generated, stored (`ml_predictions`: model, threshold set, snapshot, feature set,
score, band, fallback reason, as-of, rule severity alongside in `ml_shadow_comparisons`), monitored
(drift, metrics), compared and reviewed — and do not affect decisions. Every assessment persists its own
provenance (`requested_mode`, `executed_mode`, `anomaly_signal_source`, `signal_mapping_version`,
`fallback_reason`); history is never relabelled when the configuration changes.

Bands (`normal / elevated / unusual / highly_unusual`) are **relative anomaly bands** from training
quantiles (p90 / p97 / p99). They are a display convention for shadow observation — not validated
operational severity, and not assumed to be four distinct risk groups.

## Verified safeguards (tests)

* Rules result byte-identical across RULES / SHADOW / ML-without-mapping / HYBRID
  (`tests/test_decision_router.py`, `tests/test_ml_decision_modes.py`).
* ML mode with a validated, scope-matched policy changes only the behavioural-anomaly input; every
  other signal, cap and the score semantics are unchanged; the rules engine still computes the score.
* Any ML failure (crash, timeout, no model, malformed prediction, missing features, tampered artifact,
  feature-set mismatch) → HTTP 200, rules result, stable `fallback_reason`, configured mode untouched,
  recovery on the next request.
* A mapping validated for another model / feature set / threshold set is never reused.
* No `score_delta` / `score_diff` between rules and ML anywhere.

## What is required before ML authority

1. History and repeat observations: enough history span and appearances per entity for the
   history-dependent features to be available for most rows (reported per feature, per split).
2. Temporal stability: score and availability shift reported and understood, not confounded.
3. Reviewed outcomes: enough REVIEWED manual outcomes **per band**, recorded blind to the ML band and
   with selection metadata when reviews are stratified (`ml_labels.selection`).
4. Demonstrated relationship: per-band positive rates with Wilson intervals, a Cochran–Armitage trend
   across bands, Spearman score↔outcome, and — when both outcomes exist in sufficient number — PR-AUC,
   ROC-AUC, precision@top-k and lift (`GET /api/ml/shadow/evidence`, `python -m backend.ml.pipeline
   shadow-evidence`). Evidence may support fewer than four effective groups.
5. A validated mapping policy: `risk_model_versions` profile `ml_anomaly_signal_map`, status `active`,
   `calibration_status = validated`, non-empty `calibration_data`, payload
   `{kind: band_points, band_points: {...} ≤ anomaly_cap, scope: {model_id, feature_set_version,
   threshold_version}}`. Any change of model, feature set or threshold set requires re-validation.
6. Explicit operational approval: `ML_DECISION_MODE = ml` by an administrator.

Minimums for (1)–(3) are **not hard-coded**. `ML_SCIENTIFIC_MIN_HISTORY_DAYS`,
`ML_SCIENTIFIC_MIN_MEDIAN_APPEARANCES`, `ML_EVIDENCE_MIN_REVIEWED_TOTAL`,
`ML_EVIDENCE_MIN_REVIEWED_PER_BAND` default to 0 (= not configured); until an administrator sets them
from reviewed policy, the gates report `NOT_CONFIGURED` / `INSUFFICIENT_EVIDENCE` with the statistics.

## Evidence-grade data — the one definition

`backend/ml/evidence_grade.py` is the only definition, used by the shadow evidence report, the scientific
gate / readiness computation and the supervised label gate (`label_stats`). A label counts as evidence
when **all** hold:

```
label_kind    = manual
review_status = reviewed          (an authorized review confirmed it — never the creator's own act)
status        = active
label         in {positive, negative}
source        not seed-* / synthetic-* / synth-*   (demo seed and synthetic corpus are excluded)
```

Everything else is a distinct population and is **reported, never mixed in**: `unreviewed`, `weak`,
`synthetic_or_seed`, `disputed`, `retracted`, `unknown_outcome`. Evidence-grade labels are further split by
how they were obtained: `blind_reviewed`, `revealed_reviewed`, `self_reviewed` (creator == reviewer — still
evidence-grade, but stated). A weak label can never be confirmed (`WEAK_LABEL_NOT_REVIEWABLE`); re-posting a
different value for the same subject/source/day is refused (`LABEL_CONFLICT` — correct through supersede).
When seed/synthetic labels exist the system state shows `NON_EVIDENCE_LABELS_PRESENT`; they are excluded,
not deleted.

## The operational outcome workflow (blind review)

```
analyst resolves the assessment on the Security Intelligence threat card
   ("Confirmed threat" / "Not a threat", optional notes)
        │   POST /api/security/assessments/{id}/resolve {outcome, ml_observation_revealed}
        ▼
manual outcome label, source=assessment_resolution, anchored to the assessment   → UNREVIEWED
        │   links to the assessment's shadow prediction (assessment id ∪ same-subject UTC day)
        ▼
authorized review in ML-Ops (confirm)                                            → REVIEWED
        ▼
eligible as scientific evidence (evidence report, readiness criteria)
```

The threat card hides the ML observation by default and the threat-history rows show no band until it is
revealed; whether it was revealed before the outcome was recorded is stored on the label
(`selection.ml_observation_revealed`, `selection.entry_point`). Outcomes entered on the ML-Ops page (bands
visible) are always recorded as revealed. Reports show `recorded_blind` / `recorded_revealed` and the
population split, so blind and revealed evidence are never silently mixed. `selection.method`
(`natural | stratified_by_band | top_scores | random | manual`) records deliberate per-band sampling; any
method other than `natural` switches the report's sampling caveat.

## Decision authority gate — one implementation

`decision_service.mode_gate_report()` is the only gate evaluation; `PUT /api/ml/config/mode` and
`PUT /api/settings/ML_DECISION_MODE` both call it, return the same 409 `MODE_GATED` body (every gate with its
state — shadow model, engineering readiness, active threshold set, scientific validity, signal mapping —
plus the unmet reasons) and audit the refusal (`mode_change_rejected`, counted in
`ml_decision_mode_rejections_total`). ML and HYBRID require engineering PASS, an active threshold set,
scientific validity `SUFFICIENT_EVIDENCE` and a validated, exactly-scoped mapping; HYBRID is additionally
release-gated. Scientific validity shown on the page is computed **live** (reviewed evidence + mapping status
change over time; the training-time snapshot does not) and the recorded status the gate reads is refreshed
when it changes; `POST /api/ml/models/{id}/readiness` recomputes both gates in full (no retraining).

## Retention and immutability

Shadow predictions that carry an outcome label, are anchored to an assessment or are named by an
assessment's `ml_prediction_id` are never aged out (`threat_assessments` are never swept either); feature
snapshots referenced by a kept prediction stay. `ML_PREDICTION_RETENTION_DAYS` applies only to unlinked
predictions (e.g. failure rows without an assessment). Retention reports `ml_predictions_retained_as_evidence`
and `ml_snapshots_retained_as_provenance` on every run. Historical duplicate comparison rows are never
deleted: the insert guard prevents new ones, reports use the earliest comparison per prediction and state
`duplicate_comparisons` as a data-quality fact. Note that `ml_predictions.person_id` / `assessment_id` FKs
are `SET NULL` on identity/assessment deletion — the prediction row survives with its `subject_id`.

## Lineage chain and retention (what survives what)

| object | retention | deletion mechanism | FK behaviour | reproducibility after deletion |
|---|---|---|---|---|
| training request | durable | none (`ml_audit_log` is never swept) | — | `training_requested` (API, actor + request_id), `training_started` / `training_finished` (trainer, both entry points, actor user id, params, outcome/error) |
| dataset | durable | `archive` releases the Parquet bytes only (refused while a model references it); row + manifest stay | `ml_models.dataset_id` SET NULL (never exercised: rows are not deleted) | logical checksum + Parquet sha256 on the row and in the manifest; `training_config.dataset_checksum/parquet_sha256` on the model |
| feature-set version | durable | definitions are migration-seeded, boot-verified | — | stamped on dataset, model, prediction |
| training job (`background_task_history`) | **30 days** (`TASK_HISTORY_RETENTION_DAYS`) | retention sweep | none (job id is a string on the model) | every answer lives elsewhere — see audit rows above and the model row (`created_by`, `created_at`, `training_job_id`, `hyperparameters`, `seed`, `code_version`) |
| model | durable | none; stage transitions only (RESTRICT from predictions/comparisons) | — | `artifact_hash`, `evaluation_report`, `quality_gates`, `training_config`, `shadow_approval{approved_by, approved_at, reason, artifact_checksum}` + `model_shadow` audit row |
| artifact file | durable (`ML_ARTIFACT_DIR`, persistent volume required in prod) | none in the application | — | re-validated by hash on every load; a missing file is reported (`ARTIFACT_FILES_MISSING`) never silently replaced |
| shadow prediction / comparison | durable when linked (outcome, assessment) — see Retention above | age sweep of UNLINKED rows only | model/threshold RESTRICT; assessment/person SET NULL | immutable; comparison 1:1 per prediction |
| assessment | durable | none | person SET NULL | provenance columns + `ml_prediction_id` |
| reviewed label | durable | none (supersede keeps history) | person SET NULL | creator/reviewer usernames **and user ids**, selection metadata, review timestamps |
| evidence report | computed | — | — | recomputable from the rows above at any time |

After `background_task_history` expires the following are still answerable from `ml_models` + `ml_datasets`
+ `ml_audit_log` (proven by `test_training_lineage_survives_without_task_history`): who requested, when,
which dataset and hashes, code version, feature set, hyperparameters/seed, artifact and checksum, the
evaluation, and who approved into SHADOW when.

### Identity deletion: audit reproducibility vs raw recomputability

Deleting an identity cascades its raw `identity_appearances` (by design — deletion must be real) and SET
NULLs `person_id` on predictions, labels and assessments; `ml_feature_snapshots`, `ml_predictions`,
`ml_shadow_comparisons`, `ml_labels` and Parquet datasets are untouched. Consequences, stated exactly:

* **Audit reproducibility — kept.** Every persisted score, band, snapshot feature vector, comparison and
  outcome stays exactly as recorded; a dataset reproduces byte-for-byte from its Parquet + hashes.
* **Raw-data recomputability — lost for that subject.** Its features cannot be recomputed from source
  events, and re-extracting a dataset definition over the same window would yield a different row set.
  The evidence report states this per model (`source_retention.predictions_with_deleted_subject`). No raw
  identity data is duplicated anywhere to work around deletion.

### Storage growth (measured 2026-08-23, dev)

Per operational assessment under SHADOW: one prediction (≈0.7 KB tuple, ≈1.6 KB with indexes), one
comparison (≈0.2 KB tuple, ≈0.6 KB), one event snapshot (≈0.9 KB tuple, ≈1.4 KB), one assessment
(≈1.1 KB) ≈ **4.7 KB per assessment**; labels are human-rate and negligible (≈0.5 KB each).

| assessments / day | per year | 3 years | 5 years |
|---|---|---|---|
| 100 | 0.17 GB | 0.5 GB | 0.9 GB |
| 1 000 | 1.7 GB | 5.1 GB | 8.6 GB |
| 10 000 | 17 GB | 51 GB | 86 GB |

Recommendation: **retain indefinitely** at the current rate (evidence-linked rows must outlive any
scientific validation), make growth observable (`ml_evidence_table_bytes{table}`, `ml_evidence_table_rows{table}`
on `/metrics`), and plan **monthly partitioning** of `ml_predictions` / `ml_shadow_comparisons` /
`ml_feature_snapshots` before they exceed tens of millions of rows. Bounded retention already applies to
unlinked failure rows only. No retention period is chosen here.

### Metrics contract

ML state gauges (`ml_decision_authority`, `ml_signal_mapping_validated`, `ml_active_shadow_model_info`,
`ml_review_coverage_ratio`, `ml_collector_watermark_age_seconds`, `ml_evidence_table_*`,
`ml_state_refreshed_timestamp_seconds`) are refreshed by every state-changing operation (mode change / pause,
registry transition incl. threshold activation, label review, collection end) and by a throttled
scrape-time refresh (a few cheap queries, at most every 30 s per process). Opening the ML-Ops page is not
required for correctness.

Face counters changed shape (2026-08-23) — the person's display name is no longer a Prometheus label:

```
OLD: face_recognition_faces_detected_total{name="<display name>"}
     face_recognition_faces_skipped_total{name="..."}
     face_recognition_batch_duplicates_total{name="..."}
NEW: face_recognition_faces_detected_total
     face_recognition_faces_skipped_total
     face_recognition_batch_duplicates_total
```

Repository-controlled consumers (`monitoring/grafana/dashboards/face-detector-overview.json` uses
`sum(rate(face_recognition_faces_detected_total[5m]))`, `monitoring/alerts/face-detector.yml`) never used the
label. External dashboards that grouped `by (name)` must drop the grouping; per-person counts live in the
database behind authorization.

### Separation of duties (policy scaffold)

Labels record creator and reviewer as usernames **and user ids** (`created_by_user_id`,
`reviewed_by_user_id`, revision `a3c8e5f1b7d2`). `ML_EVIDENCE_REQUIRE_INDEPENDENT_REVIEW` (default **off**)
makes `confirm` refuse a label by its own creator (`SELF_REVIEW_REFUSED`); while off, self-reviewed labels
remain evidence-grade and are reported as the `self_reviewed` population. Turning it on is a policy
decision, not made here.

## Signal-mapping policy — how a validated row comes to exist

No endpoint, CLI or seed creates a `risk_model_versions` row with profile `ml_anomaly_signal_map` (the
only writers are test fixtures). Creating one is a deliberate, out-of-band administrative act: an
administrator inserts the row with `status=active`, `calibration_status=validated`, non-empty
`calibration_data` citing the shadow evidence report it was derived from, and the exact
`scope{model_id, feature_set_version, threshold_version}`. The lookup requires **all three** scope values
to be known and equal (no wildcard; a model without an active threshold set gets no policy), examines every
active row (a newer invalid row never masks a valid one) and logs why each rejected row was ignored. The
first time the system sees a new policy version it writes `ml_audit(mapping_observed)` so even an
out-of-band row leaves an audit trail.

## Reserved model types

`coappearance_anomaly_model`, `social_graph_anomaly_model` and `threat_ranking_model` are reserved
interfaces: no dataset definition, features, trainer, artifact or inference path exists. The API refuses to
train them (`422 MODEL_TYPE_NOT_IMPLEMENTED`), the trainer refuses them too (defense in depth), and the
ML-Ops page renders them from the capability contract (`GET /api/ml/overview` → `model_types`) as
*Reserved / Future — not trainable* with the Train action disabled.

## Retraining

Scheduled retraining is gated (`SCHEDULED_RETRAINING_GATED`); training is a manual, reviewable action.
The bottleneck is data maturity, not model search: do not retrain on an unchanged dataset. When
`history_span_days` exceeds ~90, build a dataset with definition `behavior_anomaly_person@v3`
(trailing 90 days, same feature set) and compare candidate vs incumbent on identical rows
(`evaluation_report.incumbent_comparison`, `pipeline evaluate`).

## Operations

* `python -m backend.ml.pipeline collect --full-rebuild` after a feature-set bump.
* `python -m backend.ml.pipeline build-dataset --definition behavior_anomaly_person --definition-version v3`
* `python -m backend.ml.pipeline train --dataset-id <uuid>`; approve for SHADOW from ML-Ops.
* `python -m backend.ml.pipeline shadow-evidence --days 90`
* Metrics: `ml_shadow_predictions_total{band}`, `ml_shadow_prediction_failures_total{reason}`,
  `ml_decision_fallback_total{reason}`, `ml_feature_schema_mismatch_total`,
  `ml_reviewed_shadow_outcomes_total{outcome}`, `ml_decision_authority`, `ml_signal_mapping_validated`,
  `ml_active_shadow_model_info{model_version,feature_set_version,algorithm}`.

## Synthetic one-year corpus (pipeline verification only)

`scripts/generate_synthetic_year.py` writes a deterministic year of camera appearances for persona-driven
identities (regular daytime, night shift, weekend visitor, occasional, new arrival, churned), plants
anomalies in ~8 % of them inside the last 30 days (off-hours burst, new camera, frequency spike, location
hop) and records a ground-truth file. With `--build-dataset --train` the REAL collector, builder and
trainer run on it, so the resulting Parquet/manifest/hashes are the system's own, and a synthetic sanity
check compares planted vs. not-planted scores on the test split. It refuses production and refuses the
development database unless `--allow-development` is given; `--remove` deletes exactly what it wrote.

A synthetic corpus proves that the pipeline works — it is not evidence about real behaviour. The
scientific gate, the evidence report and the signal mapping stay INSUFFICIENT_EVIDENCE /
REQUIRES_VALIDATION on synthetic data by design.

Reproducibility: the corpus is a pure function of `(--days, --identities, --seed, --end)`; `--end`
defaults to today 00:00 UTC and is recorded in `synthetic_year_ground_truth.json` (`reproduce`).

```
python scripts/generate_synthetic_year.py                                   # dry run (counts only)
python scripts/generate_synthetic_year.py --apply --yes-i-understand        --build-dataset --train --export-parquet logs/audit/synthetic        # isolated stack only
python scripts/generate_synthetic_year.py --remove --yes-i-understand       # deletes exactly what it wrote
```

### What the one-year corpus exposed (fixed 2026-08-22)

* **Collector backlog cap.** One collection pass scanned at most 20 000 appearance rows and then
  moved the watermark, so a backlog larger than one pass (a bulk import, a long outage, or a year
  generated at once) was never fully collected. The collector now scans the candidate window in
  keyset batches of 20 000 (`(created_at, id)` cursor) until it is exhausted, commits the watermark per
  batch, and reports `candidate_rows`, `batches` and `rows_scanned` honestly.
* **Split strategy must be declared.** `temporal_group` (the default, unchanged) assigns an entity wholly
  to its earliest period and drops its later rows; with a year of regular entities that leaves almost
  nothing for val/test (synthetic year: train 11 211 / val 3 / test 0, 8 786 dropped). A dataset may now
  be built with `split_strategy = temporal` (definition field, `--split-strategy`, or the build form):
  same time boundaries, no row dropped, entities may recur, and the overlap is **measured and recorded**
  (`split_config.entity_overlap`). Scores on such a test split describe the later behaviour of known
  entities — the operational case — not generalisation to unseen entities; the engineering gate records
  the declared strategy and the scientific gate states `ENTITY_OVERLAP_ACROSS_SPLITS` as a fact.

Verified on the isolated stack (2026-08-22, 300 identities, 365 days, seed 20260822): collector
86 755 rows in 5 batches; dataset 86 755 rows, split 52 603 / 16 492 / 17 660 with 0 dropped,
history span 364.6 days, median 406 appearances per entity, 95.7 % of test rows from entities also in
train; model v1 engineering PASS, scientific INSUFFICIENT_EVIDENCE (`NOT_CONFIGURED`,
`SIGNAL_MAPPING_UNVALIDATED`); train p90 0.62 vs test p90 0.66 — the large train→test shift seen on the
34-day dev corpus (0.48 vs 0.93) is population maturity, not behaviour. Planted-vs-not-planted test
medians 0.222 vs 0.205 — descriptive only; the synthetic corpus says nothing about real behaviour.

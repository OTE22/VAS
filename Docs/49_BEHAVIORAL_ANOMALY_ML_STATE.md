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

## Reviewer bias

The Security Intelligence threat card keeps the ML observation **hidden behind a click** so an analyst
recording an outcome is not primed by the band. The ML-Ops page (administrators) shows bands openly.
Outcomes recorded from the ML-Ops predictions table after looking at the band are not evidence-grade;
use `selection.method` to mark stratified reviews.

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

# ML lifecycle audit — 29 September 2026

Scope: VAS ML Ops and security-intelligence model families. This is not an audit of VMS video decoding capacity, face-recognition model training, or all 34 background services. The existing four-stage workflow remains **Prepare & train → Test & review → Connect service → Monitor**.

## What actually happens

| Stage | Implementation | Method and limits |
|---|---|---|
| Source extraction | `backend/ml/collector.py`, `feature_builders.py`, `relational_feature_service.py` | Incremental database collection, cutoffs, deduplication, source counts, versioned feature snapshots. Person history is point-in-time. Pair/graph features describe the observed relationship cache; they do not reconstruct an arbitrary historical graph. |
| Feature engineering | `feature_builders.py`, `feature_store.py`, `relational_feature_service.py` | Counts, ratios, recency, activity windows, hour sine/cosine, numeric identity status, pair and graph statistics; weighted degree uses `log1p`. Unavailable values carry reasons instead of invented zeros. |
| Targets/labels | `dataset_builder.py`, `tabular.prepare_rows` | Anomaly models are unsupervised. Supervised ranking joins active reviewed manual positive/negative labels to the closest snapshot at or before label event time and maps labels to 1/0. Training applies label-readiness/source governance. Regression requires an explicit finite numeric target excluded from predictors. |
| Dataset preparation | `dataset_builder.py`, `data_validator.py`, `dataset_steps.py` | Immutable Parquet, logical/file checksums, source IDs, validation and split metadata. Rejects invalid numeric values, leakage-class violations and other quality failures. Rebuilds create a new dataset version. |
| Split | `dataset_steps.split_rows` | Default temporal/group split with 60/20/20 fractions of elapsed time, not guaranteed row proportions. An entity belongs to its earliest window; its later-window rows are excluded. Optional temporal-only split allows repeated entities and records overlap. No random shuffle is silently substituted. |
| Missing values and selection | `dataset_builder._select_supervised_features`, `trainer._assemble_matrix`, `scoring.preprocess_feature_vector` | New supervised builds remove columns above 50% missing **in training only**. Matrix selection requires at least 70% training coverage. Training medians fill absent/null values; true zero remains zero. Feature order and medians travel with the artifact and are shared by serving. |
| Encoding/scaling | Existing feature builders and numeric scoring contract | Engineered predictors are numeric. No generic one-hot encoder, category label encoder, standard scaler, PCA or SMOTE exists. Logistic regression currently fits raw feature units. Scaling is a candidate improvement for that algorithm, requiring a versioned train/serve artifact and comparative evaluation, not an unconditional change for all models. |
| Hyperparameter tuning | `tabular.fit_xgboost`, `run_spec.py` | Optional XGBoost-only Optuna TPE search; validation log loss for classification, validation RMSE for regression; bounded search, trial/time limits and optional median pruning. Test data is not passed into optimization. Best parameters refit on training data. Other algorithms use requested bounded parameters, without automated search. |
| Training | `trainer.py`, `tabular.py` | MAD baseline / Isolation Forest for anomaly families; logistic regression, random forest, gradient boosting or optional XGBoost for ranking; XGBoost regression for an explicit numeric target. Fits produce candidates, with recorded seed, parameters, dependencies and artifact identity. |
| Evaluation | `trainer.py`, `evaluation_visuals.py` | Anomaly: score quantiles, band counts, seed stability and distribution diagnostics; no outcome accuracy without reviewed labels. Ranking: ROC-AUC and average precision when both classes exist; descriptive confusion matrix at 0.5. Regression: MAE, RMSE, R² and training-mean baseline. Read each split's sample count before interpreting metrics. |
| Registry/API use | `registry_service.py`, `workflow_policy.py`, `service_deployment.py`, `model_scoring_service.py` | Artifact/dependency/schema checks, evidence gates, explicit selection and audit. Anomaly models remain shadow observations; ranking supports analyst ordering; regression stays offline. Model outputs are not calibrated threat probabilities. |
| Monitoring | `drift_service.py`, `monitoring_contract.py`, `worker.py` | Data PSI/KS/JS and behavior-score PSI plus fallback/shadow health. Data baseline is the preceding processing-time window, **not the training dataset**. Report-only monitoring never changes models. See gaps below. |

The safe order is: inspect source data → compute deterministic point-in-time features → define labels and splits → fit feature selection/imputation/any learned transforms on training only → tune on validation → evaluate the untouched test set → review/connect → monitor and compare a new candidate when needed.

## Defects corrected in this working tree

1. **Held-out feature-selection leakage.** The early supervised sparse-column filter previously used all labeled rows. It now learns from the declared training split, applies a fixed feature set to all rows, counts explicit nulls as missing, and records the fitting population and exclusions in dataset quality and manifest metadata. Existing datasets are not rewritten.
2. **Explicit-null median failure.** Matrix assembly counted present null keys as observed values and could pass `None` into NumPy median. It now omits nulls from coverage/median estimation, imputes with valid training medians and explicitly rejects invalid numeric training values. All-null columns are not fitted.
3. **Cross-model drift contamination.** Feature drift previously always read person snapshots without a schema filter. It now selects the recorded model schema and its entity type/predictors. Prediction and shadow-comparison queries now filter by exact model ID, with bounded time windows and deterministic ordering.
4. **Incomplete drift evidence looked normal.** Reports retain missing features, expose missing/invalid counts, check each feature's sample sufficiency and disclose query truncation. Fallback scores are excluded from score-distribution comparisons. Unsupported model-family score telemetry is explicit. Added `assessment_status` and monitoring contract version 2; the existing severity field remains compatible. A warning in one feature is retained even if another feature lacks evidence.
5. **Notebook methods were difficult to locate.** The exporter now explains encoding, missingness, tuning objectives, task-specific metrics and maintenance limits. Its source browser includes sparse feature selection, Optuna tuning, ranking metrics, drift and service connection. The first executable cell still displays the checksum-verified actual dataset.

## Running deployment evidence checked read-only

- Behavior v3: Isolation Forest; 286 training rows, **0 validation, 0 test**.
- Coappearance/pair v1: Isolation Forest; 744 training rows, **0 validation, 0 test**.
- Both candidates are `validated`; neither is selected for live/shadow service use.
- No graph, ranking or regression model is registered in the inspected database.
- `OPTUNA_ENABLED=false`; the two current anomaly fits did not run Optuna tuning.
- `ML_DRIFT_MONITORING_ENABLED=false`, minimum samples 200; no saved drift reports exist.
- `XGBOOST_ENABLED=true`, `MLFLOW_ENABLED=true`, `SHAP_ENABLED=false`. These flags alone do not establish integration health or a particular run's use.

These observations cannot establish held-out accuracy, scientific readiness, automatic model maintenance, or 30-camera throughput. The audited fixes have not been deployed into the API/worker by this task.

## Remaining work, in order

1. **Evaluation population and labels.** Collect representative later periods and sufficient independent identities/pairs for the chosen group holdout. Keep group exclusions visible. Temporal-only evaluation can answer a different question—future observations of known identities—and must be deliberately selected and reported. Gather reviewed outcomes before claiming anomaly detection precision/recall or ranking usefulness.
2. **Algorithm-specific preprocessing and comparison.** Evaluate a versioned scaling pipeline for logistic regression against the current model. Choose categorical encoding only if categorical predictors are introduced. Add missingness indicators only after testing whether availability predicts the intended target rather than operational artifacts. Do not apply SMOTE across time/entity boundaries or invent positive security labels.
3. **Tuning and decision metrics.** Enable bounded tuning only when validation evidence is sufficient. Consider rolling temporal validation to assess stability beyond one split. Add precision/recall/F1 and precision-at-review-budget where appropriate, using a threshold/budget chosen on validation, not test. Assess calibration separately if probability output is ever required.
4. **Complete monitoring coverage.** Persist version-bound scores and schema/feature availability for pair, graph and ranking consumers, with explicit retention and outcome linkage. The current behavior prediction table is not a substitute for those services. Add a training-baseline comparison alongside the current recent-window comparison. Separate event-time/current-state populations to diagnose backfill effects.
5. **Fix the scheduling contract before enabling it.** The automatic drift guard currently requires production-stage ML/hybrid predictions; supported anomaly deployment is shadow-only. Simply turning on the flag does not make shadow monitoring operational. Adapt scheduling to supported selected-model evidence, with per-model sample/window checks and tests, before activation.
6. **Performance drift and retraining.** Build recurring outcome-based metrics on predictions joined to delayed reviewed labels, including cohort, sample size and label-lag reporting. Score/data drift alone does not prove concept drift or accuracy loss. Scheduled retraining is currently scaffolded and enabling it is refused (`SCHEDULED_RETRAINING_GATED`). Any future trigger should create an evaluated candidate, retain rollback information, and use the existing review/connect step.

## API boundaries

- Training: `POST /api/ml/training-jobs` (durable job; inspect its status).
- Candidate/service selection: `POST /api/ml/services/{model_type}/deploy`.
- Current selection/consumption: `GET /api/ml/services/status`.
- Pair/graph scoring: `POST /api/ml/score/relational`.
- Analyst ranking: `POST /api/ml/rank/threat-review`.
- Manual drift job: `POST /api/ml/drift/run`; reports: `GET /api/ml/drift/reports`.
- These routes retain their authentication/authorization/CSRF and model-governance checks. An HTTP 202 is job acceptance, not proof that training/evaluation/deployment completed. Application behavior inference is integrated through the decision/shadow service path.

## Validation

- Disposable PostgreSQL, no production test writes: **152 isolated backend tests passed**, including 15 new lifecycle regressions covering model/schema/time isolation, missing features, caps, nonfinite/fallback scores, training-only selection and null imputation.
- **14 notebook tests passed**, executing exported code offline with network access blocked.
- Two refreshed real-data notebooks execute integrity, validation, split and matrix reproduction checks. Saved worker/model evidence remains historical; no live training is performed by these notebook cells. Execution results are retained in their published audit bundle.
- Existing dataset/model artifacts, selected modes and running services are preserved. No schema migration or retraining is required for these source fixes. New supervised builds record the corrected feature-selection policy; older sparse selections require a new dataset version if reevaluated.

## References

- [scikit-learn: leakage and consistent preprocessing](https://scikit-learn.org/stable/common_pitfalls.html): learned transformations and feature selection must fit training data; reuse those transformations at serving.
- [Google Cloud: MLOps delivery and training pipelines](https://docs.cloud.google.com/architecture/mlops-continuous-delivery-and-automation-pipelines-in-machine-learning): versioning, validation, deployment, monitoring and controlled retraining are lifecycle responsibilities.

This architecture follows established ML lifecycle principles. That does not certify this deployment or imply that every large company uses the same tools or gates.

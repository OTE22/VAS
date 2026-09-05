# Guided ML Operations workflow

The existing `/admin/ml-ops` console now leads with seven clickable stages, dataset/model/run selectors, training progress, and a compact five-question run summary. Existing preparation, evaluation, governance, shadow approval, cancellation, monitoring, label review, archive, and audit controls remain available below. The workflow uses their existing requests and confirmation controls; it does not introduce another training or deployment implementation.

## Evidence and limits

- Dataset inspection reads the registered Parquet artifact. `/api/ml/datasets/{id}/explorer` supports `split`, `label`, `q`, `page`, and `page_size` (1–100). It scans at most 100,000 rows in batches off the event loop. Counts describe the scanned prefix before display filters; partial scans are explicitly labelled. Duplicate keys match the build validator's `(entity_id, as_of)` definition. Invalid-row diagnostics describe their own checks and do not replace build validation.
- `/api/ml/datasets/{id}/validation-report` downloads the original saved build report and missing-value report with dataset version and checksum. This works even when the artifact is unavailable. Both endpoints require the existing administrator `ML_MANAGE` capability and use `no-store`. No new database migration or artifact rewrite is needed.
- Stage history, duration, process memory and cumulative CPU time are recorded at stage boundaries for new runs. Resource snapshots are not continuous monitoring; GPU telemetry is unavailable. Remaining time is a labelled estimate from reported progress. Historical runs show missing telemetry explicitly.
- New models record resolved feature names and training medians in the existing training configuration. The preprocessor still uses the same shared training/inference implementation.
- New supervised evaluations save a diagnostic confusion matrix at score threshold 0.5 against reviewed test labels. This threshold never controls production decisions or readiness. Tree models expose impurity importance; logistic models expose signed coefficients in original units with a comparability warning. Boosting models expose their recorded training loss by iteration, including an accessible table. Unsupported or historical diagnostics are not fabricated.
- Baseline comparison displays the existing evaluation against the incumbent shadow model on identical rows. It is descriptive and does not authorize promotion. Deployment readiness continues to use the existing engineering/scientific evidence and approval gates.
- Feature collection actually precedes dataset creation in this architecture. The Feature Engineering stage explains the feature contract and matrix construction during training; it does not suggest that inspecting a historical model recomputes features.

## Verification without user data

```text
docker exec face_recognition_api python -m pytest tests/test_ml_workflow_isolated.py tests/test_ml_ops_page.py tests/test_ml_job_architecture.py -k "not serves_for_an_admin and not requires_authentication" -q
node scripts/dev/mlops_workflow_e2e.js
```

The isolated backend suite uses a fake database, temporary Parquet/model files, and a small synthetic fit/evaluation/artifact round trip. The real-page browser suite intercepts every request, including training and shadow approval, and covers filtering, downloads, failed jobs, recovery, permission denial, stale responses, mobile layout and keyboard use. It never submits a live job or approval. `PW_CORE` and `CHROME` can override the browser paths; `WORKFLOW_SCREENSHOT` optionally saves a visual review.

The API process must load the changed routes and the worker must load the changed trainer through the normal deployment/restart procedure. No live service restart, live training, database migration, or historical-data modification is part of these tests.


## Platform integrations and operating contract

The implementation extends the existing durable worker, artifact registry, feature
store, Parquet snapshots, data validator, administrator capability and audit log.
DVC and Pandera were not added: snapshot storage already has immutable versions,
canonical-row checksums, file hashes, lineage and schema/quality gates. Adding a
second authority would duplicate those contracts.

| Location | Controls and evidence |
| --- | --- |
| Admin Settings | MLflow URL/experiment, availability flags, thread/trial/time/sample limits, capability registry and existing settings audit |
| Project / pipeline | Immutable named versions of model contract, algorithm, target, predictor list, recorded dataset split strategy and task-compatible metrics |
| Training run | Dataset version, seed, parameter overrides, optional Optuna search/trials/timeout/pruning, optional SHAP, clean Git requirement |
| Results / registration | Held-out metrics, baseline, curves, importance, SHAP global and individual contributions, exports, reproducibility, tracking state, comparisons, governed promotion |

MLflow, lineage, validation and reproducibility default on. Existing explicit
administrator overrides remain effective. XGBoost is available by default when its
package imports. Optuna and SHAP are disabled administratively by default and also
require a separate opt-in for each run. The capability endpoint distinguishes
`Available`, `Disabled`, `Unavailable` and `Misconfigured`; it reports actionable
recovery instructions without exposing credentials. Remote MLflow URLs must use
HTTPS without embedded credentials, query strings or fragments. Configure MLflow
SDK service credentials through the deployment's existing secret mechanism.

With an empty tracking URL, MLflow uses a SQLite database and managed artifacts
under the existing persistent ML artifact root. This suits the current single
training / synchronization worker. Use an HTTPS MLflow service with durable SQL
and artifact storage when moving to a distributed deployment; back up tracking
and model storage together. No separate MLflow process or DVC service is required
for the default deployment.

`ml_pipeline_versions` is append-only through the API. Training copies the selected
version into the durable job payload and model configuration. `ml_tracking_runs`
retains the experiment identity, reproducibility manifest, synchronization state,
registered model/version and last retryable error. Training starts a real MLflow
run; completion and lifecycle changes queue final synchronization through the
existing worker. Uploads therefore do not depend on the short shutdown window of
a completed training process. A failed upload retains the local artifact and
returns an explicit failed synchronization state. Retry from Results after
restoring integration access. Job/model tags make retries reuse the existing run
and registry version. The local governed registry remains deployment authority.

The portable MLflow pyfunc includes the exact shared scoring/preprocessing module
used locally. Exported evidence includes parameters, nested measured metrics,
evaluation JSON, reproducibility JSON, the platform artifact and optional SHAP
assets. Registry aliases mirror local versions and governed stages. MLflow uses
aliases for this workflow; the API reference describes alias operations in the
[MLflow client documentation](https://www.mlflow.org/docs/latest/api_reference/python_api/mlflow.client.html).

### Algorithms, tuning and explanations

Existing anomaly and reviewed-label ranking contracts keep their serving gates.
XGBoost classification is selectable for reviewed binary ranking. Numeric
regression is a separate **offline** contract: select an existing unsupervised
feature snapshot, declare a finite numeric feature target and save a pipeline.
The target is excluded before fitting imputation and predictors. Unknown features,
invalid targets, failed schema/quality checks and changed snapshot hashes refuse
the run. Regression reports MAE, RMSE and R? against a training-mean baseline;
ranking reports ROC AUC and average precision with a training-prevalence reference.
A regression target is never interpreted as a security threat label.

Optuna is currently bounded to XGBoost classifier/regressor, whose boosting
iterations support real pruning. Supported search dimensions are int, float or
categorical over the documented bounded XGBoost parameters (`n_estimators`,
`max_depth`, `learning_rate`, `subsample`, `colsample_bytree`, `reg_lambda`). Trials
use seeded TPE, one trial at a time, a run timeout and optional median pruning.
The objective is validation log loss or RMSE. Test data is never used to tune or
fit. The winning parameters are fitted again on training rows; that final fit is
outside the tuning timeout. No completed trial is an actionable run failure.

SHAP supports the supervised tree and linear algorithms, including XGBoost
regression. It uses a bounded, explicitly identified holdout sample. Native
XGBoost contributions are wrapped as SHAP explanations; other supported estimators
use SHAP explainers. Results include mean absolute contributions, individual
signed contributions and base values, plus checksummed JSON and PNG exports.
The API only serves those registered exports. SHAP values describe model behavior,
not causation or calibrated threat probabilities. See the
[SHAP explainer reference](https://shap.readthedocs.io/en/latest/generated/shap.TreeExplainer.html).

### Automatic GPU selection and CPU fallback

CPU Linux images install `xgboost-cpu`; GPU images install `xgboost`. They are
mutually exclusive because both provide the same Python module. Both development
and production GPU overrides now give the ML worker the GPU image and device
reservation. Use the CPU stack on hosts without GPU access; a Compose GPU
reservation itself requires an NVIDIA-capable host/runtime.

For each XGBoost run, the worker checks CUDA build support and performs a tiny
real CUDA fit. It checks the resulting booster device rather than trusting a
package import or an ONNX provider list. A usable CUDA device runs training,
Optuna trials and final fitting on GPU. An unavailable device uses CPU. A GPU
runtime/OOM failure during a fit retries that fit once on CPU with the same data,
seed and parameters; subsequent fits use CPU. Data/configuration errors still
fail. The actual device and fallback reason are saved in results, the fitted
model, tuning evidence and reproducibility manifest. Cross-device runs can differ
numerically even with the same seed. This follows the versioned
[XGBoost GPU interface](https://xgboost.readthedocs.io/en/release_3.2.0/gpu/).

### Reproducibility and promotion

Every completed model records dataset ID/version/logical checksum/file hash,
storage reference, split configuration, pipeline configuration, resolved
parameters, seed, Git commit/dirty state, training-source checksum, direct and
installed environment dependency versions, Python/platform and execution device.
A clean Git requirement refuses unknown or dirty code. Ordinary runs accurately
record missing Git identity; a checksum identifies code but does not substitute
for retaining the source checkout or deployment image. No environment variables
or credential values are exported.

Offline ranking/regression artifacts can move from `validated` to `approved`
only with passed quality gates, an administrator's review reason and a matching
artifact checksum. The artifact is verified again before approval. This is
approval for offline use, not live deployment. Existing anomaly shadow approval,
rollback and the database anomaly stage cap remain in force. Production drift is
deferred; the canonical `MLPrediction` evidence readiness contract in
`monitoring_contract.py` provides the extension point. Existing historical and
manual shadow drift reports remain available.

### Additive migration, API and verification

Apply Alembic revision `fcc3d4e5f6a7` through the existing migration job when
deploying the updated images. It adds the two integration tables and does not
rewrite dataset/model rows. Production Compose defaults pin this same revision
for migration, API and worker readiness. Downgrade drops integration evidence and must not be
used as a routine rollback. Back up and retain those tables instead. This work
has not applied the migration to the user's application schema or restarted the
live services.

New authenticated routes:

| Method | Route | Purpose |
| --- | --- | --- |
| GET | `/api/ml/capabilities` | Availability, recovery instructions, limits |
| GET / POST | `/api/ml/pipelines` | List / create immutable pipeline versions |
| GET | `/api/ml/experiments` | Tracking evidence and failures |
| GET | `/api/ml/comparisons?model_ids=?&model_ids=?` | Compare 2?5 registered models; flag incompatible data/tasks |
| POST | `/api/ml/experiments/{job_id}/retry` | Queue synchronization with an audit reason |
| POST | `/api/ml/models/{id}/promote` | Checksum-bound offline approval |
| GET | `/api/ml/models/{id}/explanations?sample=0` | Recorded global/individual contributions |
| GET | `/api/ml/models/{id}/explanations/download/{filename}` | Allowlisted checksummed export |

`POST /api/ml/training-jobs` additionally accepts `pipeline_id` and `run_options`.
All existing fields remain valid. New mutations retain administrator RBAC, CSRF,
rate limiting and audit logging. UI requests use the console's existing shared
API/auth/error helpers.

```text
python -m pytest tests/test_ml_platform_integrations.py tests/test_ml_workflow_isolated.py tests/test_ml_ops_page.py tests/test_ml_job_architecture.py tests/test_gpu_runtime.py -q
ML_PLATFORM_VERIFY_POSTGRES=1 python -m pytest tests/test_ml_platform_persisted.py -q
node scripts/dev/mlops_workflow_e2e.js
```

The optional-library tests run actual XGBoost, Optuna, SHAP and MLflow, including
portable prediction parity, pruned trials, registry deduplication and alias
removal. The persisted test creates a randomly named PostgreSQL schema, sets
`search_path` to that schema **only**, applies the additive migration there, and
runs synthetic snapshot selection ? pipeline version ? queued training ?
evaluation ? reproducibility ? MLflow registration ? offline approval. It also
checks CSRF/permission refusal, artifact checksum binding, exports, audit events,
outage recovery and unchanged input bytes. Only the generated test schema is
removed. Authentication/limiter setup and live metrics/cache updates are isolated;
the dataset, validator, trainer, artifact registry and MLflow are real.
GPU selection/error paths use controlled test doubles, with actual CPU fallback
fits. A real CUDA fit must additionally be verified on a GPU-enabled deployment;
no NVIDIA GPU was exposed in this verification environment.


## Guidance for first-time readers

The page uses layered explanations: essential instructions are visible beside
the selected stage and controls; the first-run walkthrough, glossary, metric
reference and status guide expand on demand. Every pipeline stage answers four
questions: what happens here, what to do, when to continue and how to recover.
The explorer explains that display filters do not alter training data. Training
help distinguishes task, target, predictors, seed and optional tuning/explanation
choices. Results distinguish data validation, model quality, MLflow copying and
permission to use a model, and identify the actual baseline type. Existing
controls and status APIs remain the authority for actions and readiness.

The browser workflow check passed after these explanations were added, including
mobile layout, keyboard navigation, error recovery and the existing training and
approval controls. It intercepts all API requests and does not submit live jobs.

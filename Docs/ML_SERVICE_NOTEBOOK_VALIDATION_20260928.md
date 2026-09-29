# Executed ML service validation, 2026-09-28

Application release tested: `1b23cf762bcc1ed1f9de6f0e22437d20bc9d35b2`.
Sealed build ID: `5e9d3305c4cd10f19742279e9b3b07e16f6f8979d42e43e4b124ef5b0229d9ed`.

Open `/notebooks/lab/tree/Validation-passed-20260928/START-HERE.ipynb` on the application host. The original failed run remains in `Validation-baseline-20260928`.

Five real worker workflows passed; all five exported notebooks passed 22 code cells each (110 total). All five were also rerun successfully from the published workspace bundle. Outputs, HTML exports, per-cell timings/error checks/output hashes, exact model evidence, worker results, runner scripts and isolated Parquet datasets are included. Select Kernel → Restart Kernel and Run All Cells, or Shift+Enter per cell. Keep notebooks in their `notebooks` directory so `../artifacts` and `../<family>-result.json` resolve correctly.

| Family | Train | Validation | Test | Service verification |
|---|---:|---:|---:|---|
| Behavior anomaly | 337 | 248 | 320 | Shadow selection, real identity inference, stop/archive |
| Coappearance anomaly | 120 | 120 | 240 | Shadow selection, real pair scoring/consumption record, stop/archive |
| Social graph anomaly | 40 | 40 | 80 | Shadow selection, real graph scoring/consumption record, stop/archive |
| Threat ranking | 40 | 40 | 40 | Approved selection, rank three identities, stop/archive |
| Numeric regression | 337 | 248 | 320 | XGBoost fit, artifact reload/scoring, expected OFFLINE_ONLY deployment refusal |

## Execution boundary

`tests/isolated_ml/validate_service_runs.py` runs actual production collection, dataset building, training, evaluation, artifact reload, selection, consumers and stopping in the baked worker image against a disposable PostgreSQL database. Its fixture contains 120 synthetic people, 960 appearances, 360 relationships and simulated reviewed labels. It refuses other database/artifact targets. It does not mount production configuration or data. Disposable schema uses ORM metadata, not an upgrade migration.

`tests/isolated_ml/execute_service_notebooks.py` executes the resulting exports in the deployed Jupyter image, using nbclient and real python3/ipykernel kernels. Network is disabled and test artifacts are mounted read-only. The notebook cells inspect recorded worker evidence and execute dataset checksums, frozen validation, exact split and preprocessing rechecks, then independently assert lifecycle results. Database collection, fitting and deployment are performed by the worker runner BEFORE the notebook; displayed source references are not executed. This boundary is explicit in every bundle README.

The initial timestamp fixture produced empty held-out partitions and correctly failed independent assertions. The corrected fixture uses separated cohorts and meets graph observation readiness without reducing production requirements. Approval fields in the fixture match the existing shadow governance contract. A test assertion was corrected to inspect regression destination mode and absent selection, since a valid offline candidate has `review_candidate` state.

## Defects discovered and fixed

1. `_git_commit()` ignored sealed release manifests. Real production images produced `code_version=None`, incorrectly failing anomaly engineering gates. Verified sealed identity now reaches the shared runtime/dataset resolver; unverified manifests remain rejected. Commit `56db4f7`.
2. Persisted relational snapshots omitted `entity_type`/`entity_id`, causing a real `KeyError` when pair and graph consumers built their score response. The persistence return now includes identifiers from the stored row, including deduplicated reads. Commit `1b23cf7`.

Regression suite: 122 tests passed with disposable PostgreSQL, including new identity resolution and persisted/deduplicated snapshot contract checks. Build provenance suite: 23 passed. One existing SQLAlchemy deprecation warning remains.

## Limits

This validates five ML families and their named consumer functions, not every Docker service or the entire browser application. It does not establish real camera/GPU decoding capacity, 30-camera throughput, migration safety on an existing database, production traffic handling, or scientific model accuracy. Anomaly scientific status remains INSUFFICIENT_EVIDENCE; ranking REQUIRES_CALIBRATION; regression OFFLINE_EVALUATION_ONLY. No synthetic dataset/model was placed in the production ML artifact volume or activated in the production database. The user workspace contains only independent test evidence and dataset copies.

## Deployment verification

API and ML worker were recreated from the tested release. Both running containers report verified, clean sealed provenance for `1b23cf7`. API image: `sha256:0d705f34e07d0a25138353a1850f4cb0afd1319557fc4019c92bd8d158230a11`. Worker image: `sha256:bbf58524cfdcd0054a1db5b7533c6ba2662d2a211a39cedbbce78f6fb7a8416c`.

After normal startup, TLS-verified `/health/ready` returned HTTP 200 at `2026-09-28T20:40:11Z`: database, models, cache, queue and offline policy healthy; 34 background services with no degraded or stale entries. Jupyter was not restarted. The two disposable PostgreSQL containers and their internal test network were removed after validation. Published notebooks and fixture datasets remain in the notebook workspace.

## Output-driven audit, 2026-09-29

The executed outputs were reviewed quantitatively, beyond successful cell execution. `tests/isolated_ml/audit_notebook_outputs.py` now checks model/job/dataset/release identity, retained/excluded row accounting, evaluation population and band totals, quantile ordering, registered quantitative evaluation, selected/consumed model identity, score ranges and probability semantics, threshold-to-band consistency, ranked ordering, archival identity, and recorded observational consumption. It also verifies the saved cell sequence/output hashes against the execution report, normalizing notebook multiline text serialization. Hashes are consistency checks, not signatures.

All **110 cross-stage checks passed** on the published bundle. Nine tests passed, including rejection of wrong model identity, incorrect bands, probability misrepresentation, unaccounted rows, evaluation count changes, nonfinite prediction, different dataset release and modified saved output. The executed `OUTPUT-AUDIT-executed.ipynb` contains seven code cells with saved findings and is linked from START-HERE.

Interpretation of the actual outputs:

- Ranking test ROC AUC is 0.575 (validation 0.4275). Arbitrary synthetic labels support mechanical validation, not a claim of useful ranking accuracy.
- Regression test R² is approximately 0.999999997. The synthetic fixture has a simple relationship between target and predictors; this does not establish generalization.
- Pair scoring returns 1.0 as an anomaly score, explicitly not a probability, and reports `pair_co_appearance_count_30d` unavailable. A valid score does not imply complete feature coverage.
- Behavioral inference succeeds directly, but captured usage correctly has `selected_model_used=false`: the fixture did not exercise assessment-linked prediction persistence. This remains a coverage gap, not a proven defect.
- Pair, graph and ranking usage records reference the selected model; observational output is explicitly not applied to live decisions.

No additional production defect was established by this audit, so no thresholds, accuracy gates or production behavior were changed to improve fixture results. The audit/checks are new regression tooling; no application rebuild is needed for these test-only files.

Run against a downloaded/local copy of the complete bundle:

```bash
python3 tests/isolated_ml/audit_notebook_outputs.py /path/to/Validation-passed-20260928
python3 tests/isolated_ml/test_audit_notebook_outputs.py /path/to/Validation-passed-20260928
```

The audit returns nonzero on inconsistent evidence. Review observations even when all checks pass; scientific and integration gaps must not be hidden by a passing mechanical audit.

# Synthetic graph pipeline validation — 2026-09-29

Purpose: exercise the graph model workflow after the real-data run failed with zero rows. **Every generated record in this run is synthetic. This is a functional test, not production accuracy evidence.**

## Open the results

[Jupyter index](https://face-detector.internal/notebooks/lab/tree/SYNTHETIC-graph-validation-20260929/START-HERE.ipynb)

The bundle includes:

- `SYNTHETIC-relationship-source.csv`: 360 generated relationships across 120 synthetic identities.
- `SYNTHETIC-social_graph_anomaly_model-dataset.csv`: the exact 240 extracted feature rows, with source snapshot IDs, timestamps, split assignment and six graph features. Every CSV row is marked synthetic.
- `artifacts/datasets/social_graph_anomaly_model-train-v1.parquet`: immutable dataset actually consumed by the trainer, plus its manifest.
- `notebooks/social_graph_anomaly_model-executed.ipynb`: **24 executed code cells**, all passed, with saved outputs. The first executable cell displays the actual saved training dataset.
- HTML notebook rendering, complete worker result, cell-execution report, semantic audit, source collection counts and drift-report evidence.

## Isolation and source identity

The deployed worker image ran the existing collector, dataset builder, trainer, registry and consumer functions. Its verified application revision was `c59affce10d4a2a43a561563b921ef31596a1a5c`. The updated test harness was mounted separately; production application code was not replaced.

The runner required explicit opt-in, database hostname `notebook-validation-db`, database name `notebook_validation`, and artifact root `/validation-output/artifacts`. Docker used a separate **internal** network, a disposable PostgreSQL container with temporary storage, no production mounts and no production credentials. The exported notebook ran in the existing notebook image with external networking disabled. The temporary database and network were removed afterward.

Only result files were published into Jupyter. No synthetic data, model, label, threshold or setting was added to the production database. No production service was restarted or model selected.

## Generated population and extraction

The fixture supplied 120 synthetic identities, 960 synthetic sightings and 360 canonical relationship records. No fabricated analyst labels were created for this graph-only run. Three observation cohorts let the real graph feature extraction run with historical cutoffs. The first eligible graph already had 40 nodes, 120 edges and 16.39 days of observation span; the normal floors of 25 nodes, 50 edges and 14 days were unchanged.

The three graph extractions yielded 40, 80 and 120 snapshots respectively, totaling 240. The standard `temporal_group` split retained **40 training, 40 validation and 40 test rows**. It excluded 120 later rows belonging to identities already assigned to an earlier split. This is intentional protection against identity overlap and future-period leakage.

The feature schema was the existing `social-graph-features-v1`: degree centrality, log weighted degree, PageRank, clustering coefficient, bridge ratio and mean edge weight. Features came from the existing graph calculations; they were not injected directly as precomputed training vectors.

## Stages exercised

| Stage | Result |
|---|---|
| Synthetic source generation | Passed; reproducible identity IDs/cohort structure |
| Graph readiness and feature extraction | Passed with normal thresholds |
| Dataset preparation and immutable Parquet export | Passed; 240 rows with checksums and lineage |
| Validation and time/entity splitting | Passed; nonempty train/validation/test populations |
| Missing-value handling and feature ordering | Passed; notebook reproduced the saved train-only preprocessing contract |
| Isolation Forest fitting | Passed; seed 42, 200 estimators, `max_samples=auto`, `contamination=auto` |
| Evaluation | Passed; split score distributions, bands, seed stability and temporal shift recorded |
| Artifact persistence and reload | Passed; hash/dependencies/features checked and scores reproduced |
| Registry and service connection | Passed in the disposable database only |
| Actual graph service consumer | Passed; consumed the selected model and returned a finite observational score |
| Stop/disconnect | Passed; test selection removed and model archived in the disposable registry |
| Feature drift reporting | Passed; normal minimum of 200 samples retained, 250 generated monitoring samples per window |
| Persistent graph-score drift | Correctly reported **unavailable for this model family** |
| Notebook execution | 24/24 code cells passed |
| Independent cross-stage semantic audit | 23/23 checks passed |

Hyperparameter search was not run: the existing Optuna integration supports XGBoost, not this Isolation Forest path. The recorded settings above are the actual fitted hyperparameters; no tuning result is invented.

## Monitoring finding

The feature drift test deliberately doubled two node-weight features in the current synthetic monitoring window. The actual data-drift service detected a **critical** distribution shift. A separate offline score PSI was calculated from the fitted model's synthetic predictions.

The graph consumer currently does not support persistent prediction-score telemetry in the production drift contract. Its drift endpoint returned `PERSISTED_SCORE_TELEMETRY_UNAVAILABLE_FOR_MODEL_FAMILY`. The notebook asserts this explicit refusal; it does not interpret an insufficient-data report as healthy monitoring. No telemetry records were fabricated in production to bypass this boundary.

Real model-quality drift, precision, recall and threat accuracy were not validated because this unsupervised synthetic population provides no genuine operational outcomes. The model's scientific gate remains **INSUFFICIENT_EVIDENCE**, while its engineering gate passed.

## Execution versus notebook replay

The real training/registry/consumer lifecycle executed once in the isolated worker. The exported notebook then executed the existing evidence and offline rechecks cell by cell: loading and hashing the exact dataset, rerunning validation and splitting, reproducing matrices, checking recorded training/evaluation and checking actual consumer/disconnect results. It does not silently retrain or redeploy when opened.

The notebook uses `../artifacts` and companion JSON files within its own bundle. Keep that folder structure when downloading or moving it. Synthetic artifacts must remain separate from production training/approval inputs.

## Reuse

The existing opt-in scripts now accept `VAS_VALIDATION_FAMILIES=social_graph_anomaly_model` to select this service. The default remains all supported families. Run `tests/isolated_ml/validate_service_runs.py` only with its explicitly guarded disposable database and output mount, then execute `tests/isolated_ml/execute_service_notebooks.py` with the same family selection in the isolated notebook runner.

This test establishes that a suitable dataset passes the implemented graph workflow. It does not repair the production history shortfall, prove real-world accuracy, add unsupported monitoring, or establish camera/GPU capacity.

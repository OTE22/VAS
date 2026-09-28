# Debug the ML workflow in Jupyter

## Open the right notebook

In `/admin/ml-ops`, choose a service and select **Open step-by-step notebook**. The application creates a new notebook in the existing authenticated Jupyter workspace and opens it directly. **Download .ipynb** remains available for an offline copy.

For an exact run, use its notebook action in the job details. A selected model follows its own training run and dataset automatically. The selected model/run/dataset is written into the first cell so you can confirm the context before debugging. The starter notebook also works before a model or dataset exists; missing stages are explicit.

## Work from the first cell to the last

Use **Shift+Enter** to execute one cell at a time. Inspect the variables and outputs before proceeding. Each numbered section explains its input, processing, output and next use:

1. Pipeline map and recorded worker timeline.
2. Data collection: appearance events, relationship observations, cutoffs, counts and lineage samples.
3. Feature meaning: computation, windows, values and missingness reasons.
4. Dataset extraction: population filters, sampling, reviewed labels and validation.
5. Train/validation/test split and exclusions.
6. Training recipe and preprocessing contract.
7. Evaluation metrics and engineering/scientific readiness.
8. Code, dependency, dataset and artifact identity.
9. Selected service model and actual usage at export time.
10. Production source browser: use `show_source("person_context")`, `show_source("dataset")`, or the printed feature computation key.
11. Offline rechecks of the immutable dataset: file/hash verification, coverage, frozen validation and declared split.
12. Production preprocessing recheck: selected columns, training medians, imputed values and fixed-order matrices.

The source browser shows export-time code locations and hashes. It does not automatically prove which code produced a historical run. Rechecks stop on changed helpers, altered files, missing frozen definitions or a mismatched preprocessing contract.

For breakpoints, execute the helper definition cell first, enable JupyterLab's debugger and set a breakpoint inside the helper before rerunning its calling cell. Kernel variables expose the verified rows, split assignments, feature names, medians and matrices.

## Recorded history and offline execution

The notebook shows saved extraction/training/evaluation evidence and the corresponding production source. It reruns pure validation, splitting and preprocessing on a read-only saved dataset. Managed data collection, model fitting, model approval and deployment continue through ML Ops. Notebook code does not need production database credentials or load pickled models.

New managed training jobs retain preparation, dataset and training diagnostic sections independently. Older overwritten stages cannot be reconstructed and appear as missing evidence. The notebook is a snapshot: export a fresh copy after another job or service request to inspect new results.

A saved dataset is required for offline data checks. Training medians and feature order require a recorded model contract. Reviewed labels and readiness gates remain prerequisites for their respective production actions.

## Deployment and verification

The API assembles a bounded, read-only evidence export. The notebook gateway checks the existing administrator session and same-origin request, accepts only identifiers, imports only a server-generated notebook and writes a new output-free file. It never starts a kernel automatically. The Jupyter artifact mount remains read-only.

NumPy is pinned to 2.4.6, matching the deployed ML worker, for the production preprocessing functions. Existing JupyterLab/ipykernel/Parquet support remains in place.

Validation uses synthetic artifacts, isolated API fixtures, a disposable Jupyter instance and a disposable PostgreSQL database. Live verification is limited to service health, read-only evidence and creating the requested starter notebook. Checks completed before deployment:

- 116 isolated backend tests passed with disposable PostgreSQL; this includes 33 notebook evidence/phase-persistence tests.
- 71 static interface/architecture checks passed; two live-login fixture tests were excluded.
- 22 notebook gateway tests passed, plus a disposable real Jupyter import/reopen test proving no kernel starts automatically.
- 12 generated-notebook tests passed in the actual rebuilt Jupyter image, including real temporary Parquet and complete cell execution for all five model families, changed-artifact refusal and preprocessing comparisons.
- Firefox checks passed for selected-record context, direct import, popup fallback, error handling, stage status and 390px layout; zero JavaScript errors. The existing guided training/deployment browser fixture still passes.

Final release image identities and live results follow after verification.

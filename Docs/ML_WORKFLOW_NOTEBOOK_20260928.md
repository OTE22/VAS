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

Deployment verified:

- Release commit: `a6f37c1fe3888b2dd4fa35e9d1300b0ef2747bc4`.
- API image: `sha256:8efa4bbc79b7f60aa040417afe8bec759d7bf4ba5e6cf2e55d4e78644f560fc6`.
- ML worker image: `sha256:19d918fbea278ccd4721326f1434f4922d0151e322f6e01f6245d755e5611cbb`.
- Jupyter and gateway image: `sha256:36c8aa91470b53e6ad70d39d81355d2e78a3f4f3a9e40b7830bfea5aac915c9b`.
- API/worker clean image provenance verified both before deployment and in the running containers. Built-image backend suite passed all 116 checks.
- User confirmed saved work and authorized restarting the active Jupyter session before its runtime was recreated.
- HTTPS readiness from VMS returned 200 with database, models, cache, queue and all 34 background services healthy. Notebook/export routes correctly returned 401 without an admin session.
- Live read-only evidence export succeeded. Created `VAS-ML-Workflow-a6f37c1.ipynb` in the persistent workspace, without overwriting any existing file. The actual Jupyter Contents API verified it is available with 26 cells.
- Starter notebook schema validated; all 11 available code cells executed offline in the rebuilt notebook image. It has no immutable dataset/model yet, so data-dependent rechecks explain their missing inputs.
- Running notebook dependency check: NumPy 2.4.6, PyArrow 23.0.1, ipykernel 6.31.0.
- Rollback image tags retain the previous API/worker (`before-workflow-notebook-20260928`) and notebook (`before-workflow-debug-20260928`). No production training, extraction or model activation was triggered by these verification checks.

Open the starter at `/notebooks/lab/tree/VAS-ML-Workflow-a6f37c1.ipynb` while signed in as an administrator. Use a new ML Ops export after subsequent runs to inspect their exact evidence.

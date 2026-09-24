# ML Operations: step-by-step user guide and demo

Page: `/admin/ml-ops`  
Audience: VAS administrators, including first-time users  
Guide checked against the application on: **24 September 2026**

## 1. What this page does

ML Operations lets you prepare data, train a model, inspect its results, and manage its permitted use. A successful training job does **not** automatically deploy a model.

The normal order is:

```text
Check services
    → Collect features
    → Build a dataset
    → Inspect data and validation
    → Train one model
    → Review results
    → Consider the permitted approval
    → Monitor and audit
```

For your first run, follow sections 3–9. Section 12 provides a complete example with suggested form values. Section 13 explains recovery from common problems.

At the last readiness review, the worker was healthy but there were **no feature snapshots, datasets, models, or reviewed labels**. This is a dated observation, not a live status indicator. Always refresh the page before working.

## 2. Find your way around the page

| Page area | What to use it for |
| --- | --- |
| Summary cards | Check decision authority, worker health, active jobs, and last synchronization. |
| Recommended next step | Find the next action based on current evidence. Clicking it navigates to a control; it does not submit the operation. |
| Overview | Follow jobs and check service health and decision mode. |
| Prepare & train | Collect features, inspect readiness, build datasets, review labels, and start training. |
| Review models | Inspect model records, evaluation, and eligible approval actions. |
| Monitor | Inspect shadow evidence, predictions, fallbacks, and available drift reports. |
| Audit | Find administrator actions, API errors, and request IDs. |
| Inspect a dataset or model | Expand the evidence browser for samples, validation, training stages, evaluation, and lineage. |
| Step-by-step instructions and troubleshooting | Expand the instructions for the current workspace. |
| Guide on a card | Get help specific to that tool. |
| Open Notebook | Open JupyterLab with your existing VAS admin session. |

You can switch workspaces without starting a job. Buttons such as **Compute features**, **Build dataset**, and **Start training** submit real work.

### A few useful words

| Term | Meaning |
| --- | --- |
| Observation | Existing operational evidence, such as a recorded appearance. |
| Feature | A measurement calculated from that evidence and supplied to a model. |
| Snapshot | Feature values saved for an entity at a particular time. |
| Dataset version | A frozen collection of records used by an experiment. |
| Label | A reviewed outcome used to teach or evaluate a supervised model. |
| Training / validation / test | Records used to fit a model, select settings, and evaluate the final result, respectively. |
| Pipeline configuration | A reusable recipe describing a model and its settings. |
| Model artifact | The saved trained model file. |
| Lineage | The evidence connecting a model to its dataset, settings, and results. |
| Shadow | Observation alongside rules; the model does not gain live decision authority. |
| Gate | A requirement that must be satisfied before an action is permitted. |

### Follow the arrows with Guide me

Click **Guide me** in the page header to start a ten-step interactive walkthrough. A highlighted outline and an arrow identify the control being explained. Use **Next →** and **← Back** to move between steps; the tour opens the relevant workspace automatically. **Go to this control** closes the tour and focuses that control so you can continue manually. **Close tour** or **Escape** exits. You can restart at any time from the header.

The tour previews the workflow; it does not collect data, start training, or approve a model. Finish the actual jobs and inspect their results before continuing your operational workflow.

## 3. Step 1 — Check that services are ready

1. Sign into VAS as an administrator.
2. Open **Admin → ML Operations**.
3. Click **Refresh console**.
4. Check the worker and synchronization indicators.
5. Open **Overview → Work in progress** and inspect any existing jobs.

**Expected result:** current status loads, the worker reports healthy, and you can see whether work is already running.

Do not treat **Checking**, **Unavailable**, or a failed refresh as confirmed readiness. If a matching job is scheduled or running, follow that job rather than submitting another copy.

The decision mode and the worker state answer different questions: rules can remain active while an ML worker is unavailable.

## 4. Step 2 — Collect features

1. Click **Prepare features** when it is the recommended action, or open **Prepare & train**.
2. Find **Is data ready?** and read its feature counts.
3. Click **Compute features** once.
4. Return to **Overview → Work in progress**.
5. Follow the feature collection job until it completes or fails.
6. Refresh status and check the feature counts again.

**Expected result:** feature snapshots are available for building a dataset.

Feature collection calculates measurements from existing operational data. It does not create camera observations, invent identities, or generate demonstration records. If there is no usable source data, resolve that prerequisite first. A completed job with zero usable output is not enough to proceed.

The current page submits the default collection operation; it does not provide a historical full-rebuild toggle. Ask the maintainer to investigate if a feature-version change requires a full rebuild.

## 5. Step 3 — Build a dataset you can inspect

The Build and train card places training controls before the reusable dataset form. For this walkthrough, **build the reusable dataset first**, even though its form appears lower down.

1. Open **Prepare & train → Build and train**.
2. Scroll to **Or build a reusable dataset**.
3. Fill in the fields below.
4. Click **Build dataset** once.
5. Follow the dataset job in **Overview → Work in progress**.
6. When it finishes, return to Prepare & train and inspect the saved dataset record.

| Field | How to choose |
| --- | --- |
| Dataset name | Use a descriptive name, such as `first-behavior-baseline`. |
| Kind | Choose **Unsupervised (features only)** for the first behavior anomaly experiment. Supervised datasets require reviewed labels. |
| Definition | Choose `behavior_anomaly_person` for person behavior, using the version offered by the page. Other model types need their compatible definitions. |
| Range start / end (local) | Choose a period containing relevant snapshots. These are browser-local times and are converted to UTC. Leave both blank to use the definition's available source range. |
| Above cap | Keep **refuse (definition default)** initially. If the source exceeds the cap, narrow the dates before deliberately choosing a sampling policy. |
| Split | Keep the definition default unless your evaluation question requires another strategy. Read the distinction below. |

### Understand the split before training

| Strategy | What it means | What to check |
| --- | --- | --- |
| `temporal_group` | Assigns an entity to its earliest time bucket and drops its later-period rows, so entities do not recur across splits. | Validation/test may become very small if most entities first appeared in the training period. |
| `temporal` | Keeps records in their time bucket; the same entity can appear in more than one split. | Results describe later behavior of potentially known entities, not performance on entirely unseen entities. Inspect recorded overlap. |

Do not switch strategies merely to make an error disappear. Choose the strategy that matches the question you intend to evaluate.

**Expected result:** a saved dataset with its version, row count, checksums, and validation evidence. Some extraction or validation errors prevent a dataset from being registered; use the job diagnostics in that case.

## 6. Step 4 — Inspect the data and validation

1. Expand **Inspect a dataset or model**.
2. Select your **Dataset version**.
3. Open the **Dataset** stage.
4. Review its source, row count, feature version, and sample records.
5. Use sample filters if needed. They change the preview, not the training dataset.
6. Open **Validation** and inspect the saved checks.
7. Use **Download validation report** if you need a copy.
8. Open **Preprocessing** and **Feature Engineering** to understand how inputs are handled. Some evidence is recorded only after training.

Check that the data represents the intended period and population, contains usable features, and has appropriate training/validation/test populations. Distinguish a full-artifact statistic from a preview or capped scan.

**Expected result:** you can explain what the model will receive and why the dataset is suitable for your experiment.

If checks fail, correct the cause and build a new version. Changing a preview filter does not repair a saved dataset. A passed technical check is not proof that the data will produce a useful model.

## 7. Step 5 — Configure and start the first training run

1. Open **Prepare & train → Build and train**.
2. Keep **Saved configuration → Use existing model defaults** for the first run.
3. Select a compatible model and algorithm.
4. Select the dataset you just inspected under **Immutable dataset version**.
5. Use seed `42` and leave **Hyperparameters (JSON)** blank.
6. Leave optional tuning, explanations, and the clean-checkout requirement unchecked for this initial baseline.
7. Click **Start training** once.
8. Follow the job in Overview.

For a first person-behavior experiment:

| Setting | Value |
| --- | --- |
| Model type | **Behavior anomaly (person)** |
| Algorithm | **Isolation Forest** |
| Immutable dataset version | Your inspected `behavior_anomaly_person` dataset |
| Seed | `42` |
| Hyperparameters | Blank: use defaults |
| Optuna / SHAP | Off |

Explicitly select the saved dataset. Leaving **Build a new dataset for this run** selected requests a new build instead of reusing the one you inspected.

**Expected result:** the job finishes and creates a model record with training configuration and evaluation evidence. Inspect the actual model stage; do not assume every completed job creates an approvable candidate.

If the job is scheduled, it is waiting. If running, inspect the current stage and message. If failed, record the error and request ID, correct the cause, then submit a new run. A time estimate is approximate. Cancellation is a request; confirm the final job state before restarting.

## 8. Step 6 — Review the trained model

1. Open **Review models** and find the new model.
2. Open **Detail** to inspect its record.
3. In **Inspect a dataset or model**, select the corresponding **Experiment / model** and, if needed, **Training run**.
4. Open **Evaluation**.
5. Inspect held-out metrics, comparison evidence, and engineering/scientific gates.
6. Open **Model Registration / Deployment** to inspect lineage and permitted use.
7. Click **Run summary** for a concise explanation of the data, transformations, algorithm, results, and deployment status.

| Question | Evidence to examine |
| --- | --- |
| Was the right data used? | Dataset identity, version, definition, dates, and checksums. |
| Was the experiment reproducible? | Seed, resolved configuration, code/environment evidence, and artifact hash. |
| How did it perform? | Actual held-out measurements and available baseline comparisons. |
| Are the conclusions supported? | Sample counts, label coverage, feature limitations, and scientific gates. |
| What can I do with it? | Model purpose, stage, serving scope, and available approval action. |

An anomaly score measures unusualness. A value of `0.90` is not automatically a 90% threat probability. Missing or insufficient evidence is not a zero score or a passed check.

**Test a model safely** is a separate observational scoring form for the model types listed there: coappearance, social-graph anomaly, and threat-review ranking. It is not the person behavior model's held-out evaluation screen. Use the selected model's saved Evaluation evidence for this walkthrough.

## 9. Step 7 — Decide whether approval is appropriate

You may finish the first experiment after reviewing its results. Approval is a separate administrator decision.

For an eligible validated anomaly candidate, **Approve for SHADOW (observation only)** opens a confirmation requiring a reason. Review the evidence and intended use before confirming. For eligible ranking/regression artifacts, **Approve artifact for offline use** permits the supported offline use; it is not live deployment.

An example reason, **only when true**, is:

> Reviewed dataset lineage, held-out results, limitations, and applicable gates. Approved for observation only; rules remain authoritative.

If an action is absent or refused, read the model's stage and gate reasons. Do not treat an approval label as proof that predictions are already flowing. Return to Overview to verify decision mode, then inspect Monitor for actual evidence.

Live ML and hybrid modes have additional gates. Completing this tutorial does not satisfy them automatically.

## 10. Step 8 — Monitor and audit

1. Open **Monitor** after the relevant model is approved and the intended observation path is active.
2. Select the applicable model and time window.
3. Inspect prediction counts, fallback reasons, and available comparison evidence.
4. Inspect drift reports when the capability is available. At the last review, new production drift capability reported disabled; historical reports remained readable.
5. Open **Audit** to trace administrator actions and request IDs.

No predictions may mean no new input, the wrong filter/window, missing approval, or an inactive observation path. It does not by itself prove that training failed.

**Stop shadow (rollback)** changes lifecycle state. Read its confirmation and the affected model before using it; do not use it merely to close a page or end a notebook session.

## 11. Debug a dataset cell by cell in Jupyter

1. In Overview, expand a dataset job's **Pipeline diagnostics & notebook**. A saved dataset's detail view also offers a notebook export.
2. Read the stage timeline and failure information first.
3. Click **Download debug notebook**.
4. Click **Open Notebook** in the page header. It opens `/notebooks/lab` on the VAS site using your admin session; no separate notebook token is needed.
5. Upload the downloaded `.ipynb` with JupyterLab's upload control.
6. Open it and select its Python kernel if prompted.
7. Run cells **from top to bottom** with `Shift+Enter`.
8. Leave `ARTIFACT_ROOT` as `/artifacts` for the managed workspace.
9. Stop at the first unexpected exception or failed assertion. Read that cell's output and the preceding evidence.
10. Correct the source/configuration through the appropriate VAS workflow and build a new dataset version when necessary.

The notebook checks saved evidence, artifact integrity, validation, and split behavior. It does **not** replay database extraction or repair the registered dataset. A failure before snapshot creation may yield a diagnostics-only notebook. Older builds can lack evidence needed for some checks; the notebook explains these gaps.

Notebook files and kernels are shared between authorized admins. Give your file a distinct name, such as `first-behavior-baseline-review.ipynb`, and clear record outputs before sharing it. The mounted artifacts are read-only. If your VAS session expires, sign into VAS again and reopen the notebook.

## 12. Worked demo — First person-behavior baseline

**This is an illustrative walkthrough, not a seeded dataset or an experiment already run.** Names, counts, and outcomes below are examples. The page uses your real operational observations; do not enter fictional labels to reproduce the example.

### Demo goal

Learn how to create and review one person-behavior anomaly model. Stop after reviewing the results; approval is optional and requires its own evidence review.

### Demo starting point

Assume your installation has observations from several dates and enough distinct entities and history to produce useful feature snapshots and nonempty evaluation splits. In a new installation, collect real observations first. Clicking Compute features repeatedly cannot replace missing source data.

### Demo A — Collect

1. Refresh the console and confirm a healthy worker.
2. Click **Prepare features**, then **Compute features**.
3. Follow the collection job in Overview.
4. Suppose it completes and reports **500 snapshots**. This is an example count, not a requirement or a guarantee of readiness.

### Demo B — Build

In **Or build a reusable dataset**, enter:

| Field | Demo value |
| --- | --- |
| Dataset name | `demo-person-behavior-baseline` |
| Kind | Unsupervised (features only) |
| Definition | `behavior_anomaly_person`, using the offered version |
| Range start / end | Blank for this example; inspect the resulting coverage |
| Above cap | refuse (definition default) |
| Split | definition default (`temporal_group`) |

Click **Build dataset** and follow its job. Suppose a new version appears. Its row count can be lower than 500 because extraction and split rules may exclude records. Inspect the actual exclusion evidence rather than expecting the counts to match.

### Demo C — Inspect

1. Expand **Inspect a dataset or model** and select `demo-person-behavior-baseline`.
2. Check the Dataset and Validation stages.
3. Confirm that validation passed and that the recorded splits support training and evaluation.
4. If validation/test is empty, stop here. Review dates, entity distribution, and split policy before building another version.

### Demo D — Train

Set:

```text
Saved configuration:       Use existing model defaults
Model type:                Behavior anomaly (person)
Algorithm:                 Isolation Forest
Immutable dataset version: demo-person-behavior-baseline, the version just inspected
Seed:                      42
Hyperparameters:           leave blank
Optuna:                    unchecked
SHAP:                      unchecked
Clean Git requirement:     unchecked
```

Click **Start training** once. In Overview, follow stage messages until the job finishes. Save the job identifier so you can find the same run later.

### Demo E — Interpret the outcome

Select the resulting model and read Evaluation and Run summary.

| Illustrative outcome | Meaning | Next action |
| --- | --- | --- |
| Job completed; model record and evaluation exist | The experiment executed and saved evidence. | Review the actual stage, metrics, and gates. |
| Engineering checks pass; scientific evidence is insufficient | Technical execution succeeded, but useful performance is not established. | Gather the missing evidence; do not claim deployment readiness. |
| Dataset validation or training fails | A prerequisite or runtime step failed. | Inspect the job error; use the exported notebook for dataset evidence. |
| Eligible candidate with reviewed evidence | An approval action may be available. | Consider it separately, for the permitted scope only. |

**Demo completion:** you can identify the dataset version, training job, model, recorded configuration, evaluation evidence, and next justified action. The demo is successful as a learning exercise even if the evidence tells you to improve the data rather than approve the model.

### Optional comparison exercise

Create a second run on the **same saved dataset**, using **Median/MAD baseline** if available. Keep the seed and default parameters. Compare the two models' recorded evaluation and limitations. You now have two algorithms evaluated on the same input snapshot; do not choose a winner merely because its raw anomaly scores are larger.

## 13. Troubleshooting in order

| What you see | What to do next |
| --- | --- |
| Worker unavailable or status unknown | Refresh and inspect service health. Resolve the worker issue before submitting work. |
| Job stays scheduled | Check the worker and other active jobs. Avoid duplicate submissions. |
| Zero feature snapshots | Confirm usable source observations exist; inspect collection output and errors. |
| No dataset in the training picker | Confirm build completion, model compatibility, and file checksum. Check whether legacy verification is offered. |
| Dataset cap exceeded | Narrow the date range, or deliberately select and document a supported sampling policy. |
| Validation fails or evaluation split is empty | Inspect failed checks and split populations; correct the cause and build a new version. |
| Supervised training is gated | Review label readiness. Only qualifying active, reviewed manual outcomes count. At the last review the gate required 100 total, including 25 positive and 25 negative. Use the current displayed requirements. |
| Hyperparameters rejected | Leave overrides blank for the first run, or enter a valid JSON object containing settings accepted by that algorithm. |
| Optional tool is disabled | Check capabilities. XGBoost, Optuna, SHAP, and MLflow were disabled at the last review. Basic person-anomaly experiments do not require them. |
| Training completes but approval is unavailable | Read the model's actual stage, purpose, and gates. Completion is not approval. |
| Notebook has no snapshot to load | The build may have failed before writing an artifact. Use its diagnostics and the worker job reference. |
| Notebook reports a checksum mismatch | Investigate the artifact/version mismatch. Do not remove the assertion to force a pass. |
| Notebook asks you to sign in | Restore your VAS admin session and reopen from the page header. Use the VAS URL, not the old localhost port 8888. |
| Request fails with 401 / 403 | Sign in again for an expired session; confirm administrator access for a permission refusal. |
| Request fails with 409 / 422 | Refresh and check active jobs/lifecycle gates for 409; correct the reported fields or validation issue for 422. |
| Unexpected server error | Record the action, time, job ID, error code, and request ID; inspect Audit and provide those references to the maintainer. |

## 14. When you are ready for more advanced work

- **Supervised ranking:** create evidence-backed labels, review them, satisfy the displayed readiness gate, and build the compatible supervised dataset.
- **Regression:** use an available regression algorithm and a saved pipeline declaring an explicit numeric target. The target must be excluded from predictors.
- **Reusable recipes:** expand **Save a reusable pipeline version**, name the recipe, choose known predictors/metrics if needed, and save. Each save creates a new version.
- **Tuning and explanations:** enable supported optional capabilities through the existing settings process before using them. Compare settings using validation data; keep test data held out.

For implementation details, see [Dataset diagnostics and JupyterLab](ml-dataset-debugging.md). For the dated deployment assessment, see [ML Operations readiness review](reviews/ml-ops-readiness-2026-09-24.md).

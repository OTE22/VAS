# ML Operations

[All guides](README.md) · [Shared navigation and dialogs](shared-controls.md)

**Open:** `/admin/ml-ops` at `https://face-detector.internal`. **Access:** Administrator.
**Source review:** 2026-09-24, current working tree. Demo below is a walkthrough with expected results, not a recorded execution.

## Features

Guided ML lifecycle: readiness, optional tools, feature jobs, reviewed labels, versioned datasets, training, model review, shadow evidence, drift and audit. Available modes/algorithms come from backend capabilities.

## Buttons and controls

| Control | What clicking or changing it does |
|---|---|
| Guide me | Start the on-page guided tour; Back/Next/Finish/Close navigate or dismiss the tour without training a model. |
| Refresh console / Refresh overview / Refresh jobs / Refresh models / Refresh calls | Reload the named live data. |
| Overview / Prepare & train / Review models / Monitor / Audit; numbered lifecycle buttons | Switch workspace sections. |
| Next-step action / Go to first task / Previous stage / Next stage | Navigate the guided workflow. These navigation controls do not themselves satisfy a readiness gate. |
| Dataset / model / run selectors + Refresh evidence | Choose which artifacts the workflow summary inspects and reload their evidence. |
| Run summary | Open the selected workflow's summary. |
| What changed | Open system notes/help; Close dismisses them. |
| Mode action | Open a reason/confirmation panel and request the selected available decision mode; backend readiness gates still apply. |
| Pause ML — return to rules | Confirm a mode change returning decisions to rules. |
| Optional-tool switches | Save MLflow, XGBoost, Optuna, SHAP or scheduled drift configuration. Installed availability and worker state are separate from the switch value. |
| Refresh tool status / Advanced settings | Reload saved/effective tool state or open Settings. |
| Compute features | Queue feature computation from available observations; inspect worker/job status. |
| Saved configuration / Refresh capabilities | Select an ML pipeline version or reload capability information. |
| Save new pipeline version | Save the chosen name, target, feature and metric configuration as a new version. |
| Dataset kind / definition / sampling / split / dates + Build dataset | Create a dataset snapshot; validation can refuse insufficient or unsuitable data. |
| Dataset details / explorer / validation report | Inspect lineage, samples and validation when offered. |
| Archive dataset | Confirm archival of the chosen dataset; it is a stored lifecycle change. |
| Backfill hashes (when offered) | Request hashes for eligible legacy datasets. |
| Model type / algorithm / dataset / run options + Start training | Submit a training job; optional tuning/explanations require compatible enabled tools. |
| Training Cancel / job Cancel | Request cancellation of eligible work. Follow the job state until settled. |
| Retry experiment (when offered) | Submit another attempt of an eligible failed experiment. |
| Model Details | Inspect metrics, artifact lineage, readiness and scientific-gate evidence. |
| Recompute readiness | Re-evaluate model gates; it does not bypass failed gates. |
| Approve shadow / Reject | Open a reason/confirmation panel and save a registry decision. Approval is subject to server checks. |
| Stop shadow (rollback) | Confirm stopping/archiving the shadow model. |
| Evidence report | Open the selected model's shadow evidence and reviewed-label coverage. |
| Shadow model / days / Previous / Next | Filter and page through stored predictions. |
| Run drift check now | Submit drift analysis; insufficient qualifying samples can block useful output. |
| Evaluation model type / IDs / parameters + Run evaluation | Submit the selected evaluation job. |
| Label review filter / Previous / Next | Browse annotation records. |
| Label value / kind / selection method / event information + Create label | Save an annotation; review status and selection method affect evidence eligibility. |
| Review / supersede label (when offered) | Save a review or replacement of an existing label. |
| Retraining-policy model selector and policy form | Inspect/edit the selected model type's available retraining policy; saving changes policy, not an immediate training result. |
| Audit Previous / Next | Navigate ML change history. |
| Confirm action / Cancel / Close detail | Execute the pending registry/mode action, cancel it, or close a read-only detail respectively. |

## Demo

1. Click Guide me to walk through the workspaces without changing model state.
2. Open Overview and inspect worker health, capabilities, current mode and data readiness.
3. In an isolated test dataset with enough observations, open Prepare & train and Compute features; wait for successful completion.
4. Build a dataset, inspect validation, select it, then Start training. Record the dataset and job IDs.
5. Open Review models and inspect the candidate's lineage and gates. Do not approve a failed or unreviewed candidate.
6. If a reviewed candidate passes shadow gates, approve it with a reason and inspect Monitor evidence after qualifying predictions arrive.
7. Use Audit to verify each submitted change. A lack of data may legitimately stop the demo before training or shadow review.

## Behavior to know

Shadow evidence is not automatically a production decision change. Saved tool settings do not install missing packages. Scheduled drift changes may require an ML worker restart and sufficient production inference samples. Trust backend capabilities and explicit gate messages rather than static option text.

## Source

[frontend/admin/ml-ops.html](../frontend/admin/ml-ops.html), [frontend/js/admin-ml-ops.js](../frontend/js/admin-ml-ops.js), [frontend/js/mlops-workflow.js](../frontend/js/mlops-workflow.js), [frontend/js/mlops-platform.js](../frontend/js/mlops-platform.js), [frontend/js/mlops-tools.js](../frontend/js/mlops-tools.js), [frontend/js/mlops-tour.js](../frontend/js/mlops-tour.js). Page access: [dashboard routes](../backend/routes/dashboard.py).

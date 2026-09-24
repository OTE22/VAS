# ML similarity model

[All guides](README.md) · [Shared navigation and dialogs](shared-controls.md)

**Open:** `/admin/ml-model` at `https://face-detector.internal`. **Access:** Administrator.
**Source review:** 2026-09-24, current working tree. Demo below is a walkthrough with expected results, not a recorded execution.

## Features

Train and review the similarity model used for merge suggestions. This is separate from the anomaly/relational models in ML Operations.

## Buttons and controls

| Control | What clicking or changing it does |
|---|---|
| Refresh | Reload dataset readiness, active model, candidate registry and training jobs. |
| Train Model | Submit a training job using available review data; follow its progress and gate results. |
| Cancel | Request cooperative cancellation of the current training job. |
| Activate (model row) | Validate and activate a candidate if server gates pass; affects future merge-suggestion scoring. |
| Reject (model row) | Record rejection of the candidate. |
| Rollback (model row) | Request activation of an eligible previous model through server validation. |
| Settings | Open the identity-related settings category. |

## Demo

1. Open Refresh and inspect how many reviewed merge examples are available.
2. If readiness permits in a test dataset, click Train Model and watch the job stages.
3. Inspect the resulting candidate and metrics; training alone should not be treated as activation.
4. Leave the candidate unactivated until its validation results have been reviewed.

## Behavior to know

An insufficient-data or failed-gate result is a valid outcome. Cancelling is cooperative, so the job may finish a stage before stopping. Do not fabricate successful training when the dataset is not ready.

## Source

[frontend/admin/ml-model.html](../frontend/admin/ml-model.html), [frontend/js/admin-ml-model.js](../frontend/js/admin-ml-model.js). Page access: [dashboard routes](../backend/routes/dashboard.py).

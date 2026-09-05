# MLOps operator workflow

This guide matches **Admin → ML Operations** at `/admin/ml-ops`. It explains
the complete operator path without changing the system's safety boundary:
rules remain authoritative, training creates a candidate, and shadow output is
observational.

## Before you begin

- Sign in with an administrator account.
- Open `/admin/ml-ops` without a URL fragment. A fragment such as
  `#operations-queue` is only an old in-page anchor; the page removes it after
  selecting the matching workspace.
- Read the live connection, decision-authority, worker, active-jobs, and last-
  synchronized indicators at the top.
- Select **Refresh console** whenever displayed state may be stale.

The status labels always include text, not color alone:

| Status | Meaning | Operator response |
|---|---|---|
| Ready | Required service state is available | Continue with the stage |
| Running | Durable work is active | Follow progress in Overview |
| Needs attention | Evidence or state needs review | Read the nearby explanation before continuing |
| Blocked | A prerequisite is unavailable | Correct the stated cause first |

## 1. Check — Overview

**What it does:** confirms control-plane and worker health, queued work, system
configuration, and the current decision authority.

**Run:** select **Refresh console**, then read **Work in progress**, **System
health**, and **Live decision mode**.

**Verify:** the console is Connected, the worker is Healthy, the decision
authority is expected, and every active job shows a stage and progress.

**Recover:** sign in again after a 401. If the worker is unavailable, restore
it and refresh. Commands already accepted by the durable queue remain recorded;
production rules continue to protect live decisions.

## 2. Prepare — data and training

**What it does:** creates point-in-time features, reviewed labels, immutable
datasets, and a candidate model. It does not deploy a model.

**Run:**

1. Compute missing features and review outcome labels.
2. Build a reusable dataset or select an existing compatible dataset.
3. Select a model type and algorithm and start training.
4. Open Overview to follow the background job.

**Verify:** the job reaches Completed; readiness shows enough coverage; the
dataset exposes its manifest/checksum/file hash; and a Validated candidate
appears under Review models.

**Recover:** correct the inline field, time-range, row-cap, or compatibility
error. Use the displayed error code and request ID in Audit. Cancel an active
job when appropriate, then rerun it after fixing the cause.

## 3. Review — model evidence

**What it does:** lets an administrator inspect evidence before allowing safe
shadow observation.

**Run:** open **Detail** for a candidate and check its purpose, dataset hashes,
metrics, engineering/scientific gates, and serving scope. Run an observational
evaluation. If the evidence is acceptable, select **Approve for Shadow** and
enter an audit reason.

**Verify:** the registry shows stage Shadow, the local confirmation says rules
remain live, and predictions begin appearing in Monitor.

**Recover:** reject an unsuitable candidate with a reason. Use **Stop shadow
(rollback)** if confidence is lost; this archives the shadow model while rules
remain authoritative.

## 4. Monitor — shadow evidence

**What it does:** compares rule and shadow outputs and displays fallbacks,
missing features, latency, and drift. Nothing here automatically retrains or
deploys a model.

**Run:** choose the comparison window and model, inspect recent predictions,
then run a drift check.

**Verify:** predictions arrive for the expected model, fallback/failure rates
are acceptable, reports have enough samples, and no feature-set compatibility
warning remains.

**Recover:** compute missing features or approve a compatible model if evidence
is absent. Return to Review and stop shadow whenever safety or confidence is in
doubt.

## 5. Audit — explain and troubleshoot

**What it does:** correlates administrator actions with ML API calls.

**Run:** read **Administrator activity**, then enable **Errors only** in Recent
API calls. Match event time, request ID, action, and target.

**Verify:** actor, reason, target, and API outcome describe the same event; a
successful retry appears after the failed request.

**Recover by response:**

- `401`: sign in again.
- `404`: refresh and choose a current record.
- `409`: refresh and review the current lifecycle state or unmet gate.
- `422`: correct the required fields or incompatible options.
- `5xx`: check System health, retain the request ID, and retry after recovery.

## UI and behavior changes in this redesign

- Five persistent lifecycle stages and workspace navigation now stay in sync.
- Every workspace has a live run/verify/recover runbook and Previous, first-task,
  and Next navigation.
- Statuses use plain text plus color; a status legend explains their meaning.
- Technical model and algorithm identifiers have readable labels while their
  underlying API values remain unchanged.
- Control hints are available through native hover text and screen-reader
  descriptions. Each card has a visible **Guide** control with fuller help.
- Empty queue and registry states provide one clear next action.
- Lifecycle confirmations use a shared accessible alert dialog with focus
  containment, Escape/backdrop handling, focus restoration, and a required
  audit reason. The dialog is outside filtered workspaces, so it cannot be
  accidentally hidden.
- Errors appear beside the action or section that failed and include a recovery
  instruction, stable error code, and request ID when available.
- The old `#operations-queue` fragment remains accepted for compatibility and
  is removed from the visible URL after navigation.

No endpoint, payload, permission, decision-authority rule, polling contract, or
existing action was removed or renamed.

## Validation checklist

1. Run `python -m pytest tests/test_ml_ops_page.py -q` inside the API container.
2. Run `python -m pytest tests/test_ml_job_architecture.py -q` inside the API
   container.
3. Sign in and confirm `/admin/ml-ops` loads while an unauthenticated request is
   redirected to `/signin`.
4. Exercise all five workspace buttons, lifecycle buttons, Previous/Next, and
   first-task focus behavior at desktop and narrow viewport widths.
5. Open a Guide and a lifecycle confirmation; verify keyboard focus remains in
   the dialog, Escape restores focus, and a reason shorter than three characters
   is rejected beside the input.
6. Refresh the console and verify all read-only workflow endpoints respond:
   overview, jobs, models, shadow summary, predictions, drift reports, dataset
   definitions/datasets, labels, policy, audit, and calls.
7. Confirm the browser console has no uncaught error and that any displayed
   failure includes a next step.

Expensive training, dataset creation, lifecycle approvals, and rollback should
be exercised in an isolated test environment, not against a production database.


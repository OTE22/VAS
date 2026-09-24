# ML Operations readiness review — 24 September 2026

## Verdict

The admin console is available for preparing data and running controlled experiments. The current installation is not ready to deploy a trained model: the live read-only checks found zero feature snapshots, zero datasets, zero models and zero reviewed labels. The ML worker was healthy and idle. Rules are the current decision authority.

## What was checked

Authenticated overview, job queue, datasets, models and capabilities endpoints all returned HTTP 200. Notebook SSO was previously verified with browser loading, real cell execution and revoked-session disconnection. No training, label creation, model approval or decision-mode changes were performed on production during this review.

The isolated workflow suite covers validation and dataset exploration, real model fitting, evaluation, artifact reload and scoring using temporary data. This supports implementation readiness, not the quality of a future model trained on operational data.

## Current prerequisites and limits

- Collect features from existing observations, then build and validate a dataset before training.
- Supervised ranking requires 100 reviewed manual labels, including at least 25 positive and 25 negative examples; none currently qualify.
- Person anomaly training offers Isolation Forest and the median/MAD baseline without requiring supervised labels. Dataset validation and model gates still apply.
- XGBoost, Optuna, SHAP and MLflow are installed but disabled in settings. They are not required for an initial baseline experiment. Numeric regression currently has no available algorithm while XGBoost is disabled.
- Live ML and hybrid modes are gated. A completed training run does not establish deployment readiness. Review actual evaluation, intended use and permitted approval actions.
- Drift capability reports disabled until deployed models have real inference data. Historical drift reports remain readable.
- Jupyter is a shared administrator workspace, not separate private notebooks per user.

## Usability changes

The recommended next action now precedes the reference guides. It distinguishes checking, unavailable evidence, running work, feature preparation, dataset creation, training and candidate review. Unknown worker status no longer appears ready. Failed data/model loads are not interpreted as empty lists.

Dataset and model inspection is a collapsed evidence browser. Detailed stage instructions remain available on demand. The notebook launcher stays in the page header. Recommended actions navigate to and focus the relevant existing control without submitting it.

At a desktop width of 1366 px, the recommended action appears roughly 323 px from the top, and the queue around 1005 px, compared with the previous queue around 2347 px. Desktop and narrow layouts were checked for horizontal overflow and navigation.

## First operational run

1. Confirm healthy worker status.
2. Select Prepare features, review collection scope and submit once.
3. Follow the collection job; investigate failures before building a dataset.
4. Build a compatible dataset and inspect its validation and samples.
5. Configure one baseline training run using defaults.
6. Review held-out evaluation, lineage and gates; approve only the supported intended use with a recorded reason.

Remaining operational validation requires representative real data. A successful temporary-data test is not evidence of useful predictive performance or readiness for autonomous decisions.

# ML Ops usability and service deployment recommendations

Read-only review, 2026-09-28. No model, mode, service binding, or application code changed during this review.

## Observed state

- ML decision mode: rules.
- 764 feature snapshots; zero datasets, models and labels at inspection time. Snapshot count alone does not establish usable training population or scientific readiness.
- Latest recorded ML training attempt failed with: “A clean, identifiable Git checkout is required for this run”. Production images exclude .git. The page offers this optional advanced reproducibility requirement; deployment provenance should instead be recorded at image build time and validated from an immutable build manifest.
- The page has five workspaces plus an evidence-stage browser, runbooks, a tour, task/algorithm choices, JSON configuration, tuning and explanation options. Guidance exists, but the operator still has to translate a service goal into separate technical actions.
- `updateNextStep` considers any validated model or usable dataset. It does not determine the next action for a selected consuming service/model family.
- Some displayed contracts are static: the overview contract labels the dataset VALID_FOR_EXPERIMENTATION and feature set ACTIVE independently of the selected dataset; permanent warnings say ML is gated even though the decision router has conditional ML activation. These should come from current, scoped readiness, including an explicit unavailable state.

## Actual service boundaries

- Behavioral anomaly models have a path through `DecisionService`, used by threat-assessment endpoints. Shadow predictions are observational. With validated evidence/mapping and ML mode, a model can supply the anomaly input; the rules engine still computes the final score. The standalone statistical anomaly endpoint is not automatically converted to ML by training a model.
- Coappearance and graph anomaly models have on-demand scoring in `/api/ml/score/relational`. The inspected frontend calls that endpoint from the ML Ops testing interface, not from the social-network or suspicious-pattern workflow. Creating a model alone does not connect those screens to it.
- Threat ranking uses `/api/ml/rank/threat-review` for analyst prioritization. It does not publish a live threat verdict.
- Numeric regression is an offline model family, not a live security consumer.
- The Advanced Features threshold learner, trajectory predictor and activity-correlation analyzer are separate algorithms. Deploying an ML Ops model does not replace them.

## Recommended operator experience

Default landing page: Services. Each card answers:

1. What does this improve?
2. What is serving now: Rules only, Testing alongside rules, Active model, or Falling back?
3. Which model version is selected and which version was actually used last?
4. What is blocking the next action, and what single action resolves it?
5. When did a real request last use this service successfully?

Use one scoped workflow: Choose service → Prepare and train → Test alongside current service → Review and activate → Monitor/roll back.

Keep the service, camera scope, dates, dataset version, run and model associated throughout the workflow and across page reloads. Do not infer progress from an unrelated saved model or dataset.

### Prepare and train

One durable orchestration job should perform preflight, compute/reuse features, build/validate an immutable dataset, train a compatible default model and evaluate it. Persist each step, reuse completed steps where safe, and show a concise remedy for failure. A snapshot count is not a substitute for sample coverage, valid splits or required reviewed labels.

Hide algorithm selection, seeds, JSON, Optuna, SHAP, notebooks and tracking diagnostics under Advanced. Keep those tools available. Default settings should follow the selected service and dataset contract, not a global form selection.

Build provenance should contain commit/revision, dirty-tree state at build, dependencies and image identity. A production run should validate that manifest rather than require a runtime .git directory. Do not reintroduce .git or credentials into the image.

### Test and activate

Start with observational testing where the model family supports it. Display sample coverage, missing inputs, failures, measured latency, baseline comparison and evidence blockers. Readiness comes from the backend; unavailable checks must remain unknown/blocking, not turn green.

Activation must name the consuming service and camera scope, validate artifact/schema/threshold compatibility, exercise the inference path, and atomically select the model and required thresholds/mapping. Keep previous selection for rollback. Do not remove evidence or release gates to make activation look simpler. HYBRID remains unavailable until its actual combination policy and required evidence exist.

### Make consuming services use deployed models

Add an explicit deployment record keyed by service/model family and supported scope, resolving to an immutable model version and its compatible feature/threshold/mapping versions. Reuse existing inference and decision services; consumers should request the approved model for their use case rather than choose an arbitrary newest registry row or require manual model IDs.

Integrate observational pair/graph results into the corresponding investigation screens, and ranking into the analyst queue, with their limitations visible. Do not silently change operational risk decisions when wiring these consumers.

Every consuming result should carry requested mode, executed mode, model version, input/schema version, threshold/mapping version, timestamp and fallback reason. Count real invocations and expose last success, failures and latency on the service card. A healthy container or trained registry entry is not proof a service is consuming that model.

## Implementation order

1. Correct scoped status and the build-provenance training blocker; show the real consumer destination before training.
2. Add the Services landing page and one persistent service-scoped Prepare/train/test workflow; put engineering options under Advanced.
3. Introduce versioned service deployment/rollback records and connect the existing pair/graph/ranking consumers explicitly, with behavioral risk routed through the current decision service.
4. Add an end-to-end verification showing a consuming service actually used the selected version, plus schema-mismatch, missing-feature, failed-inference and rollback tests.
5. Measure real-camera performance and analytical quality once representative cameras, history and reviewed evidence are available.

Acceptance: an operator can enable one supported use case without JSON, notebooks, manual model IDs or rebuilding application containers; can see the exact blocker when activation is unavailable; and can verify the selected version was used by the intended service or see why it fell back.

Code references: frontend/admin/ml-ops.html; frontend/js/admin-ml-ops.js:updateNextStep; backend/ml/model_specs.py; backend/ml/model_scoring_service.py; backend/ml/decision_service.py; backend/ml/system_state.py; backend/routes/ml_ops.py.

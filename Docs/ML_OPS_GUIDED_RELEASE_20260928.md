# Guided ML Services release — 2026-09-28

## Operator workflow

Open `/admin/ml-ops` and choose the service you want to improve. Its selected model, blockers, compatible datasets, and usage are independent of the other services. Advanced tools remain available below the guided workspace.

1. **Prepare & train:** use recommended settings to incrementally prepare features, build and validate an immutable dataset, then train/evaluate/register a candidate in one durable worker job. Alternatively reuse a compatible saved dataset. Follow the named stage and error remedy; starting training does not activate a model.
2. **Test & review:** select a candidate for that service. Inspect engineering checks, evaluation evidence, scientific gates and lineage. Recheck readiness when evidence changes. Training completion alone does not approve deployment.
3. **Connect service:** review the exact candidate and confirm a reason. The server verifies family, schema, artifact checksum and lifecycle requirements. Behavioral assessment initially observes alongside rules; enabling live ML still requires its separate scientific and service readiness gates.
4. **Monitor:** see the selected version, latest actual use, fallback details and the consuming application. ML Ops test requests are not reported as Security Intelligence use. Use **Stop using model** to stop the current selection; behavioral assessment returns to rules. This does not automatically restore an archived version.

Service selection and current stage survive a browser reload. Only compatible saved datasets appear in the guided picker. Worker/API failures disable dependent mutations and explain the missing condition.

## What consumes each family

| Service | Actual destination | Behavior |
| --- | --- | --- |
| Behavior assessment | Security Intelligence threat assessment | Rules remain authoritative in shadow; live ML stays gated. |
| Pair relationships | Security Intelligence network analysis | Select two identities and include deployed-model observations. |
| Social network | Security Intelligence network analysis | Select one identity and include deployed-model observations. |
| Analyst review queue | ML Ops analyst review | Explicitly approved ranking model orders review work. Sufficient reviewed positive/negative labels are required. |
| Numeric experiments | Offline experiments | Requires an explicit numeric target and saved dataset in Advanced tools. No live service connection is offered. |

Pair/graph observations name their model version and threshold. They do not change the graph, threat score, or identity decision. Missing/unavailable models leave the statistical network result usable.

## Release safeguards

- Existing durable jobs, permissions, CSRF checks, request bounds and registry gates remain in use. No database schema migration is required.
- Model replacement is atomic; a failed artifact verification retains the previous binding.
- Production images seal their source/dependency identity without carrying `.git` or credentials. Clean-code training requirements accept only verified committed builds; dirty or altered images remain ineligible. See [build provenance](BUILD_PROVENANCE.md).
- Training and model activation were not performed against production data during implementation tests. Real sample coverage, labels and scientific evidence remain prerequisites where applicable.
- This release does not establish 30-camera throughput or accuracy. Those require representative cameras and a sustained load test.

## Validation and rollout record

Backend tests use a disposable PostgreSQL database on an internal Docker network. Browser workflow tests use isolated API fixtures. Synthetic fit/artifact checks use temporary files. Production checks are read-only health, code identity, route protection and service heartbeat checks.

Checks before image build:

- 82 isolated intelligence/guided-training/service tests passed with disposable PostgreSQL, including exact-version consumption, failed-deployment preservation and schema rejection.
- 71 page/architecture/frontend contract checks passed; two tests requiring a live test login were excluded from this isolated run.
- 19 JavaScript guidance scenarios passed in Node 24.
- 9 isolated workflow/artifact tests passed, including a synthetic fit/evaluation/artifact round trip.
- 23 standalone build-provenance tests passed.
- Firefox fixture checks passed for compatible selections, persistence, durable-job progress/failure messages, evidence review, exact-hash connection, unavailable-service blocking, offline-only experiments and a 500px layout; zero JavaScript errors.

Final image and rollout verification:

- Built-image backend suite: **83 passed**; built-image static contracts: **71 passed**, two live-login tests excluded. The additional backend case covers rejecting a mismatched input feature schema.
- Security Intelligence Firefox checks passed on the real graph canvas for pair/graph requests, exact model labels, no deployed model, HTTP failure, larger selections and checkbox changes during pending requests. Model-observation failures preserve the existing network view.
- Release source commit: `1dd400c13bab02efadacf0847081ee08bd39bf2b`.
- API image: `sha256:3155281a3b789b18608060696da2a0aa12bc9a12b40104f63783a2382617fb69`.
- ML worker image: `sha256:93cfceb92b31e154e71e6e1d2b139da6bdde4fd69e226cb68550f24ad8e4e9c9`.
- Both running containers report verified clean image provenance, build ID `fbced738f8a03b2783c88f8730865365387399d96273624bdea04811a1983cd6`.
- Recreated only `face_recognition` and `ml_worker`; no active ML jobs existed before restart. Prior images remain tagged `before-guided-mlops-20260928` for infrastructure rollback.
- Verified from VMS through the configured HTTPS CA: `/health/ready` returned 200, all 34 background services healthy, database/models/cache/queue healthy; protected ML status returned 401 without credentials; ML Ops redirected unauthenticated access to sign-in; updated JavaScript returned 200.
- Read-only live service projection succeeded: worker healthy/idle, decision mode rules, zero models/datasets; 392 behavior snapshots and 372 pair snapshots. Those counts are historical feature rows, not training-quality evidence.
- No production labels, training jobs or model selections were created by validation. Disposable test database/network and test browser fixtures were stopped after use.

The workflow is deployed. Model training and governed activation remain operator actions using real available data. Camera-capacity validation remains a separate deployment exercise.

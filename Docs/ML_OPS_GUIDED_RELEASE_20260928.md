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

Final image and rollout results follow after verification.

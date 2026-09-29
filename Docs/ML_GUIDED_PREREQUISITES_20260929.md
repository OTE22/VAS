# Guided prerequisites and first-cell dataset preview

The existing Prepare & train → Test & review → Connect service → Monitor workflow remains in place. A six-item progress checklist explains its prerequisites, preserves saved selections, and links into the existing panels. Users can inspect any panel; mutation buttons require current server evidence. A trained candidate is distinct from a selected model and a model with recorded successful consumption.

`backend/ml/workflow_policy.py` shares connection prerequisites between the service facade, serialized model evidence and the registry transition under its existing row lock. Both guided deployment and direct registry shadow/ranking approval require engineering checks, matching schema, recorded dataset/revision and nonempty train/validation/test evaluation. Ranking also requires both outcome classes in validation/test. These are minimum coverage checks, not scientific accuracy approval. Existing artifact verification, explicit confirmation/audit, anomaly shadow cap, decision-mode gates and stop actions remain. Existing selections are not automatically changed; deficient old evidence is shown as requiring review.

Source readiness is read-only: missing camera observations, insufficient eligible relationship history and reviewed-label requirements are explained before a guided fresh-data job. A compatible saved dataset can bypass absent current raw history; supervised label governance remains. Immutable worker validation still determines actual eligibility. The lower-level experiment workflow is preserved; it cannot bypass the registry connection gate.

The first executable cell in dataset and workflow notebooks now says **THIS IS THE DATA USED** and previews up to 20 rows (editable up to 100) from the exact saved Parquet dataset. It shows dataset/model/version context, source snapshot identifiers, timestamps, labels, split assignments and individual feature columns. It verifies path containment and the recorded file SHA-256 before displaying records. HTML values are escaped. Excluded rows are explicitly marked; this is extracted model data, not raw images or a query of today's mutable source tables. Full validation and split rechecks remain later cells. Missing snapshots are reported honestly; no synthetic replacement is generated. Existing archived notebooks are not rewritten.

Validation: 137 isolated backend tests with disposable PostgreSQL; 14 notebook execution tests covering all five families, first-cell preview and tamper rejection; 26 Node guidance/compatibility scenarios. Firefox fixtures exercise preserved selection, refresh/resume, guided job creation, failure messages, exact-candidate blocking, explicit connection, unavailable status, offline regression and mobile overflow; zero JavaScript errors. Tests use local fixture responses, not production login or writes.

## Deployment and application-data notebooks

Release `bee3d35325a9ca1e72a63f2b7ac7b5a211e60188` was built with clean verified provenance (build ID `4bac821a862cdb1cc7a4768104cc0c0dbdbedb11faedb893cc878685b70ff738`). The baked worker image passed all 137 backend tests before API/worker recreation. Jupyter was not restarted and no model selection was changed.

Fresh read-only exports of the actual behavioral v3 and pair v1 candidates were generated, then fully executed in isolated Jupyter kernels with the production artifacts volume mounted read-only and network disabled. Each completed 21 code cells; the first output includes a verified data table. Dataset previews show the first 20 of 358 behavioral extracted rows and 930 pair extracted rows. Dataset files include excluded rows; the split column explains whether a row enters training/evaluation.

Open:
- `/notebooks/lab/tree/Production-data-20260929/Behavior-v3-data-used.ipynb`
- `/notebooks/lab/tree/Production-data-20260929/Pair-v1-data-used.ipynb`

These application-data notebooks remain distinct from the archived synthetic validation bundles. Their original model evaluation gaps are preserved and the new connection policy correctly reports EVALUATION_COVERAGE_REQUIRED. Graph/label source readiness is blocked as expected.

TLS-verified live readiness returned HTTP 200 at `2026-09-29T05:29:54Z`; all 34 background services had no degraded/stale entries. The served guidance script contains the new controls. Disposable database/network were removed after testing.

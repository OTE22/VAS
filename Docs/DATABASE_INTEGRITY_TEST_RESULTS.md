# Populated database integrity test results

> **Follow-up:** fixes are now implemented and passed 59 isolated tests, including all six previously failing cases. Production migration is still pending. See [DATABASE_INTEGRITY_FIX.md](DATABASE_INTEGRITY_FIX.md).

Date: 2026-09-15. **41 passed, 6 expected failures; no unexpected failures in the final run.**

These six expected failures reproduce four missing safeguards. They are not successful integrity checks and do not establish production acceptance. They are marked strict `xfail`, limited to the specific missing-rejection assertion; unrelated infrastructure exceptions still fail. A future fix that makes a safeguard test pass requires removing its expected-failure mark.

## Isolation and scope

Copied only the deployed VAS database schema using `pg_dump --schema-only --no-owner --no-privileges`, including migration-created indexes and constraints. Restored into a temporary PostgreSQL 15/pgvector container named `vas-normalization-qa-*`, with temporary in-memory data storage, no published ports and no external network. Tests shared only that container's isolated network namespace.

No production rows were copied. Synthetic data was inserted in the temporary database and rolled back per test. The test database container was removed afterwards. Production application, chatbot code, data and schema were unchanged.

## Passing behavior

- Unknown camera rejected through a real relational foreign key.
- Camera deletion blocked while detections reference it.
- Duplicate watchlist memberships rejected.
- Multiple primary photos for one identity rejected.
- Duplicate alert/detection triggers rejected by the migration-created partial unique index.
- Detection removal clears the detection link while preserving appearance camera/UUID provenance.
- Photo removal clears the image link while preserving the embedding's owner.
- Identity deletion cascades to its images, embeddings and live alerts.
- Actual merge consolidation preserves image/embedding ownership, the primary-photo rule, watchlist membership and alert ownership. Both different-photo and duplicate-checksum cases passed.
- Existing populated detection-replay, expiry-maintenance and related-identity-filter tests passed, alongside targeted background-job failure handling tests.

Merge tests bypass only the face-similarity compatibility gate with an explicit mock; they exercise real database transfer/consolidation. Test photos have synthetic absent paths: these tests do not validate image processing or physical file copying. They do not constitute an authenticated browser/API end-to-end test, load test, concurrent merge test, full migration upgrade test or backup-restore recovery test.

## Confirmed gaps

| Finding | What the tests demonstrated | Checks |
| --- | --- | ---: |
| N1: Camera-list validation | Both a direct database write and `LiveAlertService.create_alert` accept a nonexistent camera ID in the JSON camera list. | 2 expected failures |
| N1: Weekday validation | Both a direct database write and `LiveAlertService.create_alert` accept weekday 9. | 2 expected failures |
| N2: Ownership consistency | A database insert can link an embedding owned by identity B to a photo owned by identity A. The individual foreign keys do not reject this. | 1 expected failure |
| N3: Suggestion membership | A database insert accepts nonexistent identity IDs in a merge suggestion's JSON membership list. | 1 expected failure |

The last two tests establish database-level gaps; they do not prove that every public application path permits the same invalid state. The camera/day tests also establish service-layer acceptance, but do not exercise all HTTP middleware or browser validation.

A separate passing characterization test confirmed that multiple embeddings per image are currently permitted. Whether that is wrong depends on the intended model-version/cardinality policy; it is not counted as a defect here.

## Reproduce

From the repository root on a host with the deployed VAS PostgreSQL container and the existing `face-detector/api:dev` and `pgvector/pgvector:pg15` images:

```bash
sudo bash scripts/test_database_integrity.sh
```

The runner reads the production schema, creates its own disposable database, runs the four selected test files, removes the database container, and prints the temporary evidence directory. It never points pytest at production. The new test module is skipped unless the isolated-test flag is explicitly enabled and enforces the temporary database name and loopback address.

New tests: `tests/test_normalization_integrity.py`.
Related suites: `tests/test_background_job_fixes.py`, `tests/test_expiry_maintenance.py`, `tests/test_related_identity_filters.py`.

## Decision

The earlier concerns now have populated-test evidence. Existing relational constraints and the tested merge/retention paths work. Correct the demonstrated alert validation and ownership/member integrity gaps before claiming those safeguards are enforced; this does not require blanket 4NF conversion. No application fix was applied as part of this testing request.

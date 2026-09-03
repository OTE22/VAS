# Durable ML Job Architecture

## Decision

CPU- and filesystem-heavy ML operations execute outside the HTTP process.
The API validates a command and atomically commits a `background_task_history`
queue row with its ML audit event. A separate `ml_worker` service claims due
rows with PostgreSQL `FOR UPDATE SKIP LOCKED`, starts each operation in an
isolated child process, and renews a database lease while it runs.

The first production topology serializes ML jobs in one worker. This bounds
CPU and memory without adding a broker. More worker replicas may be added
later because row claims are concurrency-safe, though the partial unique index
continues to allow only one active job of each expensive operation type.

## Lifecycle

`scheduled -> running -> completed | failed | cancelled`

- The database, never browser memory or an API process, owns job state.
- Cancellation is persisted and the worker terminates the child process.
- The worker heartbeat renews `lease_expires_at`.
- An expired lease becomes `WORKER_LEASE_EXPIRED`.
- Training and dataset publication are not automatically retried after an
  unknown crash point; an automatic replay could duplicate model lineage.
- Collection remains checkpointed and can be safely resubmitted.

## Boundaries

- API: authentication, authorization, CSRF, validation, audit, enqueue, status.
- Worker supervisor: claim, heartbeat, cancellation, child-process isolation.
- Job child: collection, dataset build, legacy-hash verification,
  training/evaluation, or drift report.
- Online inference: remains in the API and retains rules fallback behavior.

The worker has no published port and is connected only to the data network in
production. It shares the persistent ML artifact volume with the API.

## Rollout

1. Apply Alembic revisions through `fbb2c3d4e5f6`.
2. Deploy/recreate the API and `ml_worker` from the same code revision.
3. Verify `GET /api/ml/jobs` and worker heartbeat fields on a small drift job.
4. Run feature collection, a small dataset build, then a training candidate.
5. Alert on `WORKER_LEASE_EXPIRED`, repeated `ML_JOB_PROCESS_FAILED`, and queue
   age above the expected operational window.

Rollback is safe before accepting new jobs: stop `ml_worker`, return the API
image to the earlier version, and downgrade the migration only after confirming
there are no scheduled/running `queue_name='ml'` rows.

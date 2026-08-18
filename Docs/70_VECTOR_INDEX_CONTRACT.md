# The vector index contract

**Status:** live. `backend/core/vector_index/` is deleted; the design below
replaces it.

For how an image reaches this index in the first place — enrollment, camera
ingest, quality scoring and recognition — see
**`Docs/71_IMAGE_INGESTION_WORKFLOW.md`**.

Anything in the older FAISS documents (`70_VECTOR_INDEX_CONTRACT.md`,
`70_VECTOR_INDEX_CONTRACT.md`) that describes `IdentityIndexService`,
two separate KNOWN/UNKNOWN indexes, positional `faiss_id` values, or four
side-by-side index artifacts describes code that no longer exists.

---

## The one rule

**PostgreSQL is authoritative. The index is a disposable acceleration layer.**

Every vector lives in `identity_embeddings.embedding`. Deleting every snapshot
on disk loses nothing: the index reconstructs from the database, and the
reconstruction is a tested release gate, not a theoretical capability.

The old design violated this. Under `VECTOR_BACKEND=faiss`, `save_embedding`
wrote `faiss_id` and `faiss_index_type` but never `embedding` — so the index
file was the only copy of each vector, and "rebuild from database" called
`reconstruct()` on the in-memory index because the database held nothing to
rebuild from.

## Keys

The contract is keyed on **`identity_embeddings.id`** — an `integer` primary
key, which fits FAISS's int64 id space losslessly and is stable across rebuilds.

Not the identity id: that is a UUID (128 bits, doesn't fit), and one identity
owns many embeddings. Not a positional index either — positions shift on every
rebuild, which is how the old `reconstruct(faiss_id)` call sites could return a
different person's vector after a rebuild.

**The contract never sees an identity id.** Search returns embedding keys;
`IdentityService.search_vector_index` resolves them through PostgreSQL and
groups by identity, keeping each person's best score. That indirection is what
lets the index implementation change without touching a schema or a route.

## Which identities are indexed

`ACTIVE` and `PROMOTED` (`SEARCHABLE_STATUSES` in `vector_index/base.py`).

`PROMOTED` is not a retired state — it is what an identity becomes the moment
somebody names it, i.e. exactly the enrolled people recognition needs to find.
`MERGED` and `INACTIVE` are excluded: their vectors either now live under
another identity or were deliberately retired.

## Sync state

`identity_embeddings.vector_index_sync_state`, one canonical column. The
complete, exclusive transition set:

```
pending -> synced      synchronisation succeeded
pending -> failed      synchronisation failed
failed  -> pending     retry requested
synced  -> pending     reconciliation found it missing, stale or mismatched
```

**Deletion is not a state.** Removing an embedding deletes the row and the index
entry; it never writes a "deleted" value.

Write order is: insert the row with `pending` → **COMMIT** → sync the index →
mark `synced` or `failed`. A crash leaves a durable, discoverable `pending` row.
**A committed embedding is never deleted or rolled back because indexing
failed** — the vector is already authoritative; only its sync state changes.

`identity_images.faiss_sync_state` was removed rather than renamed: it was
written in three places and read by nothing.

## Snapshots

`snapshot-<n>/{index.bin, keys.json, manifest.json}`, committed by a single
atomic pointer swap:

1. write every file, `fsync` **each**;
2. `fsync` the snapshot directory;
3. `os.replace` the `CURRENT` pointer — the one commit point;
4. `fsync` the parent directory.

A crash before step 3 leaves the previous snapshot authoritative. On load the
checksum, `ntotal`, key count, dimension and index type are all verified; any
mismatch quarantines the snapshot and rebuilds from PostgreSQL.

`PgVectorIndex.save()` raises `UnsupportedOperation` and `load()` returns
`LoadResult(supported=False)`. A quiet no-op would read as "a snapshot exists" —
the kind of fake success that turns a recovery drill into a data-loss incident.

## Cadence

| Setting | Default | Why |
|---|---|---|
| `VECTOR_INDEX_AUTOSAVE_INTERVAL_SECONDS` | 900 | A 100k snapshot writes ~201 MB: **2.0 s measured** on the configured `/app/database` ext4 volume, up to **~21 s** on the 9p bind mount or under load. 900 s is ~43x the worst case. Values below the 120 s floor are clamped, with a warning. |
| `VECTOR_INDEX_RECONCILE_INTERVAL_SECONDS` | 3600 | Drift detection, not a hot path. |

**Overlapping saves are SKIPPED, never queued** — by an in-process flag and by
`DistributedLock("vector-index-save")`. A queued duplicate would burn a full
save writing bytes the run that overtook it already wrote. Skipping is safe precisely
because PostgreSQL can rebuild the index.

Both loops start **only** when the active backend is `faiss`. Under pgvector
there is nothing to snapshot or reconcile, and the three legacy loops that used
to run unconditionally (they saved an empty index 527 times) are gone.

## Reconciliation

Compares **stable keys + model version + vector checksum + identity status** —
never `COUNT(*)` against `ntotal`. Equal counts routinely hide an unequal set:
one vector missing and one stale vector present counts identically to a healthy
index, and the old size-mismatch logic saw nothing.

## Backend selection

`VECTOR_BACKEND=faiss` with FAISS unavailable **fails startup**, unless
`VECTOR_INDEX_FALLBACK=pgvector` is set explicitly — which then logs CRITICAL,
reports degraded health, and writes a `vector_index_fallback` audit row. There
is no silent fallback.

An index type without an implementation (`hnsw`, `ivf`) fails startup naming the
supported set. Only `flat` ships.

## Observability

| Metric | Meaning |
|---|---|
| `fr_vector_index_size{backend}` | Vectors held. Under pgvector, counted from the database — that backend cannot report a count from memory, and publishing 0 read as an outage. |
| `fr_vector_index_drift{backend}` | Entries disagreeing with PostgreSQL at the last reconciliation. |
| `fr_vector_index_pending{backend}` | Committed rows not confirmed in the index, **scoped to searchable identities** so merged-away rows cannot park in the backlog forever. |
| `fr_vector_index_last_rebuild_timestamp{backend}` | Last successful rebuild. |
| `fr_vector_index_recovery_failures_total{backend,reason}` | Snapshots rejected on load and quarantined. |

Refreshed on `/metrics` scrape as well as by the loops, so the gauges stay live
under pgvector where no loop runs.

Audit rows (`identity_audit_log`, action types `vector_index_*`) are written for
rebuild, reconcile, removal, corruption recovery and configured fallback. They
are attributed to the `system` principal created by migration `a3b4c5d6e7f8`: a
login-disabled account that exists solely to satisfy the NOT NULL foreign key.
Attributing an automated rebuild to a human admin would be a false record.

## Scale

Exact `IndexIDMap2(IndexFlatIP)`. At 100k × 512 float32 that is 205 MB resident
and a linear scan of ~51 MFLOP per query across 8 OMP threads. IVF/HNSW buy
nothing before roughly 1M vectors and cost recall. Swap only on recorded
evidence — see `tests/test_vector_index_benchmark.py`, which is excluded from
normal CI behind `@pytest.mark.benchmark`.

## Tests

| File | Covers |
|---|---|
| `test_vector_index_contract.py` | add/search/remove/rebuild against every implementation |
| `test_vector_index_recovery.py` | truncation, checksum mismatch, torn snapshot, quarantine, DB-only rebuild |
| `test_vector_index_reconcile.py` | drift both directions, pending recovery, model/checksum mismatch |
| `test_vector_index_concurrency.py` | threaded add/remove/search, save lock discipline |
| `test_vector_index_migration.py` | schema, and that a downgrade never touches `embedding` |
| `test_vector_index_integration.py` | the running app: backend selection, loop gating, no legacy imports, metrics, audit |
| `test_faiss_integrity.py` | keeps the retired module retired |
| `test_vector_index_benchmark.py` | 100k p50/p95/throughput/memory (marked, not in CI) |

---

## Why pgvector, and not FAISS

The decision is made and shipped: `VECTOR_BACKEND=pgvector`. The reasoning,
kept because it explains constraints the code still honours:

| | pgvector | FAISS |
|---|---|---|
| Consistency | ACID — the embedding is written in the same transaction as its identity | a separate index that can drift from the database |
| Persistence | automatic | manual save/load; a failed save loses work |
| Backup | included in `pg_dump` | a second artefact to back up and restore in step |
| Multi-instance | works — instances share the database | needs shared storage, or each instance drifts |
| Recovery | reconcile from the rows | rebuild, and hope the rows are intact |
| Search | 5–20 ms at 1M vectors | 1–5 ms at 1M vectors |

FAISS is faster in isolation. It lost on the property that matters here:
**PostgreSQL is the only source of truth, so an index that can silently
disagree with it is a liability, not an optimisation.** That is the rule at the
top of this document, and it is why the FAISS path is a disposable accelerator
rather than a peer.

### Why the same face can score differently on the two backends

A comparison that used to live in its own document, and confused people into
thinking pgvector was less accurate:

* Both compute **cosine similarity over L2-normalised vectors**, so the metric
  is identical. A difference in score is not a difference in mathematics.
* FAISS `IndexFlatIP` is an **exact** search — it examines every vector.
  pgvector with an HNSW index is **approximate**: it explores a bounded
  neighbourhood, so it can miss the true nearest neighbour and return a
  slightly lower best score.
* The gap is a **recall** artefact, not a modelling one, and it closes as
  `ef_search` rises.

So a lower pgvector score for a known-good match usually means `ef_search` is
too low — not that the embedding or the threshold is wrong.

## Tuning HNSW

| Setting | Default in `config.py` | What it does |
|---|---|---|
| `PGVECTOR_INDEX_TYPE` | `hnsw` | index type |
| `PGVECTOR_HNSW_M` | `16` | connections per node. Build-time; changing it requires an index rebuild |
| `PGVECTOR_HNSW_EF_CONSTRUCTION` | `100` | candidate list width while building. Build-time; higher = better index, slower build |
| `PGVECTOR_HNSW_EF_SEARCH` | `100` | candidates explored per query. **Search-time — changing it needs no rebuild** |

> **Read your own values.** The compose files override some of these
> (`PGVECTOR_HNSW_M: 32`, `PGVECTOR_HNSW_EF_CONSTRUCTION: 128`), so the running
> configuration is not the `config.py` default:
>
> ```bash
> docker compose $COMPOSE exec -e PYTHONPATH=/app -w /app face_recognition \
>   python -c "from config import settings; print(settings.PGVECTOR_HNSW_M, settings.PGVECTOR_HNSW_EF_CONSTRUCTION, settings.PGVECTOR_HNSW_EF_SEARCH)"
> ```

**If recognition misses a face you believe is enrolled**, raise
`PGVECTOR_HNSW_EF_SEARCH` first. It is the only one of the three that takes
effect without rebuilding the index, and recall — not the threshold — is the
usual cause. Lowering `SIMILARITY_THRESHOLD` to compensate for poor recall
trades a missed match for a false one.

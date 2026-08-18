"""
Exact FAISS index (IndexFlatIP) keyed by `identity_embeddings.id`.
==================================================================

Chosen deliberately for the 10k-100k target: 100k x 512 x float32 is ~205 MB
resident and an exact scan answers in single-digit milliseconds across the 8
OMP threads available. IVF/HNSW buy nothing before ~1M vectors and cost recall,
so exactness is free here.

Two structural choices fix the defects that made the previous implementation
unsafe:

* **`IndexIDMap2` carries the database row id.** Deletion is a real
  `remove_ids`, not metadata tombstoning. Previously a "deleted" vector stayed
  in the index forever and, because callers search with `top_k=1`, a tombstone
  that happened to be the nearest neighbour consumed the only slot and the
  search returned nothing — a deleted person silently hid the live person
  behind them.

* **Snapshots commit through a single atomic pointer.** The previous save did
  four sequential `os.replace` calls: each file atomic, the set not. A crash
  between them paired a new index with old metadata, and nothing could detect
  it (no checksum, and `ntotal` was never recorded). Here everything is written
  into a fresh snapshot directory, fsynced, and only then does one rename of
  `CURRENT` publish it.

Nothing here is the only copy of anything: `rebuild_from_db` reconstructs the
whole index from `identity_embeddings.embedding` alone.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import threading
from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

import numpy as np

from backend.core.vector_index.base import (SEARCHABLE_STATUS_SQL, EMBEDDING_DIM, InvalidVector,
                                            LoadResult, RebuildReport,
                                            UnsupportedOperation,
                                            validate_vector, vector_checksum)

logger = logging.getLogger(__name__)

SNAPSHOT_FORMAT_VERSION = 2
CURRENT_POINTER = "CURRENT"
QUARANTINE_DIR = "quarantine"


def _fsync_path(path: str) -> None:
    """fsync a file or directory. Durability of a rename needs the DIRECTORY."""
    flags = os.O_RDONLY
    if os.path.isdir(path):
        flags |= getattr(os, "O_DIRECTORY", 0)
    fd = os.open(path, flags)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


# Distinguishes "the caller passed nothing" from "the caller passed None".
# embedding_model_version is nullable, so None is a legitimate stamp and cannot
# double as a not-supplied marker — see add_many.
_UNSET = object()


class FlatFaissIndex:
    """Exact inner-product index over L2-normalized vectors."""

    name = "flat_faiss"
    index_type = "flat"
    metric = "inner_product"

    def __init__(self, storage_dir: str, dim: int = EMBEDDING_DIM,
                 model_version: Optional[str] = None):
        import faiss

        self._faiss = faiss
        self.storage_dir = os.path.abspath(storage_dir)
        self.dim = dim
        self.model_version = model_version
        os.makedirs(self.storage_dir, exist_ok=True)

        # One lock for every mutation AND every read of index+sidecar together,
        # so a rebuild swapping both cannot be observed half-applied.
        self._lock = threading.RLock()
        self._save_version = 0
        self._last_rebuild_at: Optional[str] = None
        self._index = self._new_index()
        # key -> (model_version, checksum). The input reconciliation compares
        # against the database; equal counts alone never prove equal content.
        self._meta: Dict[int, Tuple[Optional[str], str]] = {}

    # -- construction ----------------------------------------------------

    def _new_index(self):
        # METRIC_INNER_PRODUCT is explicit. Vectors are normalized, so inner
        # product IS cosine similarity and `score >= threshold` reads correctly.
        base = self._faiss.IndexFlatIP(self.dim)
        return self._faiss.IndexIDMap2(base)

    # -- write path ------------------------------------------------------

    def add(self, key: int, vector: np.ndarray,
            model_version: Any = _UNSET) -> None:
        self.add_many([(key, vector)], model_version=model_version)

    def add_many(self, items: Sequence[Tuple[int, np.ndarray]],
                 model_version: Any = _UNSET) -> int:
        """Index vectors, stamping each with the model that produced it.

        `model_version` is the provenance OF THESE VECTORS, defaulting to the
        index's own configured model. It must be the row's value when
        reconciling: stamping every entry with the index's global model instead
        made a model-version mismatch un-fixable — reconciliation re-added the
        vector, stamped it with the old model again, and reported the same drift
        on every pass, forever.

        NULL IS A REAL VALUE, NOT AN ABSENCE. `identity_embeddings.
        embedding_model_version` is nullable and documented as "provenance
        unknown, recorded honestly", so a legacy row's true stamp is None. This
        used to default on `model_version is not None`, which meant reconcile
        re-added such a row as None, the index recorded the configured model
        instead, the next pass compared them unequal — and reported the same
        drift forever, the exact bug the paragraph above claims to have fixed,
        reintroduced for the one case where the row's value IS None. The _UNSET
        sentinel separates "caller said None" from "caller said nothing".
        """
        if not items:
            return 0
        stamp = self.model_version if model_version is _UNSET else model_version
        keys: List[int] = []
        rows: List[np.ndarray] = []
        metas: List[Tuple[int, Optional[str], str]] = []
        for key, vector in items:
            normalized = validate_vector(vector, dim=self.dim)
            key_int = int(key)
            if key_int < 0:
                raise InvalidVector(f"key must be a positive row id, got {key_int}")
            keys.append(key_int)
            rows.append(normalized)
            metas.append((key_int, stamp, vector_checksum(normalized)))

        matrix = np.vstack(rows).astype(np.float32)
        id_array = np.asarray(keys, dtype=np.int64)
        with self._lock:
            # Re-adding an existing key would duplicate it in IndexIDMap2.
            existing = [k for k in keys if k in self._meta]
            if existing:
                self._index.remove_ids(np.asarray(existing, dtype=np.int64))
            self._index.add_with_ids(matrix, id_array)
            for key_int, model, checksum in metas:
                self._meta[key_int] = (model, checksum)
        return len(keys)

    def remove(self, keys: Sequence[int]) -> int:
        wanted = [int(k) for k in keys]
        if not wanted:
            return 0
        with self._lock:
            present = [k for k in wanted if k in self._meta]
            if not present:
                return 0
            removed = int(self._index.remove_ids(np.asarray(present, dtype=np.int64)))
            for key in present:
                self._meta.pop(key, None)
        return removed

    # -- read path -------------------------------------------------------

    def search(self, vector: np.ndarray, top_k: int = 1,
               threshold: float = 0.0) -> List[Tuple[int, float]]:
        query = validate_vector(vector, dim=self.dim).reshape(1, -1)
        with self._lock:
            if self._index.ntotal == 0:
                return []
            k = max(1, min(int(top_k), self._index.ntotal))
            scores, ids = self._index.search(query, k)
        out: List[Tuple[int, float]] = []
        for score, key in zip(scores[0], ids[0]):
            if key < 0:               # FAISS's "no result" sentinel
                continue
            if float(score) >= threshold:
                out.append((int(key), float(score)))
        return out

    def contains(self, key: int) -> bool:
        with self._lock:
            return int(key) in self._meta

    def keys(self) -> Set[int]:
        with self._lock:
            return set(self._meta)

    def entry_meta(self, key: int) -> Optional[Tuple[Optional[str], str]]:
        with self._lock:
            return self._meta.get(int(key))

    # -- rebuild: the database is the only source ------------------------

    async def rebuild_from_db(self, session) -> RebuildReport:
        """Reconstruct the entire index from `identity_embeddings.embedding`.

        This reads the DATABASE — never the current index. The old
        implementation called `reconstruct()` on the very index it was
        rebuilding, so a lost or corrupt index could not be recovered and a
        failed rebuild nulled the DB linkage on its way out.

        Only vectors belonging to ACTIVE identities are indexed, so deleted or
        deactivated rows are dropped by construction.
        """
        from sqlalchemy import text

        report = RebuildReport()
        fresh = self._new_index()
        fresh_meta: Dict[int, Tuple[Optional[str], str]] = {}

        result = await session.execute(text(
            "SELECT e.id, e.embedding, e.embedding_model_version "
            "FROM identity_embeddings e "
            "JOIN identities i ON i.id = e.identity_id "
            "WHERE e.embedding IS NOT NULL AND " + SEARCHABLE_STATUS_SQL + " "
            "ORDER BY e.id"))

        batch: List[Tuple[int, np.ndarray]] = []
        for row in result:
            key, raw, model = row[0], row[1], row[2]
            try:
                vector = validate_vector(raw, dim=self.dim)
            except (InvalidVector, TypeError, ValueError):
                report.skipped_unusable += 1
                continue
            batch.append((int(key), vector))
            fresh_meta[int(key)] = (model, vector_checksum(vector))

        if batch:
            matrix = np.vstack([v for _k, v in batch]).astype(np.float32)
            ids = np.asarray([k for k, _v in batch], dtype=np.int64)
            fresh.add_with_ids(matrix, ids)
        report.rebuilt = len(batch)

        with self._lock:
            previous = set(self._meta)
            self._index = fresh
            self._meta = fresh_meta
            self._last_rebuild_at = datetime.utcnow().isoformat() + "Z"
            report.removed_stale = len(previous - set(fresh_meta))

        logger.info("[VECTOR_INDEX] rebuilt from database: %d vectors, "
                    "%d unusable rows skipped, %d stale keys dropped",
                    report.rebuilt, report.skipped_unusable, report.removed_stale)
        return report

    # -- persistence: one atomic pointer swap ----------------------------

    def _snapshot_root(self) -> str:
        return self.storage_dir

    def _pointer_path(self) -> str:
        return os.path.join(self.storage_dir, CURRENT_POINTER)

    def save(self) -> None:
        """Publish a snapshot through a single atomic commit point.

        1. write every file into snapshot-<n>/ and fsync EACH file
        2. fsync the snapshot directory (its entries become durable)
        3. os.replace the CURRENT pointer  <- the atomic commit
        4. fsync the parent directory (the swap survives power loss)

        A crash before (3) leaves the previous snapshot authoritative and the
        partial directory ignorable; after (3) the new snapshot is durable.
        """
        with self._lock:
            version = self._save_version + 1
            snapshot_name = f"snapshot-{version:06d}"
            snapshot_dir = os.path.join(self._snapshot_root(), snapshot_name)
            os.makedirs(snapshot_dir, exist_ok=True)

            index_path = os.path.join(snapshot_dir, "index.bin")
            keys_path = os.path.join(snapshot_dir, "keys.json")
            manifest_path = os.path.join(snapshot_dir, "manifest.json")

            try:
                self._faiss.write_index(self._index, index_path)
                _fsync_path(index_path)

                with open(keys_path, "w", encoding="utf-8") as handle:
                    json.dump({str(k): {"model": m, "sha": c}
                               for k, (m, c) in self._meta.items()}, handle)
                    handle.flush()
                    os.fsync(handle.fileno())

                with open(index_path, "rb") as handle:
                    digest = hashlib.sha256(handle.read()).hexdigest()

                manifest = {
                    "format_version": SNAPSHOT_FORMAT_VERSION,
                    "index_type": self.index_type,
                    "metric": self.metric,
                    "dim": self.dim,
                    "ntotal": int(self._index.ntotal),
                    "sha256": digest,
                    "save_version": version,
                    "saved_at": datetime.utcnow().isoformat() + "Z",
                    "embedding_model_version": self.model_version,
                }
                with open(manifest_path, "w", encoding="utf-8") as handle:
                    json.dump(manifest, handle, indent=1)
                    handle.flush()
                    os.fsync(handle.fileno())

                _fsync_path(snapshot_dir)          # step 2

                pointer_tmp = self._pointer_path() + ".tmp"
                with open(pointer_tmp, "w", encoding="utf-8") as handle:
                    handle.write(snapshot_name)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(pointer_tmp, self._pointer_path())   # step 3: commit
                _fsync_path(self._snapshot_root())              # step 4
            except Exception:
                shutil.rmtree(snapshot_dir, ignore_errors=True)
                raise

            self._save_version = version
            self._prune_snapshots(keep=2)
            logger.info("[VECTOR_INDEX] saved %s (%d vectors)",
                        snapshot_name, self._index.ntotal)

    def _prune_snapshots(self, keep: int = 2) -> None:
        """Keep the newest few; the previous one is the rollback target."""
        try:
            names = sorted(n for n in os.listdir(self._snapshot_root())
                           if n.startswith("snapshot-"))
            for stale in names[:-keep] if len(names) > keep else []:
                shutil.rmtree(os.path.join(self._snapshot_root(), stale),
                              ignore_errors=True)
        except OSError:
            pass

    def load(self) -> LoadResult:
        """Restore the pointed-to snapshot, verifying it before trusting it.

        Any inconsistency quarantines the snapshot and reports `loaded=False`
        so the caller rebuilds from the database. The old loader replaced a
        corrupt index with an EMPTY one, loaded the metadata on top, and
        returned success — leaving N entries addressing 0 vectors with nothing
        logged.
        """
        pointer = self._pointer_path()
        if not os.path.isfile(pointer):
            return LoadResult(supported=True, loaded=False, reason="no snapshot pointer")

        with open(pointer, encoding="utf-8") as handle:
            snapshot_name = handle.read().strip()
        snapshot_dir = os.path.join(self._snapshot_root(), snapshot_name)
        if not os.path.isdir(snapshot_dir):
            return LoadResult(supported=True, loaded=False,
                              reason=f"pointer references missing snapshot {snapshot_name!r}")

        index_path = os.path.join(snapshot_dir, "index.bin")
        keys_path = os.path.join(snapshot_dir, "keys.json")
        manifest_path = os.path.join(snapshot_dir, "manifest.json")

        def _quarantine(reason: str) -> LoadResult:
            logger.error("[VECTOR_INDEX] snapshot %s rejected: %s — quarantining and "
                         "rebuilding from the database", snapshot_name, reason)
            target = None
            try:
                qdir = os.path.join(self._snapshot_root(), QUARANTINE_DIR)
                os.makedirs(qdir, exist_ok=True)
                target = os.path.join(
                    qdir, f"{snapshot_name}-{datetime.utcnow():%Y%m%dT%H%M%S}")
                shutil.move(snapshot_dir, target)
            except OSError as exc:
                logger.warning("[VECTOR_INDEX] could not quarantine %s: %s",
                               snapshot_name, exc)
            return LoadResult(supported=True, loaded=False, reason=reason,
                              quarantined=target)

        for required in (index_path, keys_path, manifest_path):
            if not os.path.isfile(required):
                return _quarantine(f"missing {os.path.basename(required)}")

        try:
            with open(manifest_path, encoding="utf-8") as handle:
                manifest = json.load(handle)
            with open(keys_path, encoding="utf-8") as handle:
                raw_keys = json.load(handle)
        except (OSError, ValueError) as exc:
            return _quarantine(f"unreadable sidecar: {exc}")

        if manifest.get("format_version") != SNAPSHOT_FORMAT_VERSION:
            return _quarantine(
                f"format version {manifest.get('format_version')!r} "
                f"!= {SNAPSHOT_FORMAT_VERSION}")
        if manifest.get("index_type") != self.index_type:
            return _quarantine(f"index type {manifest.get('index_type')!r} "
                               f"!= {self.index_type!r}")
        if manifest.get("dim") != self.dim:
            return _quarantine(f"dim {manifest.get('dim')!r} != {self.dim}")

        try:
            with open(index_path, "rb") as handle:
                digest = hashlib.sha256(handle.read()).hexdigest()
        except OSError as exc:
            return _quarantine(f"unreadable index: {exc}")
        if digest != manifest.get("sha256"):
            return _quarantine("index checksum mismatch (torn or corrupt save)")

        try:
            index = self._faiss.read_index(index_path)
        except Exception as exc:
            return _quarantine(f"index failed to parse: {exc}")

        meta = {int(k): (v.get("model"), v.get("sha")) for k, v in raw_keys.items()}
        if index.ntotal != manifest.get("ntotal"):
            return _quarantine(f"ntotal {index.ntotal} != manifest "
                               f"{manifest.get('ntotal')}")
        if index.ntotal != len(meta):
            return _quarantine(f"ntotal {index.ntotal} != {len(meta)} keys "
                               "(index and sidecar disagree)")

        with self._lock:
            self._index = index
            self._meta = meta
            self._save_version = int(manifest.get("save_version", 0))
        logger.info("[VECTOR_INDEX] loaded %s (%d vectors, verified)",
                    snapshot_name, index.ntotal)
        return LoadResult(supported=True, loaded=True, count=index.ntotal)

    # -- introspection ---------------------------------------------------

    def stats(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "backend": self.name,
                "index_type": self.index_type,
                "metric": self.metric,
                "dim": self.dim,
                "count": int(self._index.ntotal),
                "keys": len(self._meta),
                "save_version": self._save_version,
                "last_rebuild_at": self._last_rebuild_at,
                "model_version": self.model_version,
                "resident_bytes": int(self._index.ntotal) * self.dim * 4,
            }

    def health(self) -> Dict[str, Any]:
        with self._lock:
            consistent = int(self._index.ntotal) == len(self._meta)
        return {
            "healthy": consistent,
            "reason": None if consistent else "index and key sidecar disagree",
            **self.stats(),
        }

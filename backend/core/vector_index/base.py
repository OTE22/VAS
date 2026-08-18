"""
The vector-index contract.
==========================

PostgreSQL is the source of truth: every face vector lives in
`identity_embeddings.embedding`. An index built over those vectors is a
**disposable acceleration layer** — deletable at any moment and fully
reconstructible from the database.

Two rules make that real, and both are encoded in this interface:

1. **The key is `identity_embeddings.id`** — the database row id, an int4 that
   fits FAISS's int64 id space losslessly. Not `identity_id` (a UUID, and one
   identity owns many embeddings, which keying by identity would collapse), and
   never a positional ordinal, a hash, or a truncated UUID. The key is stable
   across rebuilds, so an index can be thrown away and rebuilt without any row
   changing.

2. **The contract speaks embedding keys, never identities.** `search` returns
   `(key, score)`; resolving keys to people is the caller's job, done against
   the database. That is what lets the index implementation change — flat to
   HNSW to IVF — without touching a database column or an API route.

Implementations shipped today: `FlatFaissIndex` and `PgVectorIndex`. Anything
else must fail loudly at startup rather than silently degrade (see
`build_index`).
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Protocol, Sequence, Tuple, runtime_checkable

import numpy as np

# ArcFace output width. Any other length is a bug, not a variant.
EMBEDDING_DIM = 512


def usable_score(value: Any, threshold: float) -> bool:
    """True when `value` is a real number at or above `threshold`.

    THE SECOND GATE, and it is not redundant with the SQL one.

    PostgreSQL does not follow IEEE here: NaN sorts and compares as GREATER
    than every real number, so `1 - (v <=> q) >= :threshold` admits a NaN row,
    and `score <> score` — the usual NaN idiom — is FALSE. (Verified on
    PostgreSQL 15.15 / pgvector 0.8.1; there is no `isnan()` function either.)
    The queries therefore bound the value from ABOVE as well, which excludes
    NaN because `NaN <= 1.0` is false.

    Python's own comparisons already reject NaN, but a score arrives here after
    a driver conversion and an arithmetic step, and the cost of proving it is
    finite is one call. A single unusable vector otherwise contaminates every
    search result forever, which is what made this worth two gates.
    """
    try:
        score = float(value)
    except (TypeError, ValueError):
        return False
    return math.isfinite(score) and score >= threshold

# The only legal values of identity_embeddings.vector_index_sync_state.
#   pending -> synced   successful synchronization
#   pending -> failed   synchronization failed
#   failed  -> pending  retry requested
#   synced  -> pending  reconciliation found it missing/stale/mismatched
# Deletion is NOT a state: removing an embedding deletes the row and the entry.
SYNC_PENDING = "pending"
SYNC_SYNCED = "synced"
SYNC_FAILED = "failed"
SYNC_STATES = frozenset({SYNC_PENDING, SYNC_SYNCED, SYNC_FAILED})

# Which identity statuses belong in the index.
#
# PROMOTED is NOT a retired state — it is what an identity becomes the moment
# somebody names it, i.e. exactly the enrolled people recognition most needs to
# find. The live pgvector backend has always searched ('ACTIVE', 'PROMOTED');
# indexing only ACTIVE would quietly drop every named person from FAISS-mode
# search and from every rebuild. MERGED and INACTIVE stay out: their vectors
# either now live under another identity or were deliberately retired.
SEARCHABLE_STATUSES = ("ACTIVE", "PROMOTED")
# The enum is stored by NAME (uppercase) in PostgreSQL, so compare on ::text.
SEARCHABLE_STATUS_SQL = "i.status::text IN ('ACTIVE', 'PROMOTED')"


class VectorIndexError(RuntimeError):
    """Base class for index faults that callers may need to distinguish."""


class InvalidVector(VectorIndexError, ValueError):
    """Wrong dimension, non-finite values, or zero magnitude."""


class UnsupportedOperation(VectorIndexError, NotImplementedError):
    """The operation is meaningless for this implementation.

    Raised rather than returning a fake success — e.g. snapshot persistence on
    a backend whose vectors already live in the authoritative table.
    """


class UnsupportedIndexType(VectorIndexError):
    """A configured index type that this release does not implement."""


@dataclass
class LoadResult:
    """Outcome of loading a persisted snapshot."""
    supported: bool                 # False when the backend has no snapshots
    loaded: bool = False            # a usable snapshot was restored
    count: int = 0
    reason: Optional[str] = None    # why nothing was loaded, when loaded is False
    quarantined: Optional[str] = None   # path of a snapshot moved aside as bad


@dataclass
class RebuildReport:
    """Outcome of rebuilding the index from the authoritative database."""
    rebuilt: int = 0                # vectors added
    skipped_unusable: int = 0       # rows whose vector is NULL/invalid
    removed_stale: int = 0          # keys dropped (deleted/inactive rows)
    source: str = "database"        # always the database — never the old index
    detail: Dict[str, Any] = field(default_factory=dict)


def coerce_vector(value: Any) -> np.ndarray:
    """Accept every shape a stored vector legitimately arrives in.

    pgvector hands a `vector` column back as the TEXT form '[0.1,0.2,...]' when
    it is selected through raw SQL (which is how the rebuild reads it), while
    the ORM path yields a list and in-process callers pass an ndarray. Parsing
    the text form is not cosmetic: without it every database row is rejected as
    invalid and a rebuild silently produces an EMPTY index — the exact failure
    mode this whole design exists to prevent.
    """
    if isinstance(value, str):
        text = value.strip()
        if text.startswith("[") and text.endswith("]"):
            text = text[1:-1]
        if not text:
            return np.empty(0, dtype=np.float32)
        return np.fromstring(text, sep=",", dtype=np.float32)
    if isinstance(value, memoryview):
        value = bytes(value)
    return np.asarray(value, dtype=np.float32)


def validate_vector(vector: Any, *, dim: int = EMBEDDING_DIM) -> np.ndarray:
    """Return an L2-normalized float32 vector, or raise InvalidVector.

    Centralized because every historical bug here was a missing check: a
    wrong-dimension array reached FAISS and died on a bare assertion (stripped
    under `python -O`, so a mismatched buffer went to C++ instead), and a
    zero-norm vector became NaN, was added successfully, and then permanently
    occupied a slot while matching nothing.
    """
    try:
        array = coerce_vector(vector).reshape(-1)
    except (TypeError, ValueError) as exc:
        raise InvalidVector(f"vector is not numeric: {exc}") from exc
    if array.size != dim:
        raise InvalidVector(f"expected a {dim}-dimensional vector, got {array.size}")
    if not np.all(np.isfinite(array)):
        raise InvalidVector("vector contains NaN or infinite values")
    norm = float(np.linalg.norm(array))
    if norm <= 0.0 or not np.isfinite(norm):
        raise InvalidVector("vector has zero magnitude")
    # Idempotence matters at the BYTE level, not just numerically. Dividing by
    # a float64 norm and casting back can flip low bits of an already-unit
    # float32 vector, so validate(validate(x)) != validate(x) byte-wise.
    # Reconciliation checksums its own validated copy, add_many re-validates
    # and checksums THAT — for the rows whose bytes shifted, the two digests
    # never agree and reconcile re-added the same vectors on every pass,
    # forever (the eternal `refreshed_mismatched=3`). A vector that is already
    # float32 and unit-norm is returned unchanged.
    if array.dtype == np.float32 and abs(norm - 1.0) < 1e-6:
        return array
    return (array / norm).astype(np.float32)


def vector_checksum(vector: np.ndarray) -> str:
    """Short, stable digest of a normalized vector.

    Reconciliation compares this to detect an index entry whose bytes no longer
    match the database row — drift that equal counts would never reveal.
    """
    return hashlib.sha256(
        np.asarray(vector, dtype=np.float32).tobytes()).hexdigest()[:16]


@runtime_checkable
class VectorIndex(Protocol):
    """Search index over embedding vectors, keyed by `identity_embeddings.id`."""

    name: str

    def add(self, key: int, vector: np.ndarray,
            model_version: Optional[str] = None) -> None:
        """Index one vector under its database row id, stamped with its model."""

    def add_many(self, items: Sequence[Tuple[int, np.ndarray]],
                 model_version: Optional[str] = None) -> int:
        """Index many; returns how many were added."""

    def search(self, vector: np.ndarray, top_k: int = 1,
               threshold: float = 0.0) -> List[Tuple[int, float]]:
        """Nearest neighbours as `(embedding_key, similarity)`, best first.

        Similarity is cosine (inner product over normalized vectors), so higher
        is better and `threshold` is a floor.
        """

    def remove(self, keys: Sequence[int]) -> int:
        """Remove entries by key; returns how many were removed."""

    def contains(self, key: int) -> bool: ...

    def keys(self) -> "set[int]":
        """Every key currently indexed — the input to reconciliation."""

    async def rebuild_from_db(self, session) -> RebuildReport:
        """Discard everything and rebuild solely from the database vectors."""

    def save(self) -> None:
        """Persist a snapshot. May raise UnsupportedOperation."""

    def load(self) -> LoadResult:
        """Restore a snapshot. Returns supported=False when inapplicable."""

    def stats(self) -> Dict[str, Any]: ...

    def health(self) -> Dict[str, Any]: ...


# Implementations available in this release. A configured type outside this map
# fails startup — never a silent downgrade to whatever happens to work.
SUPPORTED_INDEX_TYPES = ("flat",)


def build_index(index_type: str, **kwargs) -> VectorIndex:
    """Construct the configured FAISS index, or refuse clearly.

    `hnsw`/`ivf`/`ivfpq` are deliberately NOT implemented here. The previous
    code accepted them and built them without a metric argument, so FAISS
    defaulted to L2 while the caller compared the result as a similarity — the
    threshold inverted and the matcher became an anti-matcher. Refusing is the
    honest behaviour until a real implementation exists.
    """
    requested = (index_type or "flat").strip().lower()
    if requested not in SUPPORTED_INDEX_TYPES:
        raise UnsupportedIndexType(
            f"index type {requested!r} is not implemented in this release. "
            f"Supported: {', '.join(SUPPORTED_INDEX_TYPES)}. "
            "Set KNOWN_INDEX_TYPE to a supported value — the service will not "
            "start with an unimplemented index type."
        )
    from backend.core.vector_index.flat_faiss import FlatFaissIndex

    return FlatFaissIndex(**kwargs)

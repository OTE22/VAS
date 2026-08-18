"""
pgvector adapter for the VectorIndex contract.
==============================================

Here the index and the source of truth are the same object: the vectors live in
`identity_embeddings.embedding` and Postgres searches them in place. That makes
most of the contract trivial and two parts of it meaningless.

`save()` and `load()` raise `UnsupportedOperation` / report `supported=False`
rather than pretending. A no-op `save()` that returned quietly would read as "a
snapshot exists" to every caller and to any operator reading the logs — the kind
of fake success that turns a recovery drill into a data-loss incident. There is
genuinely nothing to snapshot, and saying so is the honest interface.

Queries are async (they need a session), so the synchronous parts of the
Protocol that require I/O are the ones marked unsupported; `search_async` is the
real entry point used by IdentityService.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

import numpy as np

from backend.core.vector_index.base import (SEARCHABLE_STATUS_SQL, EMBEDDING_DIM, LoadResult, usable_score,
                                            RebuildReport, UnsupportedOperation,
                                            validate_vector)

logger = logging.getLogger(__name__)


class PgVectorIndex:
    """Adapts the pgvector store to the shared contract.

    Keys are `identity_embeddings.id`, exactly as with FAISS, so callers resolve
    results the same way regardless of backend.
    """

    name = "pgvector"
    index_type = "pgvector"
    metric = "cosine"

    def __init__(self, dim: int = EMBEDDING_DIM, model_version: Optional[str] = None):
        self.dim = dim
        self.model_version = model_version

    # -- writes are the database write itself ----------------------------

    def add(self, key: int, vector: np.ndarray,
            model_version: Optional[str] = None) -> None:
        """No-op by construction: persisting the row IS indexing it.

        Validation still runs, so an invalid vector is rejected identically on
        both backends rather than only on the FAISS path.
        """
        validate_vector(vector, dim=self.dim)

    def add_many(self, items: Sequence[Tuple[int, np.ndarray]],
                 model_version: Optional[str] = None) -> int:
        for _key, vector in items:
            validate_vector(vector, dim=self.dim)
        return len(items)

    def remove(self, keys: Sequence[int]) -> int:
        """Deleting the row deletes the index entry — the caller owns that."""
        return 0

    # -- reads -----------------------------------------------------------

    async def search_async(self, session, vector: np.ndarray, top_k: int = 1,
                           threshold: float = 0.0) -> List[Tuple[int, float]]:
        """`(embedding_key, cosine_similarity)`, best first, ACTIVE identities only."""
        from sqlalchemy import text

        query = validate_vector(vector, dim=self.dim)
        literal = "[" + ",".join(f"{float(x):.8f}" for x in query) + "]"
        rows = (await session.execute(text(
            "SELECT e.id, 1 - (e.embedding <=> CAST(:q AS vector)) AS similarity "
            "FROM identity_embeddings e "
            "JOIN identities i ON i.id = e.identity_id "
            "WHERE e.embedding IS NOT NULL AND " + SEARCHABLE_STATUS_SQL + " "
            "ORDER BY e.embedding <=> CAST(:q AS vector) "
            "LIMIT :k"), {"q": literal, "k": max(1, int(top_k))})).all()
        # usable_score, not `>= threshold`: a NaN would already have been
        # excluded by SQL, but a score that is not a real number must never
        # reach a caller whatever the database returned. See base.usable_score.
        return [(int(r[0]), float(r[1])) for r in rows
                if usable_score(r[1], threshold)]

    def search(self, vector: np.ndarray, top_k: int = 1,
               threshold: float = 0.0) -> List[Tuple[int, float]]:
        raise UnsupportedOperation(
            "pgvector search requires a database session; use search_async(session, ...)")

    def contains(self, key: int) -> bool:
        raise UnsupportedOperation(
            "pgvector membership requires a database session; query identity_embeddings")

    def keys(self) -> Set[int]:
        raise UnsupportedOperation(
            "pgvector keys require a database session; query identity_embeddings")

    async def keys_async(self, session) -> Set[int]:
        from sqlalchemy import text
        rows = (await session.execute(text(
            "SELECT e.id FROM identity_embeddings e "
            "JOIN identities i ON i.id = e.identity_id "
            "WHERE e.embedding IS NOT NULL AND " + SEARCHABLE_STATUS_SQL))).all()
        return {int(r[0]) for r in rows}

    # -- rebuild / persistence -------------------------------------------

    async def rebuild_from_db(self, session) -> RebuildReport:
        """Nothing to rebuild: the database rows already are the index."""
        count = len(await self.keys_async(session))
        return RebuildReport(rebuilt=count, source="database",
                             detail={"note": "pgvector stores vectors in place; "
                                             "no separate index to reconstruct"})

    def save(self) -> None:
        raise UnsupportedOperation(
            "pgvector has no snapshot to save — its vectors are already the "
            "authoritative rows in identity_embeddings")

    def load(self) -> LoadResult:
        return LoadResult(supported=False, loaded=False,
                          reason="pgvector reads vectors from the database; "
                                 "there is no snapshot to load")

    # -- introspection ---------------------------------------------------

    def stats(self) -> Dict[str, Any]:
        return {"backend": self.name, "index_type": self.index_type,
                "metric": self.metric, "dim": self.dim,
                "model_version": self.model_version,
                "snapshots": "not applicable"}

    def health(self) -> Dict[str, Any]:
        return {"healthy": True, "reason": None, **self.stats()}

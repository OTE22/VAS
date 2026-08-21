"""One accessor for the live index, and one way to read a stored vector.

Every module that used to reach into `backend.core.identity_index` did one of
two things: pull a vector back out of the index, or mutate the index. Both are
now answered from here, so no consumer imports an index implementation and no
consumer reconstructs a vector from a search structure.

`load_vectors` reads `identity_embeddings.embedding` — the authoritative store.
The old `unknown_index.reconstruct(faiss_id)` path was only ever necessary
because under the FAISS backend the vector existed nowhere else; it also
silently returned garbage whenever a positional `faiss_id` had shifted under a
rebuild. Reading the row cannot drift.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Iterable, List, Optional, Sequence

import numpy as np
from sqlalchemy import text

from backend.core.vector_index.base import (SEARCHABLE_STATUS_SQL, coerce_vector,
                                            usable_score)

logger = logging.getLogger(__name__)


def get_vector_index_manager():
    """The VectorIndexManager published by lifespan, or None before startup."""
    try:
        import backend.core as core_module
    except Exception:                                    # pragma: no cover
        return None
    return getattr(core_module, "vector_index_manager", None)


def get_vector_index():
    """The VectorIndex published by lifespan, or None in a process without one.

    None does NOT mean "the backend has no index". Both backends build one —
    `factory.build_backend` returns a PgVectorIndex for pgvector and a FAISS
    index for faiss — and `lifespan` publishes the manager for either. None
    means no lifespan has run in THIS process, which is the normal case for a
    script, a management command or a test; such callers fall back to querying
    PostgreSQL rather than failing.

    What actually differs between the backends is where the searchable vectors
    live. Under pgvector they live in `identity_embeddings`, so deleting the
    row removes the searchable entry and `PgVectorIndex.remove()` has nothing
    of its own to drop; there is no independently persisted index state that
    can drift from the table. Under FAISS the index is separate state, which is
    why callers deleting rows must confirm the removal (see
    `remove_embedding_keys(..., strict=True)`).
    """
    manager = get_vector_index_manager()
    if manager is not None:
        return getattr(manager, "index", None)
    try:
        import backend.core as core_module
    except Exception:                                    # pragma: no cover
        return None
    return getattr(core_module, "vector_index", None)


async def load_vectors(session, embedding_ids: Sequence[int]) -> Dict[int, np.ndarray]:
    """Read stored vectors by embedding id, straight from PostgreSQL."""
    keys = [int(k) for k in embedding_ids if k is not None]
    if not keys:
        return {}
    rows = (await session.execute(
        text("SELECT id, embedding FROM identity_embeddings "
             "WHERE id = ANY(:keys) AND embedding IS NOT NULL"),
        {"keys": keys})).all()
    out: Dict[int, np.ndarray] = {}
    for row in rows:
        vector = coerce_vector(row[1])
        if vector.size:
            out[int(row[0])] = vector
    return out


async def load_vector(session, embedding_id) -> Optional[np.ndarray]:
    """Single-row convenience wrapper around :func:`load_vectors`."""
    if embedding_id is None:
        return None
    found = await load_vectors(session, [embedding_id])
    return found.get(int(embedding_id))


async def search_similar_embeddings(session, vector: np.ndarray, *,
                                    top_k: int = 20,
                                    threshold: float = 0.0,
                                    identity_type: Optional[str] = None
                                    ) -> List[Dict[str, Any]]:
    """Nearest embeddings, resolved to identities through the database.

    Works on either backend: it prefers the in-process index when one is
    configured and otherwise queries pgvector directly. Results are always
    resolved through `identity_embeddings`, so a key with no live row cannot
    surface — the stale-vector-wins-top-k failure the old index had.
    """
    index = get_vector_index()
    hits: List[tuple] = []
    # Distinguish "the index answered, with nothing" from "the index could not
    # answer". Only the second justifies a database scan; treating an honest
    # empty result as a failure would run every miss twice.
    index_answered = False
    if index is not None and hasattr(index, "search"):
        try:
            hits = list(index.search(vector, top_k=max(int(top_k) * 5, int(top_k)),
                                     threshold=threshold))
            index_answered = True
        except Exception as exc:
            logger.warning("[VECTOR_INDEX] in-process search unavailable (%s); "
                           "falling back to the database", exc)
            hits = []

    if index_answered:
        keys = [int(k) for k, _ in hits]
        if not keys:
            return []          # the index searched and found nothing
        scores = {int(k): float(s) for k, s in hits}
        rows = (await session.execute(
            text("SELECT e.id, e.identity_id, i.type "
                 "FROM identity_embeddings e "
                 "JOIN identities i ON i.id = e.identity_id "
                 f"WHERE e.id = ANY(:keys) AND {SEARCHABLE_STATUS_SQL}"),
            {"keys": keys})).all()
        found = [{"embedding_id": int(r[0]), "identity_id": str(r[1]),
                  "identity_type": str(r[2]).lower(),
                  "similarity": scores.get(int(r[0]), 0.0)} for r in rows]
    else:
        literal = "[" + ",".join(f"{float(x):.8f}" for x in np.asarray(vector).ravel()) + "]"
        rows = (await session.execute(
            text("SELECT e.id, e.identity_id, i.type, "
                 "       1 - (e.embedding <=> CAST(:q AS vector)) AS similarity "
                 "FROM identity_embeddings e "
                 "JOIN identities i ON i.id = e.identity_id "
                 f"WHERE e.embedding IS NOT NULL AND {SEARCHABLE_STATUS_SQL} "
                 "ORDER BY e.embedding <=> CAST(:q AS vector) LIMIT :k"),
            {"q": literal, "k": max(1, int(top_k) * 5)})).all()
        found = [{"embedding_id": int(r[0]), "identity_id": str(r[1]),
                  "identity_type": str(r[2]).lower(),
                  "similarity": float(r[3])} for r in rows]

    if identity_type is not None:
        wanted = str(identity_type).lower()
        found = [f for f in found if f["identity_type"] == wanted]
    found = [f for f in found if usable_score(f["similarity"], threshold)]
    found.sort(key=lambda f: f["similarity"], reverse=True)
    return found[:int(top_k)]


async def remove_identity_vectors(session, identity_id) -> int:
    """Drop every indexed vector belonging to an identity."""
    index = get_vector_index()
    if index is None:
        return 0
    rows = (await session.execute(
        text("SELECT id FROM identity_embeddings WHERE identity_id = :i"),
        {"i": str(identity_id)})).all()
    keys = [int(r[0]) for r in rows]
    if not keys:
        return 0
    try:
        return int(index.remove(keys))
    except Exception as exc:
        logger.warning("[VECTOR_INDEX] removal failed for identity %s: %s",
                       identity_id, exc)
        return 0


async def request_snapshot(trigger: str = "route") -> Dict[str, Any]:
    """Ask the manager to snapshot now; it may legitimately decline.

    Routes used to call `identity_index.save()` directly after every mutation.
    That is what produced hundreds of redundant writes of an index nobody was
    reading, and at 100k a snapshot writes 201 MB — ~2 s on the configured
    ext4 volume, up to ~21 s on slower storage. Either way, long enough that
    two overlapping requests would fight over the same files.

    The manager takes the distributed lock and SKIPS rather than queues when a
    save is already running, so a declined snapshot is a normal outcome, not a
    failure: the index is derived state and the next autosave (or a rebuild
    from PostgreSQL) produces the identical result.
    """
    manager = get_vector_index_manager()
    if manager is None:
        return {"saved": False, "reason": "no_index_manager"}
    try:
        return await manager.save_once(trigger=trigger)
    except Exception as exc:
        logger.warning("[VECTOR_INDEX] snapshot request failed: %s", exc)
        return {"saved": False, "reason": f"error: {exc}"}


def index_stats() -> Optional[Dict[str, Any]]:
    """Stats for the active index, or None when there is no in-process index."""
    index = get_vector_index()
    if index is None:
        return None
    try:
        return index.stats()
    except Exception as exc:
        return {"error": str(exc)}


async def remove_embedding_keys(session, embedding_ids: Iterable[int],
                                *, strict: bool = False) -> int:
    """Drop specific embedding keys from the index.

    `strict=True` re-raises instead of swallowing. Callers that are about to
    DELETE the corresponding rows need it: with the failure swallowed, the rows
    disappear while the in-process index keeps their keys, and a later search
    returns a key that resolves to nothing. Under VECTOR_BACKEND=pgvector there
    is no in-process index, so this returns 0 without raising and the caller's
    DELETE is itself the removal.
    """
    index = get_vector_index()
    keys = [int(k) for k in embedding_ids if k is not None]
    if index is None or not keys:
        return 0
    try:
        return int(index.remove(keys))
    except Exception as exc:
        logger.warning("[VECTOR_INDEX] removal failed for keys %s: %s", keys, exc)
        if strict:
            raise
        return 0

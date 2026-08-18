"""
Reconciliation between PostgreSQL (truth) and the search index (derived).
========================================================================

Drift is detected by comparing **stable keys and content**, never by comparing
`COUNT(*)` against `ntotal`. Equal counts routinely hide an unequal set: one
vector missing and one stale vector present counts identically to a healthy
index, and the old size-mismatch logic saw nothing.

For every ACTIVE embedding row and every indexed key we compare:

  * the key itself            -> missing from index, or stale in index
  * `embedding_model_version` -> vectors from a different model must be re-added
  * the vector checksum       -> the indexed bytes no longer match the row
  * the identity's status     -> deleted/deactivated rows leave the index

Actions taken, expressed in the one legal state machine:

    pending -> synced    the vector was added successfully
    pending -> failed    adding it failed
    failed  -> pending   retry requested
    synced  -> pending   this pass found it missing, stale or mismatched

Deletion is not a state: a removed row simply loses its index entry.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import numpy as np

from backend.core.vector_index.base import (SEARCHABLE_STATUS_SQL, SYNC_FAILED, SYNC_PENDING,
                                            SYNC_SYNCED, InvalidVector,
                                            validate_vector, vector_checksum)

logger = logging.getLogger(__name__)


@dataclass
class ReconcileReport:
    checked: int = 0
    added_missing: int = 0          # in the DB, absent from the index
    removed_stale: int = 0          # in the index, gone/inactive in the DB
    refreshed_mismatched: int = 0   # checksum or model-version drift
    retried_failed: int = 0         # previously 'failed', re-queued
    still_failing: int = 0
    unusable_rows: int = 0          # NULL/invalid vector — reported, not guessed
    detail: Dict[str, Any] = field(default_factory=dict)

    @property
    def drift(self) -> int:
        return (self.added_missing + self.removed_stale
                + self.refreshed_mismatched)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "checked": self.checked,
            "added_missing": self.added_missing,
            "removed_stale": self.removed_stale,
            "refreshed_mismatched": self.refreshed_mismatched,
            "retried_failed": self.retried_failed,
            "still_failing": self.still_failing,
            "unusable_rows": self.unusable_rows,
            "drift": self.drift,
            **self.detail,
        }


async def reconcile(index, session, *, model_version: Optional[str] = None
                    ) -> ReconcileReport:
    """Converge the index onto the database. Returns what it changed."""
    from sqlalchemy import text

    report = ReconcileReport()

    rows = (await session.execute(text(
        "SELECT e.id, e.embedding, e.embedding_model_version, "
        "       e.vector_index_sync_state "
        "FROM identity_embeddings e "
        "JOIN identities i ON i.id = e.identity_id "
        "WHERE " + SEARCHABLE_STATUS_SQL + " "
        "ORDER BY e.id"))).all()

    expected: Dict[int, Any] = {}
    to_add: List[tuple] = []      # (key, vector, row_model_version)
    to_mark_synced: List[int] = []
    to_mark_failed: List[int] = []

    indexed_keys = index.keys()

    for row in rows:
        key, raw, row_model, state = int(row[0]), row[1], row[2], row[3]
        report.checked += 1

        if raw is None:
            # No vector to index. Honest state: it cannot be synced.
            report.unusable_rows += 1
            if state == SYNC_SYNCED:
                to_mark_failed.append(key)
            continue
        try:
            vector = validate_vector(raw)
        except (InvalidVector, TypeError, ValueError):
            report.unusable_rows += 1
            to_mark_failed.append(key)
            continue

        expected[key] = vector
        checksum = vector_checksum(vector)
        entry = index.entry_meta(key) if hasattr(index, "entry_meta") else None

        if key not in indexed_keys:
            to_add.append((key, vector, row_model))
            if state == SYNC_SYNCED:
                report.added_missing += 1      # synced -> pending -> synced
            elif state == SYNC_FAILED:
                report.retried_failed += 1     # failed  -> pending -> synced
            else:
                report.added_missing += 1
            continue

        # Present — but is it the RIGHT vector, from the right model?
        indexed_model, indexed_checksum = entry if entry else (None, None)
        if indexed_checksum != checksum or indexed_model != row_model:
            to_add.append((key, vector, row_model))
            report.refreshed_mismatched += 1
        elif state != SYNC_SYNCED:
            to_mark_synced.append(key)         # index is right, the row lied

    # Anything indexed that the database no longer vouches for.
    stale = sorted(indexed_keys - set(expected))
    if stale:
        report.removed_stale = index.remove(stale)

    if to_add:
        for key, vector, row_model in to_add:
            try:
                index.add(key, vector, model_version=row_model)
                to_mark_synced.append(key)
            except Exception as exc:
                report.still_failing += 1
                to_mark_failed.append(key)
                logger.warning("[VECTOR_INDEX] reconcile could not index "
                               "embedding %s: %s", key, exc)

    if to_mark_synced:
        await session.execute(text(
            "UPDATE identity_embeddings SET vector_index_sync_state = :s "
            "WHERE id = ANY(:ids)"), {"s": SYNC_SYNCED, "ids": to_mark_synced})
    if to_mark_failed:
        await session.execute(text(
            "UPDATE identity_embeddings SET vector_index_sync_state = :s "
            "WHERE id = ANY(:ids)"), {"s": SYNC_FAILED, "ids": to_mark_failed})
    if to_mark_synced or to_mark_failed:
        await session.commit()

    if report.drift or report.still_failing:
        logger.info("[VECTOR_INDEX] reconcile: %s", report.as_dict())
    return report


async def pending_backlog(session) -> int:
    """Rows whose vector is committed but not confirmed in the index.

    Scoped to identities the index actually covers. A merged-away or
    deactivated identity's embedding row keeps whatever state it had when its
    owner left — reconciliation will never touch it, because it is correctly
    excluded from the index. Counting it made the backlog gauge report
    permanent, unactionable work: an alert that can only be cleared by editing
    the database by hand is worse than no alert.
    """
    from sqlalchemy import text
    return int((await session.execute(text(
        "SELECT count(*) FROM identity_embeddings e "
        "JOIN identities i ON i.id = e.identity_id "
        "WHERE e.vector_index_sync_state <> :s AND " + SEARCHABLE_STATUS_SQL),
        {"s": SYNC_SYNCED})).scalar() or 0)

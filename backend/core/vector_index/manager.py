"""
Lifecycle owner for the vector index: autosave, reconciliation, metrics, audit.
==============================================================================

Two operational facts shape this module.

**Snapshots are not free.** Measured on this hardware, saving a 100k-vector
flat index writes ~201 MB. On the configured snapshot path — /app/database, a
named ext4 volume — that takes **~2.0 s** (pytest benchmark: 1.82 s; direct
measurement: 2.01 s). The same save takes up to **~21 s** when it lands on the
9p bind mount from the Windows host, or under heavy concurrent load.

The cadence is sized against the SLOW figure, not the fast one: an autosave
interval must be far longer than one save on the worst filesystem the deployment
can be pointed at, or the loop spends its life saving. The default is 15 minutes
(`VECTOR_INDEX_AUTOSAVE_INTERVAL_SECONDS`) — ~43x the worst case, ~450x the
measured case — and the floor below is enforced so a misconfiguration cannot
create a permanently-saving service.

**Overlapping saves must be skipped, not queued.** A queued duplicate is worse
than useless: by the time it runs, the state it was going to persist has
already been persisted by the run that overtook it, so it burns a full save
worth of I/O to write the same bytes again. `_saving` (in-process) and a
`DistributedLock` (cross-process) both SKIP on contention and say so.

Losing a snapshot is not a data-loss event — PostgreSQL holds every vector and
`rebuild_from_db` reconstructs the index from it. That is what makes skipping
safe.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# A full 100k save measured 2.0s on the configured ext4 volume and up to 21s on
# the 9p bind mount / under load. The cadence is checked against the WORST of the
# two: anything close to it would mean the next cycle starts as the previous
# finishes, on a deployment whose storage happens to be slow.
MEASURED_100K_SAVE_SECONDS = 2.0
WORST_CASE_100K_SAVE_SECONDS = 21.0
# A FLOOR, not a default. config.py owns the configured cadence; this is the
# point below which the cadence stops being meaningful, and it is derived from
# the measurement above rather than chosen. The DEFAULT_* constants that used
# to sit here duplicated config.py's 900/3600 and would have drifted from them
# silently — the settings object is the only place a default is declared.
MIN_AUTOSAVE_INTERVAL_SECONDS = 120.0


class VectorIndexManager:
    """Owns the index's background work and its observable state."""

    def __init__(self, selection, db_manager, settings):
        self.selection = selection
        self.index = selection.index
        self.backend = selection.backend
        self.db_manager = db_manager
        self.settings = settings

        self._saving = False           # in-process guard
        self._reconciling = False
        self._last_rebuild_ts: Optional[float] = None
        self._last_drift: int = 0
        self._skipped_saves = 0
        self._recovery_failures = 0
        self._system_user_id = None    # resolved once, see _system_actor_id

    # -- cadences ---------------------------------------------------------

    def autosave_interval(self) -> float:
        configured = float(self.settings.VECTOR_INDEX_AUTOSAVE_INTERVAL_SECONDS)
        if configured < MIN_AUTOSAVE_INTERVAL_SECONDS:
            logger.warning(
                "[VECTOR_INDEX] autosave interval %.0fs is below the %.0fs floor "
                "(a 100k snapshot takes ~%.0fs to write); using the floor",
                configured, MIN_AUTOSAVE_INTERVAL_SECONDS,
                WORST_CASE_100K_SAVE_SECONDS)
            return MIN_AUTOSAVE_INTERVAL_SECONDS
        return configured

    def reconcile_interval(self) -> float:
        return float(self.settings.VECTOR_INDEX_RECONCILE_INTERVAL_SECONDS)

    # -- startup ----------------------------------------------------------

    async def startup(self) -> Dict[str, Any]:
        """Load a verified snapshot, or rebuild from PostgreSQL.

        A rejected snapshot is never loaded partially and never silently
        replaced by an empty index: it is quarantined, counted, audited, and the
        index is reconstructed from the authoritative store.
        """
        outcome: Dict[str, Any] = {"backend": self.backend, "loaded": False,
                                   "rebuilt": 0, "quarantined": None}
        if self.selection.degraded:
            await self._audit("vector_index_fallback", success=False,
                              details={"requested": self.selection.requested,
                                       "running": self.backend,
                                       "reason": self.selection.reason})

        try:
            result = self.index.load()
        except Exception as exc:                      # noqa: BLE001
            logger.error("[VECTOR_INDEX] load raised: %s", exc, exc_info=True)
            result = None

        if result is not None and not result.supported:
            outcome["note"] = result.reason           # pgvector: nothing to load
            await self._publish_metrics()
            return outcome

        if result is not None and result.loaded:
            outcome["loaded"] = True
            outcome["count"] = result.count
            logger.info("[VECTOR_INDEX] restored %d vectors from snapshot", result.count)
        else:
            reason = getattr(result, "reason", "load failed")
            quarantined = getattr(result, "quarantined", None)
            if quarantined:
                self._recovery_failures += 1
                outcome["quarantined"] = quarantined
                self._count_recovery_failure(reason)
                await self._audit("vector_index_corruption_recovery", success=True,
                                  details={"reason": reason, "quarantined": quarantined})
            logger.warning("[VECTOR_INDEX] no usable snapshot (%s) — rebuilding "
                           "from PostgreSQL", reason)
            report = await self.rebuild("startup")
            outcome["rebuilt"] = report.get("rebuilt", 0)

        await self.reconcile_once(trigger="startup")
        return outcome

    # -- operations -------------------------------------------------------

    async def rebuild(self, trigger: str = "manual") -> Dict[str, Any]:
        """Reconstruct the index from PostgreSQL. The only recovery path."""
        async with self.db_manager.get_session() as db:
            report = await self.index.rebuild_from_db(db)
        self._last_rebuild_ts = time.time()
        payload = {"rebuilt": report.rebuilt,
                   "skipped_unusable": report.skipped_unusable,
                   "removed_stale": report.removed_stale,
                   "source": report.source, "trigger": trigger}
        await self._audit("vector_index_rebuild", success=True, details=payload)
        await self._publish_metrics()
        return payload

    async def reconcile_once(self, trigger: str = "scheduled") -> Dict[str, Any]:
        """Converge the index onto PostgreSQL, comparing keys and content."""
        if self.backend != "faiss":
            await self._publish_metrics()
            return {"skipped": "reconciliation is meaningless for pgvector"}
        if self._reconciling:
            return {"skipped": "a reconciliation is already running"}

        self._reconciling = True
        try:
            from backend.core.vector_index.reconcile import reconcile
            async with self.db_manager.get_session() as db:
                report = await reconcile(self.index, db)
            self._last_drift = report.drift
            payload = report.as_dict()
            payload["trigger"] = trigger
            if report.drift or report.still_failing:
                await self._audit("vector_index_reconcile", success=True, details=payload)
            await self._publish_metrics()
            return payload
        finally:
            self._reconciling = False

    async def save_once(self, trigger: str = "autosave") -> Dict[str, Any]:
        """Snapshot the index — SKIPPING (never queueing) a concurrent save."""
        if self.backend != "faiss":
            return {"skipped": "pgvector has no snapshot"}

        if self._saving:
            self._skipped_saves += 1
            logger.info("[VECTOR_INDEX] save already in progress — skipping this "
                        "cycle (a queued duplicate would rewrite identical bytes)")
            return {"skipped": "in-process save running"}

        lock = None
        try:
            from backend.core.distributed_lock import DistributedLock
            lock = DistributedLock("vector-index-save", ttl_seconds=600)
            if not await lock.acquire():
                self._skipped_saves += 1
                logger.info("[VECTOR_INDEX] another worker holds the save lock "
                            "(%s) — skipping", lock.holder_hint or "unknown")
                return {"skipped": "distributed lock held"}
        except Exception as exc:                      # noqa: BLE001
            logger.debug("[VECTOR_INDEX] distributed lock unavailable (%s); "
                         "relying on the in-process guard", exc)
            lock = None

        self._saving = True
        started = time.perf_counter()
        try:
            await asyncio.to_thread(self.index.save)
            elapsed = time.perf_counter() - started
            logger.info("[VECTOR_INDEX] snapshot written in %.1fs (%s)",
                        elapsed, trigger)
            return {"saved": True, "seconds": round(elapsed, 2)}
        except Exception as exc:                      # noqa: BLE001
            logger.error("[VECTOR_INDEX] snapshot failed: %s "
                         "(PostgreSQL is unaffected; the index is rebuildable)",
                         exc, exc_info=True)
            await self._audit("vector_index_save_failed", success=False,
                              details={"error": type(exc).__name__})
            return {"saved": False, "error": type(exc).__name__}
        finally:
            self._saving = False
            if lock is not None:
                try:
                    await lock.release()
                except Exception:                     # noqa: BLE001
                    pass

    async def remove_keys(self, keys, reason: str = "deleted") -> int:
        """Drop entries and record it — vector removal was previously unaudited."""
        removed = self.index.remove(list(keys))
        if removed:
            await self._audit("vector_index_removal", success=True,
                              details={"removed": removed,
                                       "keys": list(keys)[:50], "reason": reason})
            await self._publish_metrics()
        return removed

    # -- observability ----------------------------------------------------

    def _count_recovery_failure(self, reason: str) -> None:
        try:
            from backend.core import metrics
            if metrics.metrics_vector_index_recovery_failures is not None:
                label = "checksum" if "checksum" in (reason or "") else (
                    "mismatch" if "disagree" in (reason or "") else "other")
                metrics.metrics_vector_index_recovery_failures.labels(
                    backend=self.backend, reason=label).inc()
        except Exception:                             # noqa: BLE001
            pass

    async def publish_metrics(self) -> None:
        """Public entry point: /metrics refreshes these on scrape."""
        await self._publish_metrics()

    async def _publish_metrics(self) -> None:
        try:
            from backend.core import metrics
        except Exception:                             # noqa: BLE001
            return

        # Index size.
        #
        # An in-process index knows its own count. pgvector cannot answer that
        # without a session, and its stats() correctly omits `count` — so
        # reading stats() alone published a hard 0 for the backend that was
        # actually serving every search. A gauge that reports zero vectors while
        # recognition works is worse than no gauge: it reads as an outage.
        size = None
        try:
            size = self.index.stats().get("count")
        except Exception:                             # noqa: BLE001
            pass
        if size is None:
            try:
                from sqlalchemy import text as _text
                from backend.core.vector_index.base import SEARCHABLE_STATUS_SQL
                async with self.db_manager.get_session() as db:
                    size = (await db.execute(_text(
                        "SELECT COUNT(*) FROM identity_embeddings e "
                        "JOIN identities i ON i.id = e.identity_id "
                        "WHERE e.embedding IS NOT NULL AND "
                        + SEARCHABLE_STATUS_SQL))).scalar()
            except Exception:                         # noqa: BLE001
                size = 0
        size = int(size or 0)

        pending = 0
        try:
            from backend.core.vector_index.reconcile import pending_backlog
            async with self.db_manager.get_session() as db:
                pending = await pending_backlog(db)
        except Exception:                             # noqa: BLE001
            pass

        def _set(metric, value):
            if metric is not None:
                try:
                    metric.labels(backend=self.backend).set(value)
                except Exception:                     # noqa: BLE001
                    pass

        _set(metrics.metrics_vector_index_size, size)
        _set(metrics.metrics_vector_index_drift, self._last_drift)
        _set(metrics.metrics_vector_index_pending, pending)
        if self._last_rebuild_ts:
            _set(metrics.metrics_vector_index_last_rebuild, self._last_rebuild_ts)

    async def _system_actor_id(self, db) -> Optional[int]:
        """The `system` principal that machine actions are attributed to.

        `identity_audit_log.user_id` is NOT NULL and a foreign key, so an
        automated action needs a real row to point at. Migration a3b4c5d6e7f8
        creates a login-disabled `system` account for exactly this; attributing
        a rebuild to the human admin instead would be a false record.
        """
        if self._system_user_id is not None:
            return self._system_user_id
        from sqlalchemy import text as _text
        row = (await db.execute(
            _text("SELECT id FROM users WHERE username = 'system' LIMIT 1"))).first()
        if row is None:
            return None
        self._system_user_id = int(row[0])
        return self._system_user_id

    async def _audit(self, action: str, *, success: bool,
                     details: Optional[Dict[str, Any]] = None) -> None:
        """Durable record of index lifecycle events.

        Rebuilds, repairs and vector removals previously produced no audit row
        at all — only log lines, which nothing retains or queries.
        """
        try:
            from backend.utils.identity_audit import IdentityAuditLogger
            async with self.db_manager.get_session() as db:
                actor_id = await self._system_actor_id(db)
                if actor_id is None:
                    logger.warning(
                        "[VECTOR_INDEX] no 'system' principal; audit row for %s "
                        "not written. Run `alembic upgrade head`.", action)
                    return
                await IdentityAuditLogger.log_action(
                    db=db, user_id=actor_id, username="system",
                    action_type=action, action_details=details or {},
                    success=success,
                    notes=f"vector index backend={self.backend}")
                await db.commit()
        except Exception as exc:                      # noqa: BLE001
            # Warning, not debug: this used to fail silently on a NOT NULL
            # violation, so the audit trail was empty and nothing said so.
            logger.warning("[VECTOR_INDEX] could not write audit row for %s: %s",
                           action, exc)

    def status(self) -> Dict[str, Any]:
        out = {
            "backend": self.backend,
            "requested_backend": self.selection.requested,
            "degraded": self.selection.degraded,
            "degraded_reason": self.selection.reason,
            "last_drift": self._last_drift,
            "last_rebuild_ts": self._last_rebuild_ts,
            "skipped_saves": self._skipped_saves,
            "recovery_failures": self._recovery_failures,
            "autosave_interval_seconds": self.autosave_interval(),
        }
        try:
            out.update(self.index.stats())
        except Exception:                             # noqa: BLE001
            pass
        return out

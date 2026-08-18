"""
Learned-threshold store
=======================
Persistence + resolution for learned analysis thresholds
(``learned_thresholds`` table).

Lifecycle: the threshold-learning job writes CANDIDATE rows; an admin
reviews and ACTIVATES one (which retires the previously active row for the
same scope+signal — re-activating the retired row is the rollback). Nothing
learned is consumed until explicitly activated, and candidates below the
minimum sample size are refused activation.

Resolution precedence (most specific wins)::

    location-specific  ->  pipeline-specific  ->  global learned  ->  static config default

Every resolution reports WHICH row (scope, version) produced the value, so
assessments can record their threshold provenance.
"""

import logging
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, List, Optional, Tuple

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

# Signals the co-appearance analysis consumes.
SIGNAL_TIME_WINDOW = "multi_camera_time_window_minutes"
SIGNAL_DISTANCE = "multi_camera_distance_meters"

_CACHE_TTL_SECONDS = 60.0


@dataclass
class ResolvedThreshold:
    value: float
    source: str          # "location" | "pipeline" | "global" | "static_default"
    version: Optional[int]
    scope_id: str = ""
    sample_count: int = 0

    @property
    def provenance(self) -> str:
        if self.source == "static_default":
            return f"static:{self.value}"
        scope = f"{self.source}:{self.scope_id}" if self.scope_id else self.source
        return f"{scope}@v{self.version}"


class ThresholdStore:
    """Cached reader + lifecycle writer for learned thresholds."""

    def __init__(self):
        # {(scope_type, scope_id, signal): (loaded_monotonic, row-dict|None)}
        self._cache: Dict[Tuple[str, str, str], Tuple[float, Optional[dict]]] = {}

    def invalidate(self) -> None:
        self._cache.clear()

    async def _active_row(self, db: AsyncSession, scope_type: str,
                          scope_id: str, signal_name: str) -> Optional[dict]:
        key = (scope_type, scope_id, signal_name)
        cached = self._cache.get(key)
        now = time.monotonic()
        if cached and now - cached[0] < _CACHE_TTL_SECONDS:
            return cached[1]
        row = None
        try:
            from db_models import LearnedThreshold
            record = (await db.execute(
                select(LearnedThreshold)
                .where(LearnedThreshold.scope_type == scope_type,
                       LearnedThreshold.scope_id == scope_id,
                       LearnedThreshold.signal_name == signal_name,
                       LearnedThreshold.status == "active")
                .order_by(LearnedThreshold.version.desc())
                .limit(1)
            )).scalar_one_or_none()
            if record is not None:
                row = {"value": record.value, "version": record.version,
                       "sample_count": record.sample_count}
        except Exception as e:
            logger.warning("[THRESHOLD_STORE] lookup failed %s/%s/%s: %s",
                           scope_type, scope_id, signal_name, e)
        self._cache[key] = (now, row)
        return row

    async def resolve(self, db: AsyncSession, signal_name: str, *,
                      pipeline_id: Optional[str] = None,
                      location_name: Optional[str] = None,
                      static_default: float = 0.0) -> ResolvedThreshold:
        """Precedence: location -> pipeline -> global -> static default."""
        if location_name:
            row = await self._active_row(db, "location", location_name, signal_name)
            if row is not None:
                return ResolvedThreshold(row["value"], "location", row["version"],
                                         location_name, row["sample_count"])
        if pipeline_id:
            row = await self._active_row(db, "pipeline", pipeline_id, signal_name)
            if row is not None:
                return ResolvedThreshold(row["value"], "pipeline", row["version"],
                                         pipeline_id, row["sample_count"])
        row = await self._active_row(db, "global", "", signal_name)
        if row is not None:
            return ResolvedThreshold(row["value"], "global", row["version"],
                                     "", row["sample_count"])
        return ResolvedThreshold(float(static_default), "static_default", None)

    # ------------------------------------------------------------------
    # Lifecycle (candidates from the learning job; activation by an admin)
    # ------------------------------------------------------------------

    async def record_candidate(self, db: AsyncSession, *, scope_type: str,
                               scope_id: str, signal_name: str, value: float,
                               sample_count: int, extras: Optional[dict] = None):
        """Insert the next-version CANDIDATE row (never auto-activates)."""
        from db_models import LearnedThreshold
        from sqlalchemy import func as sa_func
        current_max = (await db.execute(
            select(sa_func.max(LearnedThreshold.version))
            .where(LearnedThreshold.scope_type == scope_type,
                   LearnedThreshold.scope_id == scope_id,
                   LearnedThreshold.signal_name == signal_name)
        )).scalar() or 0
        row = LearnedThreshold(
            scope_type=scope_type, scope_id=scope_id, signal_name=signal_name,
            value=float(value), sample_count=int(sample_count),
            extras=extras or {}, version=current_max + 1, status="candidate")
        db.add(row)
        await db.flush()
        return row

    async def activate(self, db: AsyncSession, threshold_id, *,
                       activated_by: str, min_samples: int) -> dict:
        """Activate a candidate (retiring the previous active row for the
        same scope+signal). Refuses candidates under the sample floor.
        Raises ValueError with a client-safe message on refusal."""
        import uuid as uuid_mod
        from db_models import LearnedThreshold
        row = (await db.execute(
            select(LearnedThreshold)
            .where(LearnedThreshold.id == uuid_mod.UUID(str(threshold_id)))
        )).scalar_one_or_none()
        if row is None:
            raise ValueError("Threshold not found")
        if row.status == "active":
            return {"id": str(row.id), "status": "active", "already_active": True}
        if row.sample_count < min_samples:
            raise ValueError(
                f"Refusing activation: {row.sample_count} samples "
                f"(minimum {min_samples}). Learn on more data first.")
        previous = (await db.execute(
            select(LearnedThreshold)
            .where(LearnedThreshold.scope_type == row.scope_type,
                   LearnedThreshold.scope_id == row.scope_id,
                   LearnedThreshold.signal_name == row.signal_name,
                   LearnedThreshold.status == "active")
        )).scalars().all()
        for prev in previous:
            prev.status = "retired"
        row.status = "active"
        row.activated_at = datetime.utcnow()
        row.activated_by = activated_by[:255]
        await db.flush()
        self.invalidate()
        return {
            "id": str(row.id), "status": "active",
            "retired": [str(p.id) for p in previous],
            "scope_type": row.scope_type, "scope_id": row.scope_id,
            "signal_name": row.signal_name, "value": row.value,
            "version": row.version,
        }

    async def list_thresholds(self, db: AsyncSession, *,
                              signal_name: Optional[str] = None,
                              status: Optional[str] = None,
                              limit: int = 200) -> List[dict]:
        from db_models import LearnedThreshold
        query = select(LearnedThreshold).order_by(
            LearnedThreshold.signal_name, LearnedThreshold.scope_type,
            LearnedThreshold.scope_id, LearnedThreshold.version.desc())
        if signal_name:
            query = query.where(LearnedThreshold.signal_name == signal_name)
        if status:
            query = query.where(LearnedThreshold.status == status)
        rows = (await db.execute(query.limit(limit))).scalars().all()
        return [{
            "id": str(r.id), "scope_type": r.scope_type, "scope_id": r.scope_id,
            "signal_name": r.signal_name, "value": r.value,
            "sample_count": r.sample_count, "version": r.version,
            "status": r.status, "extras": r.extras,
            "created_at": r.created_at.isoformat() + "Z" if r.created_at else None,
            "activated_at": r.activated_at.isoformat() + "Z" if r.activated_at else None,
            "activated_by": r.activated_by,
        } for r in rows]


# Global instance
threshold_store = ThresholdStore()

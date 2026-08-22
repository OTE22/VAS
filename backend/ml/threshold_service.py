"""
Threshold SETS for ML models — the authoritative decision cutpoints at serving
time and the exact provenance a prediction records.

One row of `ml_model_thresholds` is one SET per (model, scope, version):
`cutpoints = {elevated, unusual, highly_unusual}`. Lifecycle:

    training      → create_candidate()          status=candidate (version = max+1)
    shadow entry  → activate_for_model()        exactly one active per (model, scope)
    archive/reject/fail → retire_model_thresholds()

Inference bands from the ACTIVE set and persists its id + version label on the
prediction; there is no "current active" inference at read time, ever. The
artifact's `band_cutpoints` are training-time provenance and are checked for
equality at activation (`THRESHOLD_ARTIFACT_MISMATCH` otherwise).

Concurrency: every mutating function serialises per model with a
transaction-scoped advisory lock (`pg_advisory_xact_lock`, released with the
caller's commit/rollback — the bootstrap_admin pattern) plus `SELECT … FOR
UPDATE` on the model row. The DB constraints stay the backstop:
`uq_ml_threshold_scope_version` and `uq_ml_threshold_one_active` turn a lost
race into a loud IntegrityError, never a silent duplicate. All functions are
flush-only; the caller's transaction commits (in `registry_service.transition`
atomically with the stage change and the audit rows).

`learned_thresholds` is a DIFFERENT registry (co-appearance signal parameters
of the risk platform, `backend/core/threshold_store.py`); the two share column
vocabulary and nothing else.
"""
import logging
import uuid
from datetime import datetime
from typing import Any, Dict, Optional

from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from backend.ml.audit import ml_audit
from backend.ml.registry_service import RegistryError

logger = logging.getLogger(__name__)

CUTPOINT_KEYS = ("elevated", "unusual", "highly_unusual")
VALID_SOURCES = ("training", "manual", "recalibration")
GLOBAL_SCOPE = ("global", "")


def version_label(row) -> str:
    """Same convention as threat_assessments.threshold_version."""
    return f"{row.scope_type}:{row.scope_id or 'global'}@v{row.version}"


def _validate_cutpoints(cutpoints: Dict[str, Any]) -> Dict[str, float]:
    if not isinstance(cutpoints, dict) or any(k not in cutpoints for k in CUTPOINT_KEYS):
        raise RegistryError("THRESHOLD_CUTPOINTS_INVALID",
                            f"cutpoints must carry {list(CUTPOINT_KEYS)}")
    out = {k: float(cutpoints[k]) for k in CUTPOINT_KEYS}
    if not (out["elevated"] <= out["unusual"] <= out["highly_unusual"]):
        raise RegistryError("THRESHOLD_CUTPOINTS_INVALID",
                            "cutpoints must be non-decreasing elevated ≤ unusual ≤ highly_unusual")
    return out


def _canonical_scope(scope_type: str, scope_id: str):
    scope_type = (scope_type or "global").strip()
    scope_id = (scope_id or "").strip()
    if (scope_type == "global") != (scope_id == ""):
        raise RegistryError("THRESHOLD_SCOPE_INVALID",
                            "global scope must have an empty scope_id and non-global scopes a non-empty one")
    return scope_type, scope_id


async def _lock_model(db: AsyncSession, model_id) -> None:
    """Serialise all threshold mutations of one model: transaction-scoped
    advisory lock (released on commit/rollback) + row lock on the model."""
    from db_models import MLModel
    await db.execute(text("SELECT pg_advisory_xact_lock(hashtext(:k))"),
                     {"k": f"ml_threshold:{model_id}"})
    row = (await db.execute(
        select(MLModel.id).where(MLModel.id == model_id).with_for_update()
    )).scalar_one_or_none()
    if row is None:
        raise RegistryError("MODEL_NOT_FOUND", "model not found")


class ThresholdService:

    async def create_candidate(self, db: AsyncSession, *, model_id, cutpoints: Dict[str, Any],
                               quantiles: Optional[Dict[str, Any]] = None, sample_count: int = 0,
                               source: str = "training", expected_metrics: Optional[Dict[str, Any]] = None,
                               scope_type: str = "global", scope_id: str = "", notes: Optional[str] = None):
        """Insert the next-version CANDIDATE set for (model, scope). Flush only."""
        from db_models import MLModelThreshold
        if source not in VALID_SOURCES:
            raise RegistryError("THRESHOLD_SOURCE_INVALID", f"source must be one of {VALID_SOURCES}")
        scope_type, scope_id = _canonical_scope(scope_type, scope_id)
        cp = _validate_cutpoints(cutpoints)
        model_uuid = uuid.UUID(str(model_id))
        await _lock_model(db, model_uuid)
        next_version = (await db.execute(text(
            "SELECT COALESCE(MAX(version), 0) + 1 FROM ml_model_thresholds "
            "WHERE model_id = :m AND scope_type = :t AND scope_id = :s"),
            {"m": model_uuid, "t": scope_type, "s": scope_id})).scalar()
        row = MLModelThreshold(
            id=uuid.uuid4(), model_id=model_uuid, scope_type=scope_type, scope_id=scope_id,
            version=int(next_version), status="candidate", cutpoints=cp,
            quantiles=quantiles, source=source, expected_metrics=expected_metrics,
            sample_count=int(sample_count or 0), created_at=datetime.utcnow(), notes=notes)
        db.add(row)
        try:
            await db.flush()
        except Exception as e:  # lost race → loud, never silent
            if "uq_ml_threshold_scope_version" in str(e):
                raise RegistryError("THRESHOLD_VERSION_CONFLICT",
                                    "a concurrent writer allocated this threshold version") from e
            raise
        return row

    async def get_active(self, db: AsyncSession, *, model_id, scope_type: str = "global", scope_id: str = ""):
        from db_models import MLModelThreshold
        scope_type, scope_id = _canonical_scope(scope_type, scope_id)
        return (await db.execute(
            select(MLModelThreshold).where(
                MLModelThreshold.model_id == uuid.UUID(str(model_id)),
                MLModelThreshold.scope_type == scope_type,
                MLModelThreshold.scope_id == scope_id,
                MLModelThreshold.status == "active")
        )).scalar_one_or_none()

    async def list_for_model(self, db: AsyncSession, model_id):
        from db_models import MLModelThreshold
        rows = (await db.execute(
            select(MLModelThreshold).where(MLModelThreshold.model_id == uuid.UUID(str(model_id)))
        )).scalars().all()
        rank = {"active": 0, "candidate": 1, "retired": 2}
        return sorted(rows, key=lambda r: (rank.get(r.status, 9), -int(r.version or 0)))

    async def activate_for_model(self, db: AsyncSession, *, model_id, actor: str,
                                 actor_user_id: Optional[int] = None, reason: Optional[str] = None,
                                 artifact_cutpoints: Optional[Dict[str, Any]] = None,
                                 scope_type: str = "global", scope_id: str = ""):
        """Activate the highest-version CANDIDATE set for (model, scope),
        retiring any active one. Idempotent: if the newest set is already
        active nothing changes and no second audit row is written."""
        from db_models import MLModelThreshold
        scope_type, scope_id = _canonical_scope(scope_type, scope_id)
        model_uuid = uuid.UUID(str(model_id))
        await _lock_model(db, model_uuid)
        rows = (await db.execute(
            select(MLModelThreshold).where(
                MLModelThreshold.model_id == model_uuid,
                MLModelThreshold.scope_type == scope_type,
                MLModelThreshold.scope_id == scope_id,
            ).order_by(MLModelThreshold.version.desc())
        )).scalars().all()
        candidate = next((r for r in rows if r.status == "candidate"), None)
        active = next((r for r in rows if r.status == "active"), None)
        if candidate is None:
            if active is not None:
                return active   # concurrent activation already happened — no-op
            raise RegistryError("THRESHOLD_CANDIDATE_MISSING",
                                "no candidate threshold set exists for this model/scope")
        if active is not None and active.version > candidate.version:
            return active       # newest set already active
        if artifact_cutpoints is not None and candidate.source == "training":
            art = _validate_cutpoints(artifact_cutpoints)
            for k in CUTPOINT_KEYS:
                if abs(float(candidate.cutpoints[k]) - art[k]) > 1e-9:
                    raise RegistryError(
                        "THRESHOLD_ARTIFACT_MISMATCH",
                        f"candidate cutpoint {k}={candidate.cutpoints[k]} differs from the artifact's {art[k]}")
        now = datetime.utcnow()
        if active is not None:
            active.status = "retired"
            active.retired_at = now
            active.retired_by = (actor or "")[:255]
            await db.flush()   # partial unique is per statement
        candidate.status = "active"
        candidate.activated_at = now
        candidate.activated_by = (actor or "")[:255]
        await db.flush()
        await ml_audit(db, action="threshold_activate", actor_username=actor,
                       actor_user_id=actor_user_id, object_type="ml_model_threshold",
                       object_id=str(candidate.id),
                       before={"status": "candidate", "retired_active": str(active.id) if active else None},
                       after={"status": "active", "version": candidate.version,
                              "cutpoints": candidate.cutpoints, "label": version_label(candidate)},
                       reason=reason)
        return candidate

    async def retire_model_thresholds(self, db: AsyncSession, *, model_id, actor: str,
                                      actor_user_id: Optional[int] = None,
                                      reason: Optional[str] = None) -> list:
        """Retire every non-retired set of a model (archive/reject/fail)."""
        from db_models import MLModelThreshold
        model_uuid = uuid.UUID(str(model_id))
        await _lock_model(db, model_uuid)
        rows = (await db.execute(
            select(MLModelThreshold).where(
                MLModelThreshold.model_id == model_uuid,
                MLModelThreshold.status != "retired")
        )).scalars().all()
        now = datetime.utcnow()
        retired = []
        for r in rows:
            before = r.status
            r.status = "retired"
            r.retired_at = now
            r.retired_by = (actor or "")[:255]
            retired.append(str(r.id))
            await ml_audit(db, action="threshold_retire", actor_username=actor,
                           actor_user_id=actor_user_id, object_type="ml_model_threshold",
                           object_id=str(r.id), before={"status": before},
                           after={"status": "retired"}, reason=reason)
        await db.flush()
        return retired


def serialize_threshold(row) -> Dict[str, Any]:
    def _iso(v):
        return v.isoformat() + "Z" if v else None
    return {
        "id": str(row.id), "model_id": str(row.model_id),
        "scope_type": row.scope_type, "scope_id": row.scope_id,
        "version": row.version, "version_label": version_label(row), "status": row.status,
        "semantics": "relative anomaly bands (train-quantile cutpoints); not validated operational severity",
        "cutpoints": dict(row.cutpoints or {}), "quantiles": row.quantiles,
        "source": row.source, "expected_metrics": row.expected_metrics,
        "sample_count": row.sample_count, "notes": row.notes,
        "created_at": _iso(row.created_at), "activated_at": _iso(row.activated_at),
        "activated_by": row.activated_by, "retired_at": _iso(row.retired_at),
        "retired_by": row.retired_by,
    }


threshold_service = ThresholdService()

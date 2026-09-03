"""
Model registry lifecycle + artifact security.

Stage graph (backend/ml/constants.STAGE_TRANSITIONS) with three hard rules:
1. Never training -> production (no path exists in the graph).
2. VALIDATED -> SHADOW only through an EXPLICIT administrator approval whose
   payload persists: approver id + name, reason, timestamp, dataset version,
   evaluation report reference, artifact checksum (must match the registry
   row), feature-set version, intended shadow scope, rollback target /
   return-to-rules procedure.
3. Anomaly model types can never reach approved/production this release —
   enforced here AND by the DB CHECK constraint ck_ml_models_anomaly_shadow_cap.

Artifact security (house pattern + additions): artifacts live ONLY under
ML_ARTIFACT_DIR (realpath prefix check, traversal rejected, external paths
never accepted); sha256 verified against the registry before every load;
feature schema + dependency versions verified; size-capped; written via
tmp -> flush -> fsync -> reload-verify -> smoke test -> checksum ->
os.replace. Server paths are never serialized to clients.

Documented residual limitation: an attacker holding BOTH database-write and
artifact-directory-write access could still swap a model consistently —
identical to the pre-existing similarity-model system, now stated
explicitly.
"""

import hashlib
import logging
import math
import os
import pickle
import uuid as uuid_mod
from datetime import datetime
from typing import Any, Dict, List, Optional

from sqlalchemy import select, func as sa_func
from sqlalchemy.ext.asyncio import AsyncSession

from backend.ml.audit import ml_audit
from backend.ml.constants import ANOMALY_MODEL_TYPES, REDIS_ACTIVE_VERSION_KEY, STAGE_TRANSITIONS
from config import settings
from backend.utils.time_utils import iso_utc

logger = logging.getLogger(__name__)

REQUIRED_ARTIFACT_KEYS = {
    "algorithm", "model", "feature_names", "feature_set_version",
    "imputation_medians", "normalization", "band_cutpoints",
    "dependency_versions", "metadata", "saved_at",
}

REQUIRED_SHADOW_APPROVAL_FIELDS = (
    "approved_by_user_id", "approved_by", "reason", "dataset_version",
    "evaluation_report_ref", "artifact_checksum", "feature_set_version",
    "intended_scope", "rollback_target",
)


class RegistryError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def current_dependency_versions() -> Dict[str, str]:
    import platform
    import numpy
    import sklearn
    return {
        "python": platform.python_version(),
        "sklearn": sklearn.__version__,
        "numpy": numpy.__version__,
    }


def _artifact_root() -> str:
    return os.path.realpath(str(settings.ML_ARTIFACT_DIR))


def _assert_inside_artifact_dir(path: str) -> str:
    """Reject traversal and any location outside the approved directory."""
    real = os.path.realpath(path)
    root = _artifact_root()
    if not (real == root or real.startswith(root + os.sep)):
        raise RegistryError("ARTIFACT_PATH_INVALID",
                            "artifact path escapes the approved ML artifact directory")
    return real


def _smoke_test(payload: Dict[str, Any]) -> None:
    """One inference on a canonical vector; non-finite output = refusal."""
    import math
    import numpy as np
    names = payload["feature_names"]
    medians = payload["imputation_medians"]
    vector = np.array([[float(medians.get(name, 0.0)) for name in names]])
    score = score_with_payload(payload, vector)[0]
    if not math.isfinite(float(score)):
        raise RegistryError("SMOKE_TEST_FAILED", "artifact produced a non-finite score")


def preprocess_feature_vector(payload: Dict[str, Any], features: Dict[str, Any]):
    """The ONE train/serve preprocessing rule: present -> float(value);
    missing -> the artifact's training median for that feature. Returns
    (vector, missing_feature_names). Raises KeyError when the artifact has
    no median for a scored feature (validate_artifact refuses such artifacts
    before they are ever cached)."""
    medians = payload["imputation_medians"]
    vector, missing = [], []
    for name in payload["feature_names"]:
        if name in features and features[name] is not None:
            vector.append(float(features[name]))
        else:
            missing.append(name)
            vector.append(float(medians[name]))
    return vector, missing


def score_with_payload(payload: Dict[str, Any], matrix) -> List[float]:
    """Model-family score in [0,1], through one train/serve implementation.

    Unsupervised artifacts return relative anomaly scores. Classifier
    artifacts return relative ranking scores; their model contract explicitly
    prevents callers from presenting those values as calibrated threat
    probabilities.
    """
    import numpy as np
    algorithm = payload["algorithm"]
    if algorithm == "isolation_forest":
        raw = -payload["model"].score_samples(matrix)  # higher = more anomalous
    elif algorithm == "mad_baseline":
        params = payload["model"]  # {feature: {median, mad}}
        names = payload["feature_names"]
        z_scores = []
        for row in np.asarray(matrix):
            zs = []
            for i, name in enumerate(names):
                mad = params[name]["mad"]
                if mad <= 0:
                    continue
                zs.append(abs(row[i] - params[name]["median"]) / (1.4826 * mad))
            z_scores.append(max(zs) if zs else 0.0)
        raw = np.array(z_scores)
    elif algorithm in ("logreg", "random_forest", "gradient_boosting"):
        # Classifier output is intentionally named a rank score by the model
        # contract.  Without an independently validated calibration study it
        # must not be presented as a probability of threat.
        raw = payload["model"].predict_proba(matrix)[:, 1]
        return [float(v) for v in raw]
    else:
        raise RegistryError("UNKNOWN_ALGORITHM", f"unsupported algorithm {algorithm!r}")
    norm = payload["normalization"]
    span = max(norm["max"] - norm["min"], 1e-9)
    out = []
    for v in raw:
        value = float(v)
        if not math.isfinite(value):
            # NaN/inf must surface as a failure, never as the lowest band:
            # max(0.0, nan) == 0.0 in CPython would silently band it "normal".
            out.append(float("nan"))
            continue
        out.append(float(min(1.0, max(0.0, (value - norm["min"]) / span))))
    return out


def save_artifact(payload: Dict[str, Any], path: str) -> str:
    """tmp -> flush -> fsync -> reload-verify -> smoke test -> os.replace.
    Returns the sha256 of the final file."""
    real = _assert_inside_artifact_dir(path)
    missing = REQUIRED_ARTIFACT_KEYS - set(payload)
    if missing:
        raise RegistryError("ARTIFACT_INCOMPLETE", f"payload missing keys: {sorted(missing)}")
    os.makedirs(os.path.dirname(real), exist_ok=True)
    tmp = real + ".tmp"
    with open(tmp, "wb") as f:
        pickle.dump(payload, f)
        f.flush()
        os.fsync(f.fileno())
    with open(tmp, "rb") as f:      # reload-verify the exact bytes written
        reloaded = pickle.load(f)
    if set(reloaded) != set(payload):
        raise RegistryError("ARTIFACT_RELOAD_MISMATCH", "reload-verify key mismatch")
    _smoke_test(reloaded)
    max_mb = int(settings.ML_MAX_ARTIFACT_MB)
    if os.path.getsize(tmp) > max_mb * (1 << 20):
        os.remove(tmp)
        raise RegistryError("ARTIFACT_TOO_LARGE", f"artifact exceeds {max_mb}MB")
    os.replace(tmp, real)
    return _sha256_file(real)


def validate_artifact(path: str, *, expected_hash: str,
                      expected_feature_names: List[str],
                      expected_dependencies: Dict[str, str]) -> Dict[str, Any]:
    """Full pre-load validation — called before ANY DB transition and before
    every inference-cache load. Never loads a file whose hash differs from
    the registry."""
    real = _assert_inside_artifact_dir(path)
    if not os.path.exists(real):
        raise RegistryError("ARTIFACT_MISSING", "artifact file not found")
    actual_hash = _sha256_file(real)
    if actual_hash != expected_hash:
        raise RegistryError("ARTIFACT_HASH_MISMATCH",
                            "artifact checksum differs from the registry")
    with open(real, "rb") as f:
        payload = pickle.load(f)
    missing = REQUIRED_ARTIFACT_KEYS - set(payload)
    if missing:
        raise RegistryError("ARTIFACT_INCOMPLETE", f"missing keys: {sorted(missing)}")
    if list(payload["feature_names"]) != list(expected_feature_names):
        raise RegistryError("FEATURE_SCHEMA_MISMATCH",
                            "artifact feature schema differs from the registry")
    # Preprocessing contract: training imputed every missing feature with
    # that feature's train median, and inference must do EXACTLY the same —
    # so the artifact must carry a median for every feature it scores.
    # An artifact that does not is refused; nothing invents a 0.0.
    medians = payload.get("imputation_medians") or {}
    without_median = [n for n in payload["feature_names"] if n not in medians]
    if without_median:
        raise RegistryError("ARTIFACT_IMPUTATION_INCOMPLETE",
                            f"no training median for feature(s) {without_median[:5]}")
    for dep, expected in (expected_dependencies or {}).items():
        current = current_dependency_versions().get(dep)
        if current is None:
            continue
        if current.split(".")[:2] != str(expected).split(".")[:2]:
            raise RegistryError(
                "DEPENDENCY_MISMATCH",
                f"{dep} {current} incompatible with artifact's {expected} (major.minor)")
    _smoke_test(payload)
    return payload


def serialize_model_row(row) -> Dict[str, Any]:
    """Client payload — artifact_path deliberately omitted (path-free)."""
    def iso(dt):
        return iso_utc(dt) if dt else None
    return {
        "id": str(row.id), "model_type": row.model_type, "version": row.version,
        "stage": row.stage, "algorithm": row.algorithm,
        "model_purpose": row.model_purpose, "score_type": row.score_type,
        "is_probability": row.is_probability,
        "calibration_status": row.calibration_status,
        "artifact_name": row.artifact_name, "artifact_hash": row.artifact_hash,
        "artifact_size_bytes": row.artifact_size_bytes,
        "dependency_versions": row.dependency_versions,
        "feature_set_version": row.feature_set_version,
        "feature_names": row.feature_names,
        "dataset_id": str(row.dataset_id) if row.dataset_id else None,
        "training_job_id": row.training_job_id, "seed": row.seed,
        "hyperparameters": row.hyperparameters, "metrics": row.metrics,
        "quality_gates": row.quality_gates,
        "evaluation_report": row.evaluation_report,
        "shadow_approval": row.shadow_approval,
        "shadow_started_at": iso(row.shadow_started_at),
        "validated_at": iso(row.validated_at),
        "rejected_at": iso(row.rejected_at), "rejection_reason": row.rejection_reason,
        "archived_at": iso(row.archived_at),
        "rolled_back_at": iso(row.rolled_back_at),
        "failure_code": row.failure_code, "notes": row.notes,
        "created_at": iso(row.created_at),
        # training lineage (revision a9c4e2d7f1b3; None on older rows)
        "training_config": getattr(row, "training_config", None),
        "code_version": getattr(row, "code_version", None),
        # Registry/file agreement: a row whose artifact file is gone (e.g. a
        # container recreated without a persistent ML volume) is reported,
        # never silently served — validate_artifact refuses it as ARTIFACT_MISSING.
        "artifact_present": (os.path.exists(row.artifact_path)
                             if getattr(row, "artifact_path", None) else None),
    }


class RegistryService:

    async def next_version(self, db: AsyncSession, model_type: str) -> int:
        from db_models import MLModel
        current = (await db.execute(
            select(sa_func.max(MLModel.version)).where(MLModel.model_type == model_type)
        )).scalar() or 0
        return current + 1

    async def get_model(self, db: AsyncSession, model_id) :
        from db_models import MLModel
        try:
            row_uuid = uuid_mod.UUID(str(model_id))
        except (ValueError, TypeError):
            return None
        return (await db.execute(
            select(MLModel).where(MLModel.id == row_uuid))).scalar_one_or_none()

    async def get_stage_model(self, db: AsyncSession, model_type: str, stage: str):
        """The model currently in `stage`, or None.

        Only the SHADOW stage is unique by schema (uq_ml_models_one_shadow).
        Every training run leaves another `validated` candidate behind, and
        `archived` grows forever, so those stages legitimately hold many rows.
        This used scalar_one_or_none(), which raises on a second row — so the
        ML overview (which asks for the validated candidate) returned 500 the
        moment an administrator trained twice without approving in between.
        For the non-unique stages the answer is the NEWEST model.
        """
        from db_models import MLModel
        return (await db.execute(
            select(MLModel)
            .where(MLModel.model_type == model_type, MLModel.stage == stage)
            .order_by(MLModel.version.desc(), MLModel.created_at.desc())
            .limit(1)
        )).scalars().first()

    async def _bump_version_key(self, model_type: str) -> None:
        try:
            from backend.core.cache_manager import cache_manager
            client = getattr(cache_manager, "redis_client", None)
            if client is not None:
                await client.incr(REDIS_ACTIVE_VERSION_KEY.format(model_type=model_type))
        except Exception:
            logger.debug("[ML_OPS] version-key bump failed (cache TTL covers it)",
                         exc_info=True)

    async def transition(self, db: AsyncSession, model_id: str, *,
                         to_stage: str, actor: str,
                         actor_user_id: Optional[int] = None,
                         reason: Optional[str] = None,
                         shadow_approval: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Guarded stage transition. Raises RegistryError with a stable code."""
        from db_models import MLModel

        row = await self.get_model(db, model_id)
        if row is None:
            raise RegistryError("MODEL_NOT_FOUND", "model not found")
        allowed = STAGE_TRANSITIONS.get(row.stage, [])
        if to_stage not in allowed:
            raise RegistryError(
                "INVALID_TRANSITION",
                f"{row.stage} -> {to_stage} is not a permitted transition "
                f"(allowed: {allowed})")
        if row.model_type in ANOMALY_MODEL_TYPES and to_stage in ("approved", "production"):
            raise RegistryError(
                "ANOMALY_SHADOW_CAP",
                "anomaly models cannot pass administrator-approved SHADOW in "
                "this release")
        if to_stage == "shadow":
            from backend.ml.model_specs import get_model_spec
            if get_model_spec(row.model_type).serving_mode not in ("shadow", "on_demand_shadow"):
                raise RegistryError(
                    "SERVING_MODE_NOT_SUPPORTED",
                    f"{row.model_type} is {get_model_spec(row.model_type).serving_mode}; "
                    "it cannot enter the live shadow loop")

        now = datetime.utcnow()
        before_stage = row.stage

        if to_stage == "shadow":
            # Explicit admin approval with the full persisted payload.
            approval = dict(shadow_approval or {})
            missing = [f for f in REQUIRED_SHADOW_APPROVAL_FIELDS
                       if approval.get(f) in (None, "")]
            if missing:
                raise RegistryError(
                    "SHADOW_APPROVAL_INCOMPLETE",
                    f"shadow entry requires explicit approval fields: {missing}")
            if approval["artifact_checksum"] != row.artifact_hash:
                raise RegistryError(
                    "SHADOW_APPROVAL_CHECKSUM_MISMATCH",
                    "approval references a different artifact checksum than the registry")
            if approval["feature_set_version"] != row.feature_set_version:
                raise RegistryError(
                    "SHADOW_APPROVAL_SCHEMA_MISMATCH",
                    "approval references a different feature-set version")
            # Artifact re-validated BEFORE any DB change.
            validate_artifact(row.artifact_path,
                              expected_hash=row.artifact_hash,
                              expected_feature_names=row.feature_names,
                              expected_dependencies=row.dependency_versions)
            # One-shadow invariant: archive the current shadow first (flush
            # before promoting — the partial unique index is per statement).
            existing = await self.get_stage_model(db, row.model_type, "shadow")
            from backend.ml.threshold_service import threshold_service
            if existing is not None and existing.id != row.id:
                existing.stage = "archived"
                existing.archived_at = now
                await db.flush()
                # the displaced model's threshold set retires with it
                await threshold_service.retire_model_thresholds(
                    db, model_id=existing.id, actor=actor, actor_user_id=actor_user_id,
                    reason="displaced by a newer shadow model")
            approval["approved_at"] = now.isoformat() + "Z"
            row.shadow_approval = approval
            row.shadow_started_at = now
            # Activate the model's candidate threshold set atomically with the
            # stage change (same transaction, same commit). The artifact's
            # band_cutpoints must equal the candidate's or activation refuses.
            _payload = validate_artifact(row.artifact_path,
                                         expected_hash=row.artifact_hash,
                                         expected_feature_names=row.feature_names,
                                         expected_dependencies=row.dependency_versions)
            await threshold_service.activate_for_model(
                db, model_id=row.id, actor=actor, actor_user_id=actor_user_id,
                reason=reason or "shadow approval",
                artifact_cutpoints=_payload.get("band_cutpoints"))
        elif to_stage == "rejected":
            if not reason:
                raise RegistryError("REASON_REQUIRED", "rejection requires a reason")
            row.rejected_at = now
            row.rejected_by = actor[:255]
            row.rejection_reason = reason
        elif to_stage == "archived":
            row.archived_at = now
        elif to_stage == "validated":
            row.validated_at = now
        elif to_stage == "failed":
            row.failure_code = (reason or "unspecified")[:64]
        if to_stage in ("rejected", "archived", "failed"):
            # a model that leaves service retires its threshold sets with it
            from backend.ml.threshold_service import threshold_service
            await threshold_service.retire_model_thresholds(
                db, model_id=row.id, actor=actor, actor_user_id=actor_user_id,
                reason=reason or f"model {to_stage}")

        row.stage = to_stage
        await ml_audit(db, action=f"model_{to_stage}", actor_username=actor,
                       actor_user_id=actor_user_id, object_type="ml_model",
                       object_id=str(row.id),
                       before={"stage": before_stage},
                       after={"stage": to_stage}, reason=reason)
        await db.commit()
        await self._bump_version_key(row.model_type)
        logger.info("[ML_OPS] model %s v%s: %s -> %s by %s",
                    row.model_type, row.version, before_stage, to_stage, actor)
        try:   # state-changing event -> gauges current without any page load
            from backend.ml import metrics as ml_metrics
            await ml_metrics.refresh_state(db, reason=f"model_{to_stage}")
        except Exception:
            pass
        return serialize_model_row(row)

    async def stop_shadow(self, db: AsyncSession, model_type: str, *,
                          actor: str, actor_user_id: Optional[int] = None,
                          reason: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """Rollback path this release: archive the shadow model and return
        the system to rules-only observation. Live decisions were never
        affected, and remain unaffected."""
        row = await self.get_stage_model(db, model_type, "shadow")
        if row is None:
            return None
        return await self.transition(
            db, str(row.id), to_stage="archived", actor=actor,
            actor_user_id=actor_user_id, reason=reason or "shadow stopped (rollback)")


# Global instance
registry_service = RegistryService()

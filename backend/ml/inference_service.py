"""
Shadow inference service.

Loads ONLY the shadow-stage model (this release: anomaly models cap at
administrator-approved SHADOW; there is no production ML path). Process-
local model cache with coordinated refresh: a Redis version key bumped on
every registry transition invalidates all workers within seconds; without
Redis a short TTL re-check covers it. Every load re-validates the artifact
(hash, schema, dependencies, smoke test) — a tampered artifact is refused
and recorded, never served.

predict_identity NEVER raises: every failure path returns
ok=False + FallbackReason. Scores are behavioral anomaly signals
(score_type="anomaly_score", is_probability=false,
calibration_status="not_applicable") — never threat probabilities.
"""

import asyncio
import logging
import time
import uuid as uuid_mod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional

from sqlalchemy.ext.asyncio import AsyncSession

from backend.ml.constants import FEATURE_SET_VERSION, MODEL_TYPE_BEHAVIOR_ANOMALY, REDIS_ACTIVE_VERSION_KEY, FallbackReason
from config import settings

logger = logging.getLogger(__name__)


@dataclass
class MLPredictionResult:
    ok: bool
    failure_reason: Optional[str] = None
    model_id: Optional[str] = None
    model_version_label: str = "none"
    behavioral_anomaly_score: Optional[float] = None
    ml_anomaly_band: Optional[str] = None
    band_cutpoints: Dict[str, float] = field(default_factory=dict)
    threshold_id: Optional[str] = None          # the ACTIVE set used — exact provenance
    threshold_version: Optional[str] = None     # its version label
    snapshot_id: Optional[int] = None
    features_checksum: Optional[str] = None
    missing_features: List[str] = field(default_factory=list)
    unavailable_features: Dict[str, str] = field(default_factory=dict)
    explanation: Optional[Dict[str, Any]] = None
    as_of_timestamp: Optional[datetime] = None
    latency_ms: Optional[float] = None
    score_type: str = "anomaly_score"
    is_probability: bool = False
    calibration_status: str = "not_applicable"


class _CachedModel:
    def __init__(self, row_id, version, artifact_hash, payload, loaded_marker,
                 threshold: Optional[Dict[str, Any]] = None):
        self.row_id = row_id
        self.version = version
        self.artifact_hash = artifact_hash
        self.payload = payload
        self.loaded_marker = loaded_marker
        # The ACTIVE threshold set, loaded WITH the model and invalidated by the
        # same marker/TTL — never looked up per request, never "newest".
        self.threshold = threshold
        self.loaded_at = time.monotonic()


class InferenceService:

    def __init__(self):
        self._cache: Dict[str, Optional[_CachedModel]] = {}
        self._last_marker_check: Dict[str, float] = {}
        self._marker_seen: Dict[str, Optional[str]] = {}

    def invalidate(self) -> None:
        self._cache.clear()
        self._last_marker_check.clear()
        self._marker_seen.clear()

    async def _current_marker(self, model_type: str) -> Optional[str]:
        """Redis version marker (None when Redis is unavailable)."""
        try:
            from backend.core.cache_manager import cache_manager
            client = getattr(cache_manager, "redis_client", None)
            if client is None:
                return None
            raw = await client.get(REDIS_ACTIVE_VERSION_KEY.format(model_type=model_type))
            return raw.decode() if isinstance(raw, (bytes, bytearray)) else (
                str(raw) if raw is not None else "0")
        except Exception:
            return None

    async def _ensure_current(self, db: AsyncSession, model_type: str) -> Optional[_CachedModel]:
        """Cached shadow model. With Redis available, the version marker is
        checked on EVERY request (one sub-millisecond GET) so a registry
        transition invalidates all workers immediately — no staleness
        window. Without Redis, a bounded TTL re-check is the fallback."""
        ttl = float(settings.ML_MODEL_CACHE_TTL_SECONDS)
        now = time.monotonic()
        cached = self._cache.get(model_type)

        marker = await self._current_marker(model_type)
        if (cached is not None and marker is not None
                and marker == self._marker_seen.get(model_type)):
            return cached
        if (cached is not None and marker is None
                and now - cached.loaded_at < ttl):
            return cached

        # (Re)load from the registry — full validation before serving.
        from backend.ml.registry_service import (
            RegistryError, registry_service, validate_artifact)
        row = await registry_service.get_stage_model(db, model_type, "shadow")
        if row is None:
            self._cache[model_type] = None
            self._marker_seen[model_type] = marker
            return None
        try:
            payload = validate_artifact(
                row.artifact_path, expected_hash=row.artifact_hash,
                expected_feature_names=row.feature_names,
                expected_dependencies=row.dependency_versions)
        except RegistryError as e:
            logger.error("[ML_OPS] shadow model load REFUSED %s v%s: %s (%s)",
                         model_type, row.version, e.code, e.message)
            self._cache[model_type] = None
            self._marker_seen[model_type] = marker
            raise
        from backend.ml.threshold_service import threshold_service, version_label
        active = await threshold_service.get_active(db, model_id=row.id)
        threshold = None
        if active is not None:
            threshold = {"id": str(active.id), "version": active.version,
                         "label": version_label(active), "cutpoints": dict(active.cutpoints or {})}
        cached = _CachedModel(str(row.id), row.version, row.artifact_hash,
                              payload, marker, threshold=threshold)
        self._cache[model_type] = cached
        self._marker_seen[model_type] = marker
        logger.info("[ML_OPS] shadow model loaded %s v%s hash=%s threshold=%s",
                    model_type, row.version, row.artifact_hash[:12],
                    threshold["label"] if threshold else "NONE")
        return cached

    @staticmethod
    def _band_for(score: float, cutpoints: Dict[str, float]) -> str:
        if score >= cutpoints.get("highly_unusual", 1.1):
            return "highly_unusual"
        if score >= cutpoints.get("unusual", 1.1):
            return "unusual"
        if score >= cutpoints.get("elevated", 1.1):
            return "elevated"
        return "normal"

    @staticmethod
    def _explain(payload: Dict[str, Any], features: Dict[str, float]) -> Dict[str, Any]:
        """Native explanation: features ranked by robust deviation from the
        training medians. Honest and model-appropriate; never raises."""
        try:
            medians = payload["imputation_medians"]
            top_k = int(settings.ML_EXPLANATION_TOP_K or 5)
            deviations = []
            for name in payload["feature_names"]:
                if name not in features:
                    continue
                deviation = abs(float(features[name]) - float(medians.get(name, 0.0)))
                deviations.append((name, float(features[name]), round(deviation, 4)))
            deviations.sort(key=lambda item: item[2], reverse=True)
            return {
                "method": "median_deviation",
                "top_factors": [
                    {"feature": name, "value": value, "deviation_from_median": deviation}
                    for name, value, deviation in deviations[:top_k]
                ],
            }
        except Exception:
            return {"method": "unavailable"}

    async def predict_identity(self, db: AsyncSession, identity_id: str,
                               model_type: str = MODEL_TYPE_BEHAVIOR_ANOMALY
                               ) -> MLPredictionResult:
        """Bounded, never-raising shadow prediction."""
        started = time.monotonic()
        timeout_s = float(settings.ML_INFERENCE_TIMEOUT_MS) / 1000.0
        try:
            return await asyncio.wait_for(
                self._predict(db, identity_id, model_type, started),
                timeout=timeout_s)
        except asyncio.TimeoutError:
            # Cancellation can strike mid-query and leave the caller's
            # session with a pending invalid transaction — roll it back so
            # the fallback path (and the rest of the request) stays usable.
            await self._recover_session(db)
            return MLPredictionResult(
                ok=False, failure_reason=FallbackReason.PREDICTION_TIMEOUT.value,
                latency_ms=(time.monotonic() - started) * 1000.0)
        except Exception as e:
            logger.warning("[ML_OPS] prediction failed identity=%s: %s",
                           identity_id, e, exc_info=True)
            await self._recover_session(db)
            return MLPredictionResult(
                ok=False, failure_reason=FallbackReason.PREDICTION_FAILED.value,
                latency_ms=(time.monotonic() - started) * 1000.0)

    @staticmethod
    async def _recover_session(db: AsyncSession) -> None:
        try:
            await db.rollback()
        except Exception:
            logger.debug("[ML_OPS] session recovery rollback failed", exc_info=True)

    async def _predict(self, db: AsyncSession, identity_id: str,
                       model_type: str, started: float) -> MLPredictionResult:
        from backend.ml.registry_service import RegistryError

        try:
            cached = await self._ensure_current(db, model_type)
        except RegistryError as e:
            reason = (FallbackReason.ARTIFACT_HASH_MISMATCH.value
                      if e.code == "ARTIFACT_HASH_MISMATCH"
                      else FallbackReason.DEPENDENCY_MISMATCH.value
                      if e.code == "DEPENDENCY_MISMATCH"
                      else FallbackReason.MODEL_LOAD_FAILED.value)
            return MLPredictionResult(
                ok=False, failure_reason=reason,
                latency_ms=(time.monotonic() - started) * 1000.0)
        if cached is None:
            return MLPredictionResult(
                ok=False, failure_reason=FallbackReason.NO_APPROVED_MODEL.value,
                latency_ms=(time.monotonic() - started) * 1000.0)

        version_label = f"{model_type}-v{cached.version}"
        if cached.threshold is None:
            # No ACTIVE threshold set: the model cannot band. Recorded as an
            # explicit fallback — never inferred from a candidate/newest row.
            return MLPredictionResult(
                ok=False, failure_reason=FallbackReason.THRESHOLD_UNRESOLVED.value,
                model_id=cached.row_id, model_version_label=version_label,
                latency_ms=(time.monotonic() - started) * 1000.0)
        try:
            from backend.ml.feature_store import feature_store
            snapshot = await feature_store.compute_online_features(db, identity_id)
        except Exception as e:
            logger.warning("[ML_OPS] online feature compute failed identity=%s: %s",
                           identity_id, e)
            return MLPredictionResult(
                ok=False, failure_reason=FallbackReason.FEATURE_COMPUTATION_FAILED.value,
                model_id=cached.row_id, model_version_label=version_label,
                latency_ms=(time.monotonic() - started) * 1000.0)

        payload = cached.payload
        features = snapshot["features"]
        vector, missing = [], []
        for name in payload["feature_names"]:
            if name in features:
                vector.append(float(features[name]))
            else:
                missing.append(name)
                vector.append(float(payload["imputation_medians"].get(name, 0.0)))
        if len(missing) == len(payload["feature_names"]):
            return MLPredictionResult(
                ok=False, failure_reason=FallbackReason.MISSING_REQUIRED_FEATURES.value,
                model_id=cached.row_id, model_version_label=version_label,
                snapshot_id=snapshot["snapshot_id"],
                features_checksum=snapshot["features_checksum"],
                missing_features=missing,
                unavailable_features=snapshot["unavailable_features"],
                as_of_timestamp=snapshot["as_of_timestamp"],
                latency_ms=(time.monotonic() - started) * 1000.0)

        from backend.ml.registry_service import score_with_payload
        score = score_with_payload(payload, [vector])[0]
        # Bands come from the ACTIVE threshold set (DB), not the artifact.
        band = self._band_for(score, cached.threshold["cutpoints"])
        return MLPredictionResult(
            ok=True,
            model_id=cached.row_id,
            model_version_label=version_label,
            behavioral_anomaly_score=round(score, 6),
            ml_anomaly_band=band,
            band_cutpoints=dict(cached.threshold["cutpoints"]),
            threshold_id=cached.threshold["id"],
            threshold_version=cached.threshold["label"],
            snapshot_id=snapshot["snapshot_id"],
            features_checksum=snapshot["features_checksum"],
            missing_features=missing,
            unavailable_features=snapshot["unavailable_features"],
            explanation=self._explain(payload, features),
            as_of_timestamp=snapshot["as_of_timestamp"],
            latency_ms=(time.monotonic() - started) * 1000.0)

    async def persist_prediction(self, db: AsyncSession, result: MLPredictionResult, *,
                                 identity_id: str, requested_mode: str,
                                 actual_mode_used: str,
                                 fallback_reason: Optional[str] = None,
                                 assessment_id: Optional[str] = None,
                                 model_type: str = MODEL_TYPE_BEHAVIOR_ANOMALY,
                                 event_time: Optional[datetime] = None
                                 ) -> Optional[str]:
        """Idempotent prediction persistence (snapshot reference, NOT the
        full vector — full_features only under the explicitly-enabled,
        privacy-justified sampling knob, default 0.0).

        Lineage invariant: a SUCCESSFUL shadow row (actual_mode_used='shadow',
        fallback_reason NULL) always carries a resolving model_id, threshold_id
        and threshold_version. If the model or threshold row vanished between
        inference and persistence, the row is written as an EXPLICIT failure
        (THRESHOLD_UNRESOLVED / MODEL_UNAVAILABLE) — never as a lineage-less
        success, never with a threshold reconstructed from the current active
        set. `event_time` is the assessment's source timestamp (never
        fabricated: NULL stays NULL); `as_of_timestamp` is the feature as-of."""
        from sqlalchemy.dialects.postgresql import insert as pg_insert
        from sqlalchemy import select
        from db_models import MLPrediction

        window_minutes = max(1, int(settings.ASSESSMENT_DEDUP_WINDOW_MINUTES))
        now = datetime.utcnow()
        bucket = int(now.timestamp() // (window_minutes * 60))
        key = (f"identity:{identity_id}:{result.model_version_label}:"
               f"{requested_mode}:{bucket}")
        # Failure rows carry their reason in the key: two DIFFERENT failure
        # causes in one window are two facts, not duplicates (a success and
        # its retries still dedup onto one row).
        effective_reason = fallback_reason or result.failure_reason
        if effective_reason:
            key += f":{effective_reason}"

        full_features = None
        sample_rate = float(settings.ML_FEATURE_SAMPLED_FULL_VECTOR_RATE or 0.0)
        if sample_rate > 0.0 and result.snapshot_id is not None:
            # Deterministic sampling (no RNG): hash of the idempotency key.
            import hashlib
            digest = int(hashlib.sha256(key.encode()).hexdigest()[:8], 16) / 0xFFFFFFFF
            if digest < sample_rate:
                from db_models import MLFeatureSnapshot
                snapshot_row = (await db.execute(
                    select(MLFeatureSnapshot)
                    .where(MLFeatureSnapshot.id == result.snapshot_id)
                )).scalar_one_or_none()
                if snapshot_row is not None:
                    full_features = dict(snapshot_row.features or {})

        person_uuid = None
        try:
            person_uuid = uuid_mod.UUID(str(identity_id))
        except (ValueError, TypeError):
            pass
        assessment_uuid = None
        if assessment_id:
            try:
                assessment_uuid = uuid_mod.UUID(str(assessment_id))
            except (ValueError, TypeError):
                pass

        threshold_uuid = None
        if result.threshold_id:
            try:
                threshold_uuid = uuid_mod.UUID(str(result.threshold_id))
            except (ValueError, TypeError):
                threshold_uuid = None

        row_id = uuid_mod.uuid4()
        values: Dict[str, Any] = dict(
            id=row_id,
            subject_type="identity",
            subject_id=str(identity_id),
            person_id=person_uuid,
            model_id=uuid_mod.UUID(result.model_id) if result.model_id else None,
            threshold_id=threshold_uuid,
            threshold_version=result.threshold_version,
            model_type=model_type,
            model_version_label=result.model_version_label,
            model_purpose="behavioral_anomaly_detection",
            requested_mode=requested_mode,
            actual_mode_used=actual_mode_used,
            fallback_reason=fallback_reason or result.failure_reason,
            snapshot_id=result.snapshot_id,
            feature_set_version=FEATURE_SET_VERSION,
            features_checksum=result.features_checksum,
            behavioral_anomaly_score=result.behavioral_anomaly_score,
            normalized_anomaly_score=result.behavioral_anomaly_score,
            ml_anomaly_band=result.ml_anomaly_band,
            score_type=result.score_type,
            is_probability=result.is_probability,
            calibration_status=result.calibration_status,
            assessment_id=assessment_uuid,
            event_time=event_time,
            as_of_timestamp=result.as_of_timestamp or now,
            latency_ms=result.latency_ms,
            idempotency_key=key,
            created_at=now,
        )
        # JSONB columns: OMIT when empty so the database stores SQL NULL,
        # never a JSON 'null' literal (which reads as a present value).
        if result.missing_features:
            values["missing_features"] = result.missing_features
        if result.unavailable_features:
            values["unavailable_features"] = result.unavailable_features
        if result.explanation:
            values["explanation"] = result.explanation
        if full_features:
            values["full_features"] = full_features

        async def _insert() -> Optional[bool]:
            stmt = pg_insert(MLPrediction).values(**values).on_conflict_do_nothing(
                index_elements=["idempotency_key"])
            insert_result = await db.execute(stmt)
            await db.commit()
            return bool(insert_result.rowcount)

        try:
            inserted = await _insert()
        except Exception as e:
            # A model or threshold row deleted between caching and persistence
            # (stale process cache) violates an FK. A successful prediction
            # whose provenance no longer resolves is NOT persisted as success:
            # it becomes an explicit failure row (its own idempotency key), the
            # cache is invalidated so the next request reloads honestly, and
            # the exception is surfaced to the caller's log.
            await db.rollback()
            msg = str(e)
            vanished = None
            if "threshold_id" in msg and values.get("threshold_id") is not None:
                vanished = FallbackReason.THRESHOLD_UNRESOLVED.value
            elif "model_id" in msg and values.get("model_id") is not None:
                vanished = FallbackReason.MODEL_UNAVAILABLE.value
            if vanished is None or effective_reason:
                raise
            logger.warning("[ML_OPS] provenance row vanished under cached prediction "
                           "(model_id=%s threshold_id=%s) — recording an explicit %s failure, "
                           "not a lineage-less success", values.get("model_id"),
                           values.get("threshold_id"), vanished)
            self.invalidate()
            values.update(
                model_id=None, threshold_id=None, threshold_version=None,
                actual_mode_used="rules", fallback_reason=vanished,
                behavioral_anomaly_score=None, normalized_anomaly_score=None,
                ml_anomaly_band=None, explanation=None,
                idempotency_key=f"{key}:{vanished}")
            values.pop("full_features", None)
            inserted = await _insert()
            key = values["idempotency_key"]

        if inserted:
            return str(row_id)
        existing = (await db.execute(
            select(MLPrediction.id).where(MLPrediction.idempotency_key == key)
        )).scalar_one_or_none()
        return str(existing) if existing else None


# Global instance
inference_service = InferenceService()

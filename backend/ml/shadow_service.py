"""
Shadow evaluation — never touches the live decision.

Runs in its OWN database session, bounded by ML_SHADOW_TIMEOUT_MS, and
swallows every failure (failed attempts are persisted as data: ml_failed
rows with reasons). The comparison record is strictly DESCRIPTIVE per the
approval corrections:

- rule_threat_score / rule_threat_severity  (heuristic threat concepts)
- behavioral_anomaly_score / ml_anomaly_band (behavioral anomaly concepts)
- rule_would_alert / ml_would_flag_anomaly / operational_disagreement
  (review-signal crossings: both_flagged | rules_only | anomaly_only | neither)

NO raw score difference between the two systems is computed or stored —
they measure different things.
"""

import asyncio
import logging
import uuid as uuid_mod
from datetime import datetime
from typing import Any, Dict, Optional

from backend.ml.constants import MODEL_TYPE_BEHAVIOR_ANOMALY
from config import settings

logger = logging.getLogger(__name__)

# Review-signal bars (descriptive comparison, not decisions):
RULES_ALERT_SEVERITIES = ("high", "critical")
ML_FLAG_BANDS = ("unusual", "highly_unusual")


def operational_disagreement(rule_would_alert: bool,
                             ml_would_flag: Optional[bool]) -> str:
    if ml_would_flag is None:
        return "neither" if not rule_would_alert else "rules_only"
    if rule_would_alert and ml_would_flag:
        return "both_flagged"
    if rule_would_alert:
        return "rules_only"
    if ml_would_flag:
        return "anomaly_only"
    return "neither"


class ShadowService:

    async def run_shadow(self, *, identity_id: str,
                         rule_score: float, rule_severity: str,
                         assessment_id: Optional[str] = None,
                         event_time=None) -> None:
        """Bounded, swallow-all shadow pass (fresh session; the caller's
        request session and response are never touched). `event_time` is the
        assessment's source timestamp — persisted on the prediction as-is."""
        timeout_s = float(settings.ML_SHADOW_TIMEOUT_MS) / 1000.0
        try:
            await asyncio.wait_for(
                self._run(identity_id=identity_id, rule_score=rule_score,
                          rule_severity=rule_severity, assessment_id=assessment_id,
                          event_time=event_time),
                timeout=timeout_s)
        except asyncio.TimeoutError:
            logger.warning("[ML_OPS] shadow timed out identity=%s (live result unaffected)",
                           identity_id)
            await self._record_failure(identity_id, rule_score, rule_severity,
                                       assessment_id, "PREDICTION_TIMEOUT", event_time=event_time)
        except Exception as e:
            logger.warning("[ML_OPS] shadow failed identity=%s: %s (live result unaffected)",
                           identity_id, e, exc_info=True)
            await self._record_failure(identity_id, rule_score, rule_severity,
                                       assessment_id, "SHADOW_INTERNAL_ERROR", event_time=event_time)

    async def _run(self, *, identity_id: str, rule_score: float,
                   rule_severity: str, assessment_id: Optional[str],
                   event_time=None) -> None:
        from db_connection import db_manager
        from backend.ml.inference_service import inference_service

        async with db_manager.get_session() as db:
            result = await inference_service.predict_identity(db, identity_id)
            prediction_id = await inference_service.persist_prediction(
                db, result, identity_id=identity_id,
                requested_mode="shadow",
                actual_mode_used="shadow" if result.ok else "rules",
                assessment_id=assessment_id, event_time=event_time)
            await self._persist_comparison(
                db, identity_id=identity_id, rule_score=rule_score,
                rule_severity=rule_severity, assessment_id=assessment_id,
                prediction_id=prediction_id, result=result)
            if assessment_id and prediction_id:
                await self._link_assessment(db, assessment_id, prediction_id)

    async def _persist_comparison(self, db, *, identity_id: str, rule_score: float,
                                  rule_severity: str, assessment_id: Optional[str],
                                  prediction_id: Optional[str], result) -> None:
        from db_models import MLShadowComparison
        if prediction_id is None:
            return
        rule_would_alert = rule_severity in RULES_ALERT_SEVERITIES
        ml_would_flag = (result.ml_anomaly_band in ML_FLAG_BANDS) if result.ok else None

        def _row(model_id):
            return MLShadowComparison(
                id=uuid_mod.uuid4(),
                prediction_id=uuid_mod.UUID(prediction_id),
                model_id=model_id,
                assessment_id=(uuid_mod.UUID(str(assessment_id)) if assessment_id else None),
                subject_id=str(identity_id),
                rule_threat_score=float(rule_score),
                rule_threat_severity=str(rule_severity),
                behavioral_anomaly_score=result.behavioral_anomaly_score,
                ml_anomaly_band=result.ml_anomaly_band,
                rule_would_alert=rule_would_alert,
                ml_would_flag_anomaly=bool(ml_would_flag),
                operational_disagreement=operational_disagreement(
                    rule_would_alert, ml_would_flag),
                ml_failed=not result.ok,
                failure_reason=result.failure_reason,
                ml_latency_ms=result.latency_ms,
                missing_features=result.missing_features or None,
                created_at=datetime.utcnow(),
            )

        model_uuid = uuid_mod.UUID(result.model_id) if result.model_id else None
        try:
            db.add(_row(model_uuid))
            await db.commit()
        except Exception as e:
            # Model row deleted under a stale cache: keep the comparison
            # (provenance survives in the prediction's version label) with
            # the FK detached rather than losing the record.
            await db.rollback()
            if "model_id" in str(e) and model_uuid is not None:
                logger.warning("[ML_OPS] comparison model row vanished — "
                               "persisting with detached provenance")
                db.add(_row(None))
                await db.commit()
            else:
                raise

    async def _record_failure(self, identity_id: str, rule_score: float,
                              rule_severity: str, assessment_id: Optional[str],
                              reason: str, event_time=None) -> None:
        """Even a hard shadow failure becomes a row — failures are data."""
        try:
            from db_connection import db_manager
            from backend.ml.inference_service import (
                MLPredictionResult, inference_service)
            async with db_manager.get_session() as db:
                result = MLPredictionResult(ok=False, failure_reason=reason)
                prediction_id = await inference_service.persist_prediction(
                    db, result, identity_id=identity_id,
                    requested_mode="shadow", actual_mode_used="rules",
                    assessment_id=assessment_id, event_time=event_time)
                await self._persist_comparison(
                    db, identity_id=identity_id, rule_score=rule_score,
                    rule_severity=rule_severity, assessment_id=assessment_id,
                    prediction_id=prediction_id, result=result)
        except Exception:
            logger.debug("[ML_OPS] shadow failure recording failed", exc_info=True)

    async def _link_assessment(self, db, assessment_id: str, prediction_id: str) -> None:
        try:
            from sqlalchemy import update
            from db_models import ThreatAssessmentRecord
            await db.execute(
                update(ThreatAssessmentRecord)
                .where(ThreatAssessmentRecord.id == uuid_mod.UUID(str(assessment_id)))
                .values(ml_prediction_id=uuid_mod.UUID(prediction_id)))
            await db.commit()
        except Exception:
            logger.debug("[ML_OPS] assessment link failed", exc_info=True)

    async def shadow_summary(self, db, *, days: int = 7) -> Dict[str, Any]:
        """DESCRIPTIVE aggregates only: review-signal crossings, band
        distribution by rule severity, latency, failures. No score diffs —
        the two values are different concepts."""
        from sqlalchemy import select, func as sa_func, text as sa_text
        from db_models import MLShadowComparison
        from datetime import timedelta

        floor = datetime.utcnow() - timedelta(days=days)
        rows = (await db.execute(
            select(MLShadowComparison)
            .where(MLShadowComparison.created_at >= floor)
            .order_by(MLShadowComparison.created_at.desc())
            .limit(5000))).scalars().all()
        if not rows:
            return {"window_days": days, "comparisons": 0,
                    "insufficient_data": True,
                    "note": "no shadow comparisons in the window"}

        disagreement_counts: Dict[str, int] = {}
        band_by_severity: Dict[str, Dict[str, int]] = {}
        latencies = []
        failures = 0
        for row in rows:
            disagreement_counts[row.operational_disagreement] = (
                disagreement_counts.get(row.operational_disagreement, 0) + 1)
            if row.ml_failed:
                failures += 1
            if row.ml_latency_ms is not None:
                latencies.append(row.ml_latency_ms)
            if row.ml_anomaly_band:
                band_by_severity.setdefault(row.rule_threat_severity, {})
                band_by_severity[row.rule_threat_severity][row.ml_anomaly_band] = (
                    band_by_severity[row.rule_threat_severity].get(row.ml_anomaly_band, 0) + 1)
        latencies.sort()
        return {
            "window_days": days,
            "comparisons": len(rows),
            "note": ("rule threat severity and anomaly bands are DIFFERENT "
                     "CONCEPTS shown side by side; no score differences are "
                     "computed"),
            "operational_disagreement": disagreement_counts,
            "band_distribution_by_rule_severity": band_by_severity,
            "shadow_failure_count": failures,
            "shadow_failure_rate": round(failures / len(rows), 4),
            "latency_ms_p50": latencies[len(latencies) // 2] if latencies else None,
            "latency_ms_p95": latencies[int(len(latencies) * 0.95)] if latencies else None,
        }


# Global instance
shadow_service = ShadowService()


# ---------------------------------------------------------------------------
# Observation read-back — ONE serializer for live responses and history
# ---------------------------------------------------------------------------

SIGNAL_NAME = "behavioral_anomaly"
SCORE_SEMANTICS = "anomaly_score_not_probability"
# Persisted vocabulary is pinned (constants.DISAGREEMENT_VALUES / DB comment);
# the presentation name says which MECHANISM would have raised attention.
RELATION_PRESENTATION = {"both_flagged": "both_flagged", "rules_only": "rules_only",
                         "anomaly_only": "ml_only", "neither": "neither"}


def serialize_observation(prediction, comparison=None, *, role: str = "observational") -> Dict[str, Any]:
    """The ML observation linked to an assessment: what the model said (or why
    it could not), separate from the rules result it sat next to. Success and
    failure are different states: a failed inference carries a reason and no
    score/band — nothing is required of it."""
    failed = bool(getattr(comparison, "ml_failed", False)) if comparison is not None \
        else (prediction.fallback_reason is not None)
    out: Dict[str, Any] = {
        "executed": True,
        "role": role,                                   # observational | anomaly_signal
        "applied_to_live_result": role == "anomaly_signal",
        "ml_failed": failed,
        "failure_reason": (getattr(comparison, "failure_reason", None) if comparison is not None
                           else prediction.fallback_reason) if failed else None,
        "prediction_id": str(prediction.id),
        "model_id": str(prediction.model_id) if prediction.model_id else None,
        "model_version": prediction.model_version_label,
        "model_type": prediction.model_type,
        "feature_set_version": prediction.feature_set_version,
        "threshold_version": prediction.threshold_version,
        "signal_name": SIGNAL_NAME,
        "score_semantics": SCORE_SEMANTICS,
        "band_semantics": "relative_anomaly_band_from_train_quantiles_not_validated_severity",
        "score": None if failed else prediction.behavioral_anomaly_score,
        "band": None if failed else prediction.ml_anomaly_band,
        "latency_ms": prediction.latency_ms,
        "missing_features": prediction.missing_features or [],
        "as_of": prediction.as_of_timestamp.isoformat() + "Z" if prediction.as_of_timestamp else None,
    }
    if comparison is not None:
        persisted = comparison.operational_disagreement
        out.update({
            "ml_would_flag": None if failed else bool(comparison.ml_would_flag_anomaly),
            "rules_would_alert": bool(comparison.rule_would_alert),
            "outcome_relation": RELATION_PRESENTATION.get(persisted, persisted),
            "outcome_relation_persisted": persisted,
        })
    else:
        out.update({"ml_would_flag": (None if failed else
                                      (prediction.ml_anomaly_band in ML_FLAG_BANDS)),
                    "rules_would_alert": None, "outcome_relation": None,
                    "outcome_relation_persisted": None})
    return out


async def load_observation(db, assessment_id: Optional[str]) -> Optional[Dict[str, Any]]:
    """The observation linked to an assessment through its prediction row
    (assessment.ml_prediction_id, or the prediction that names the
    assessment). None when nothing is linked (rules mode, gated modes)."""
    if not assessment_id:
        return None
    import uuid as uuid_mod
    from sqlalchemy import select
    from db_models import MLPrediction, MLShadowComparison, ThreatAssessmentRecord
    try:
        aid = uuid_mod.UUID(str(assessment_id))
    except (ValueError, TypeError):
        return None
    record = (await db.execute(
        select(ThreatAssessmentRecord).where(ThreatAssessmentRecord.id == aid))).scalar_one_or_none()
    prediction = None
    if record is not None and record.ml_prediction_id:
        prediction = (await db.execute(
            select(MLPrediction).where(MLPrediction.id == record.ml_prediction_id))).scalar_one_or_none()
    if prediction is None:
        prediction = (await db.execute(
            select(MLPrediction).where(MLPrediction.assessment_id == aid)
            .order_by(MLPrediction.created_at.desc()).limit(1))).scalars().first()
    if prediction is None:
        return None
    comparison = (await db.execute(
        select(MLShadowComparison).where(MLShadowComparison.prediction_id == prediction.id)
        .order_by(MLShadowComparison.created_at.desc()).limit(1))).scalars().first()
    role = "anomaly_signal" if (record is not None and getattr(record, "anomaly_signal_source", None) == "ml") \
        else "observational"
    return serialize_observation(prediction, comparison, role=role)

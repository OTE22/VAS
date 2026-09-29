"""
Drift monitoring — report-only, honest about insufficiency.

Data drift: per-feature PSI / KS / JS between a baseline window and the
current window of feature snapshots. Prediction drift: anomaly-score
distribution, prediction volume, shadow failure rate, operational
disagreement mix, latency, fallback rate, pipeline mix.

Every report EMBEDS its baseline (period + stats + sample count) so it is
self-describing. Below ML_DRIFT_MIN_SAMPLES the report says
insufficient_data=true instead of pretending NORMAL. Severity comes from
PSI bands (warning/critical) — and drift NEVER triggers deployment or
retraining; one weak signal is a report line, nothing more.
"""

import logging
import math
import uuid as uuid_mod
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from config import settings
from backend.utils.time_utils import iso_utc

logger = logging.getLogger(__name__)

DRIFT_BINS = 10
DRIFT_SAMPLE_LIMIT = 20000


def _finite_number(value):
    return (not isinstance(value, bool) and isinstance(value, (int, float))
            and math.isfinite(value))


def psi(baseline: List[float], current: List[float], bins: int = DRIFT_BINS) -> Optional[float]:
    """Population Stability Index over shared bin edges. None when either
    side is degenerate (honesty over fabrication)."""
    if len(baseline) < 2 or len(current) < 2:
        return None
    lo = min(min(baseline), min(current))
    hi = max(max(baseline), max(current))
    if hi <= lo:
        return 0.0
    edges = [lo + (hi - lo) * i / bins for i in range(bins + 1)]

    def hist(values):
        counts = [0] * bins
        for v in values:
            idx = min(bins - 1, max(0, int((v - lo) / (hi - lo) * bins)))
            counts[idx] += 1
        total = len(values)
        return [max(c / total, 1e-6) for c in counts]

    b, c = hist(baseline), hist(current)
    return round(sum((ci - bi) * math.log(ci / bi) for bi, ci in zip(b, c)), 6)


def ks_statistic(baseline: List[float], current: List[float]) -> Optional[Dict[str, float]]:
    if len(baseline) < 2 or len(current) < 2:
        return None
    try:
        from scipy import stats
        stat, p_value = stats.ks_2samp(baseline, current)
        return {"statistic": round(float(stat), 6), "p_value": round(float(p_value), 6)}
    except Exception:
        return None


def js_divergence(baseline: List[float], current: List[float], bins: int = DRIFT_BINS) -> Optional[float]:
    if len(baseline) < 2 or len(current) < 2:
        return None
    lo = min(min(baseline), min(current))
    hi = max(max(baseline), max(current))
    if hi <= lo:
        return 0.0

    def hist(values):
        counts = [0] * bins
        for v in values:
            idx = min(bins - 1, max(0, int((v - lo) / (hi - lo) * bins)))
            counts[idx] += 1
        total = len(values)
        return [max(c / total, 1e-9) for c in counts]

    p, q = hist(baseline), hist(current)
    m = [(pi + qi) / 2 for pi, qi in zip(p, q)]

    def kl(a, b):
        return sum(ai * math.log(ai / bi) for ai, bi in zip(a, b))

    return round(0.5 * kl(p, m) + 0.5 * kl(q, m), 6)


def _severity_for_psi(worst_psi: Optional[float]) -> str:
    if worst_psi is None:
        return "normal"
    critical = float(settings.ML_DRIFT_PSI_CRITICAL)
    warning = float(settings.ML_DRIFT_PSI_WARNING)
    if worst_psi >= critical:
        return "critical"
    if worst_psi >= warning:
        return "warning"
    return "normal"


class DriftService:

    async def _model(self, db, model_id):
        from db_models import MLModel
        from backend.ml.model_specs import get_model_spec
        model = await db.get(MLModel, uuid_mod.UUID(str(model_id)))
        if model is None:
            raise ValueError("Drift monitoring requires an existing model")
        spec = get_model_spec(model.model_type)
        if not model.feature_set_version or not model.feature_names:
            raise ValueError("Drift monitoring requires a recorded feature contract")
        return model, spec

    async def _snapshot_features(self, db: AsyncSession, start: datetime,
                                 end: datetime, *, model, spec):
        from db_models import MLFeatureSnapshot
        rows = (await db.execute(
            select(MLFeatureSnapshot.features)
            .where(MLFeatureSnapshot.entity_type == spec.entity_type,
                   MLFeatureSnapshot.feature_set_version == model.feature_set_version,
                   MLFeatureSnapshot.computed_at >= start,
                   MLFeatureSnapshot.computed_at < end)
            .order_by(MLFeatureSnapshot.computed_at.desc(), MLFeatureSnapshot.id.desc())
            .limit(DRIFT_SAMPLE_LIMIT + 1))).scalars().all()
        truncated = len(rows) > DRIFT_SAMPLE_LIMIT
        rows = rows[:DRIFT_SAMPLE_LIMIT]
        by_feature = {name: [] for name in model.feature_names}
        invalid = {name: 0 for name in model.feature_names}
        for features in rows:
            for name in by_feature:
                value = (features or {}).get(name)
                if _finite_number(value):
                    by_feature[name].append(float(value))
                elif value is not None:
                    invalid[name] += 1
        return by_feature, len(rows), truncated, invalid

    async def run_data_drift(self, db: AsyncSession, *, model_id, window_days: int = 7,
                             baseline_days: int = 30,
                             job_id: Optional[str] = None) -> Dict[str, Any]:
        """Feature-distribution drift: baseline window vs current window, for
        ONE model (the shadow model of its type at computation time)."""
        from db_models import MLDriftReport

        now = datetime.utcnow()
        window_start = now - timedelta(days=window_days)
        baseline_start = window_start - timedelta(days=baseline_days)
        model, spec = await self._model(db, model_id)
        current, current_n, current_cap, current_invalid = await self._snapshot_features(
            db, window_start, now, model=model, spec=spec)
        baseline, baseline_n, baseline_cap, baseline_invalid = await self._snapshot_features(
            db, baseline_start, window_start, model=model, spec=spec)

        min_samples = max(2, int(settings.ML_DRIFT_MIN_SAMPLES))
        insufficient = current_n < min_samples or baseline_n < min_samples or current_cap or baseline_cap
        metrics: Dict[str, Any] = {}
        worst_psi = None
        for name in sorted(baseline):
            bn, cn = len(baseline[name]), len(current[name])
            enough = bn >= min_samples and cn >= min_samples
            invalid_values = baseline_invalid[name] + current_invalid[name] > 0
            insufficient = insufficient or not enough or invalid_values
            feature_psi = psi(baseline[name], current[name]) if enough else None
            metrics[name] = {
                "psi": feature_psi,
                "ks": ks_statistic(baseline[name], current[name]) if enough else None,
                "js_divergence": js_divergence(baseline[name], current[name]) if enough else None,
                "baseline_n": bn, "current_n": cn,
                "insufficient_data": not enough or invalid_values,
                "baseline_missing_rate": round((baseline_n - bn - baseline_invalid[name]) / baseline_n, 6) if baseline_n else None,
                "current_missing_rate": round((current_n - cn - current_invalid[name]) / current_n, 6) if current_n else None,
                "baseline_invalid_n": baseline_invalid[name], "current_invalid_n": current_invalid[name],
            }
            if feature_psi is not None and (worst_psi is None or feature_psi > worst_psi):
                worst_psi = feature_psi
        # Keep any observed warning even when another feature lacks evidence.
        severity = _severity_for_psi(worst_psi)

        baseline_stats = {
            name: {"n": len(values),
                   "mean": round(sum(values) / len(values), 6) if values else None}
            for name, values in baseline.items()
        }
        report = MLDriftReport(
            id=uuid_mod.uuid4(), report_kind="data_drift",
            model_id=uuid_mod.UUID(str(model_id)), scope_type="global", scope_id="",
            baseline_start=baseline_start, baseline_end=window_start,
            baseline_stats=baseline_stats, baseline_sample_count=baseline_n,
            window_start=window_start, window_end=now,
            sample_count=current_n, insufficient_data=insufficient,
            metrics={"monitoring_contract_version": 2, "features": metrics, "worst_psi": worst_psi,
                     "assessment_status": "insufficient_data" if insufficient else "assessed",
                     "model_type": model.model_type, "entity_type": spec.entity_type,
                     "feature_set_version": model.feature_set_version,
                     "baseline_kind": "previous_processing_time_window",
                     "time_basis": "computed_at", "population": "matching_feature_snapshots",
                     "sample_limit": DRIFT_SAMPLE_LIMIT,
                     "truncated": {"baseline": baseline_cap, "current": current_cap}},
            severity=severity, job_id=job_id, created_at=now)
        db.add(report)
        await db.commit()
        logger.info("[ML_OPS] data drift report severity=%s insufficient=%s worst_psi=%s",
                    severity, insufficient, worst_psi)
        return self.serialize_report(report)

    async def run_prediction_drift(self, db: AsyncSession, *, model_id, window_days: int = 7,
                                   baseline_days: int = 30,
                                   job_id: Optional[str] = None) -> Dict[str, Any]:
        """Anomaly-score distribution + volumes + shadow health, for ONE model."""
        from db_models import MLDriftReport, MLPrediction, MLShadowComparison

        now = datetime.utcnow()
        window_start = now - timedelta(days=window_days)
        baseline_start = window_start - timedelta(days=baseline_days)

        model, spec = await self._model(db, model_id)
        score_supported = model.model_type == "behavior_anomaly_model"

        async def _scores(start, end):
            if not score_supported:
                return [], 0, 0, False, 0
            rows = (await db.execute(
                select(MLPrediction.behavioral_anomaly_score,
                       MLPrediction.fallback_reason)
                .where(MLPrediction.model_id == model.id,
                       MLPrediction.model_type == model.model_type,
                       MLPrediction.created_at >= start,
                       MLPrediction.created_at < end)
                .order_by(MLPrediction.created_at.desc(), MLPrediction.id.desc())
                .limit(DRIFT_SAMPLE_LIMIT + 1))).all()
            truncated = len(rows) > DRIFT_SAMPLE_LIMIT
            rows = rows[:DRIFT_SAMPLE_LIMIT]
            scores = [float(r[0]) for r in rows if _finite_number(r[0]) and r[1] is None]
            invalid = sum(1 for r in rows if r[0] is not None and not _finite_number(r[0]))
            fallbacks = sum(1 for r in rows if r[1] is not None)
            return scores, len(rows), fallbacks, truncated, invalid

        current_scores, current_total, current_fallbacks, current_cap, current_invalid = await _scores(window_start, now)
        baseline_scores, baseline_total, _, baseline_cap, baseline_invalid = await _scores(baseline_start, window_start)

        comparisons = (await db.execute(
            select(MLShadowComparison.operational_disagreement,
                   MLShadowComparison.ml_failed, MLShadowComparison.ml_latency_ms)
            .where(MLShadowComparison.model_id == model.id,
                   MLShadowComparison.created_at >= window_start,
                   MLShadowComparison.created_at < now)
            .order_by(MLShadowComparison.created_at.desc(), MLShadowComparison.id.desc())
            .limit(DRIFT_SAMPLE_LIMIT + 1))).all()
        comparisons_cap = len(comparisons) > DRIFT_SAMPLE_LIMIT
        comparisons = comparisons[:DRIFT_SAMPLE_LIMIT]
        disagreement: Dict[str, int] = {}
        failures = 0
        latencies = []
        for kind, failed, latency in comparisons:
            disagreement[kind] = disagreement.get(kind, 0) + 1
            failures += int(bool(failed))
            if _finite_number(latency) and latency >= 0:
                latencies.append(latency)
        latencies.sort()

        min_samples = max(2, int(settings.ML_DRIFT_MIN_SAMPLES))
        insufficient = (not score_supported or len(current_scores) < min_samples
                        or len(baseline_scores) < min_samples or current_cap or baseline_cap
                        or comparisons_cap or current_invalid > 0 or baseline_invalid > 0)
        score_psi = None if insufficient else psi(baseline_scores, current_scores)
        severity = "normal" if insufficient else _severity_for_psi(score_psi)

        metrics = {
            "monitoring_contract_version": 2,
            "assessment_status": "insufficient_data" if insufficient else "assessed",
            "model_type": model.model_type,
            "score_source_supported": score_supported,
            "unavailable_reason": None if score_supported else "PERSISTED_SCORE_TELEMETRY_UNAVAILABLE_FOR_MODEL_FAMILY",
            "baseline_kind": "previous_prediction_time_window",
            "sample_limit": DRIFT_SAMPLE_LIMIT,
            "truncated": {"baseline": baseline_cap, "current": current_cap, "comparisons": comparisons_cap},
            "invalid_scores": {"baseline": baseline_invalid, "current": current_invalid},
            "anomaly_score_psi": score_psi,
            "prediction_volume": {"baseline": baseline_total, "current": current_total},
            "fallback_rate": (round(current_fallbacks / current_total, 4)
                              if current_total else None),
            "shadow_failure_rate": (round(failures / len(comparisons), 4)
                                    if comparisons else None),
            "operational_disagreement": disagreement,
            "latency_ms_p95": (latencies[int(len(latencies) * 0.95)]
                               if latencies else None),
        }
        report = MLDriftReport(
            id=uuid_mod.uuid4(), report_kind="prediction_drift",
            model_id=uuid_mod.UUID(str(model_id)), scope_type="global", scope_id="",
            baseline_start=baseline_start, baseline_end=window_start,
            baseline_stats={"score_n": len(baseline_scores)},
            baseline_sample_count=len(baseline_scores),
            window_start=window_start, window_end=now,
            sample_count=len(current_scores), insufficient_data=insufficient,
            metrics=metrics, severity=severity, job_id=job_id, created_at=now)
        db.add(report)
        await db.commit()
        return self.serialize_report(report)

    async def run_all(self, db: AsyncSession, job_id: Optional[str] = None) -> Dict[str, Any]:
        """One data-drift + one prediction-drift report PER SHADOW MODEL. Drift
        monitoring exists to monitor a model: with no shadow model nothing is
        written and the caller gets `skipped`. Scope is always the only scope
        any code supports — global."""
        from backend.ml.constants import ANOMALY_MODEL_TYPES
        from backend.ml.registry_service import registry_service
        note = ("drift reports are observations only — they never trigger "
                "deployment or retraining, and a single statistical signal never "
                "proves model failure")
        reports = []
        for model_type in ANOMALY_MODEL_TYPES:
            row = await registry_service.get_stage_model(db, model_type, "shadow")
            if row is None:
                continue
            data = await self.run_data_drift(db, model_id=row.id, job_id=job_id)
            prediction = await self.run_prediction_drift(db, model_id=row.id, job_id=job_id)
            reports.append({"model_id": str(row.id), "model_type": model_type,
                            "model_version": row.version,
                            "data_drift": data, "prediction_drift": prediction})
        if not reports:
            return {"skipped": "NO_SHADOW_MODEL", "reports": [], "note": note}
        first = reports[0]
        return {"reports": reports, "data_drift": first["data_drift"],
                "prediction_drift": first["prediction_drift"], "note": note}

    @staticmethod
    def serialize_report(row) -> Dict[str, Any]:
        def iso(dt):
            return iso_utc(dt) if dt else None
        return {
            "id": str(row.id), "report_kind": row.report_kind,
            "model_id": str(row.model_id) if row.model_id else None,
            "scope_type": row.scope_type, "scope_id": row.scope_id,
            "baseline_start": iso(row.baseline_start),
            "baseline_end": iso(row.baseline_end),
            "baseline_sample_count": row.baseline_sample_count,
            "window_start": iso(row.window_start), "window_end": iso(row.window_end),
            "sample_count": row.sample_count,
            "insufficient_data": row.insufficient_data,
            "metrics": row.metrics, "severity": row.severity,
            "assessment_status": "insufficient_data" if row.insufficient_data else "assessed",
            "created_at": iso(row.created_at),
        }

    async def scheduled_check(self) -> None:
        """supervised_loop work fn — REPORT ONLY, never any action."""
        from db_connection import db_manager
        async with db_manager.get_session() as db:
            await self.run_all(db, job_id="scheduled")


# Global instance
drift_service = DriftService()

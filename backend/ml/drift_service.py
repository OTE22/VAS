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
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import select, func as sa_func
from sqlalchemy.ext.asyncio import AsyncSession
from config import settings
from backend.utils.time_utils import iso_utc

logger = logging.getLogger(__name__)

DRIFT_BINS = 10


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

    async def _snapshot_features(self, db: AsyncSession, start: datetime,
                                 end: datetime) -> Dict[str, List[float]]:
        from db_models import MLFeatureSnapshot
        rows = (await db.execute(
            select(MLFeatureSnapshot.features)
            .where(MLFeatureSnapshot.entity_type == "person",
                   MLFeatureSnapshot.computed_at >= start,
                   MLFeatureSnapshot.computed_at < end)
            .limit(20000))).scalars().all()
        by_feature: Dict[str, List[float]] = {}
        for features in rows:
            for name, value in (features or {}).items():
                if isinstance(value, (int, float)):
                    by_feature.setdefault(name, []).append(float(value))
        return by_feature

    async def run_data_drift(self, db: AsyncSession, *, model_id, window_days: int = 7,
                             baseline_days: int = 30,
                             job_id: Optional[str] = None) -> Dict[str, Any]:
        """Feature-distribution drift: baseline window vs current window, for
        ONE model (the shadow model of its type at computation time)."""
        from db_models import MLDriftReport

        now = datetime.utcnow()
        window_start = now - timedelta(days=window_days)
        baseline_start = window_start - timedelta(days=baseline_days)
        current = await self._snapshot_features(db, window_start, now)
        baseline = await self._snapshot_features(db, baseline_start, window_start)

        min_samples = int(settings.ML_DRIFT_MIN_SAMPLES)
        current_n = max((len(v) for v in current.values()), default=0)
        baseline_n = max((len(v) for v in baseline.values()), default=0)
        insufficient = current_n < min_samples or baseline_n < min_samples

        metrics: Dict[str, Any] = {}
        worst_psi = None
        if not insufficient:
            for name in sorted(set(baseline) & set(current)):
                feature_psi = psi(baseline[name], current[name])
                metrics[name] = {
                    "psi": feature_psi,
                    "ks": ks_statistic(baseline[name], current[name]),
                    "js_divergence": js_divergence(baseline[name], current[name]),
                    "baseline_n": len(baseline[name]),
                    "current_n": len(current[name]),
                }
                if feature_psi is not None and (worst_psi is None or feature_psi > worst_psi):
                    worst_psi = feature_psi
        severity = "normal" if insufficient else _severity_for_psi(worst_psi)

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
            metrics={"features": metrics, "worst_psi": worst_psi},
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

        async def _scores(start, end) -> Tuple[List[float], int, int]:
            rows = (await db.execute(
                select(MLPrediction.behavioral_anomaly_score,
                       MLPrediction.fallback_reason)
                .where(MLPrediction.created_at >= start,
                       MLPrediction.created_at < end)
                .limit(20000))).all()
            scores = [float(r[0]) for r in rows if r[0] is not None]
            fallbacks = sum(1 for r in rows if r[1] is not None)
            return scores, len(rows), fallbacks

        current_scores, current_total, current_fallbacks = await _scores(window_start, now)
        baseline_scores, baseline_total, _ = await _scores(baseline_start, window_start)

        comparisons = (await db.execute(
            select(MLShadowComparison.operational_disagreement,
                   MLShadowComparison.ml_failed, MLShadowComparison.ml_latency_ms)
            .where(MLShadowComparison.created_at >= window_start)
            .limit(20000))).all()
        disagreement: Dict[str, int] = {}
        failures = 0
        latencies = []
        for kind, failed, latency in comparisons:
            disagreement[kind] = disagreement.get(kind, 0) + 1
            failures += int(bool(failed))
            if latency is not None:
                latencies.append(latency)
        latencies.sort()

        min_samples = int(settings.ML_DRIFT_MIN_SAMPLES)
        insufficient = len(current_scores) < min_samples or len(baseline_scores) < min_samples
        score_psi = None if insufficient else psi(baseline_scores, current_scores)
        severity = "normal" if insufficient else _severity_for_psi(score_psi)

        metrics = {
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
            "created_at": iso(row.created_at),
        }

    async def scheduled_check(self) -> None:
        """supervised_loop work fn — REPORT ONLY, never any action."""
        from db_connection import db_manager
        async with db_manager.get_session() as db:
            await self.run_all(db, job_id="scheduled")


# Global instance
drift_service = DriftService()

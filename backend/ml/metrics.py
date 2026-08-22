"""
Shadow-ML observability — Prometheus collectors for the ML subsystem.

Created lazily with duplicate protection (same discipline as
backend/core/metrics.py). Low-cardinality labels only: bands, reasons and
model versions — never entity ids or subject ids.
"""

import logging
from typing import Optional

logger = logging.getLogger(__name__)

_metrics = {}


def _safe(metric_class, name, documentation, **kwargs):
    try:
        return metric_class(name, documentation, **kwargs)
    except ValueError as exc:
        if "Duplicated timeseries" not in str(exc):
            raise
        from prometheus_client import REGISTRY
        for collector in list(REGISTRY._names_to_collectors.values()):
            if getattr(collector, "_name", None) == name:
                return collector
        return None


def get():
    """The collectors, created once. Returns None when prometheus_client is absent."""
    if _metrics:
        return _metrics
    try:
        from prometheus_client import Counter, Gauge
    except Exception:
        return None
    _metrics.update({
        "predictions": _safe(Counter, "ml_shadow_predictions_total",
                             "Shadow predictions that produced a band", ["band"]),
        "failures": _safe(Counter, "ml_shadow_prediction_failures_total",
                          "Shadow predictions that did not score, by reason", ["reason"]),
        "fallbacks": _safe(Counter, "ml_decision_fallback_total",
                           "ML-mode decisions that fell back to the statistical signal, by reason", ["reason"]),
        "schema_mismatch": _safe(Counter, "ml_feature_schema_mismatch_total",
                                 "Predictions refused because model and snapshot feature sets differ"),
        "reviewed": _safe(Counter, "ml_reviewed_shadow_outcomes_total",
                          "Reviewed manual outcomes linked to shadow predictions", ["outcome"]),
        "authority": _safe(Gauge, "ml_decision_authority",
                           "1 when ML supplies an operational input (ML mode eligible), else 0"),
        "mapping": _safe(Gauge, "ml_signal_mapping_validated",
                         "1 when a validated, scope-matched ML->risk mapping is active, else 0"),
        "model_info": _safe(Gauge, "ml_active_shadow_model_info",
                            "Active shadow model (value 1)", ["model_version", "feature_set_version", "algorithm"]),
        "review_coverage": _safe(Gauge, "ml_review_coverage_ratio",
                                 "Share of recent shadow predictions with a reviewed manual outcome"),
    })
    return _metrics


def observe_prediction(band: Optional[str], failure_reason: Optional[str]) -> None:
    m = get()
    if not m:
        return
    try:
        if failure_reason:
            m["failures"].labels(reason=str(failure_reason)[:64]).inc()
            if failure_reason == "FEATURE_SCHEMA_MISMATCH":
                m["schema_mismatch"].inc()
        elif band:
            m["predictions"].labels(band=str(band)[:32]).inc()
    except Exception:
        logger.debug("[ML_METRICS] prediction observation failed", exc_info=True)


def observe_fallback(reason: Optional[str]) -> None:
    m = get()
    if m and reason:
        try:
            m["fallbacks"].labels(reason=str(reason)[:64]).inc()
        except Exception:
            pass


def observe_reviewed_outcome(outcome: str) -> None:
    m = get()
    if m and outcome:
        try:
            m["reviewed"].labels(outcome=str(outcome)[:16]).inc()
        except Exception:
            pass


def set_state(*, ml_authority: bool, mapping_validated: bool, shadow_model=None,
              review_coverage: Optional[float] = None) -> None:
    m = get()
    if not m:
        return
    try:
        m["authority"].set(1 if ml_authority else 0)
        m["mapping"].set(1 if mapping_validated else 0)
        if shadow_model is not None:
            m["model_info"].labels(model_version=str(shadow_model.version),
                                   feature_set_version=str(shadow_model.feature_set_version),
                                   algorithm=str(shadow_model.algorithm)).set(1)
        if review_coverage is not None:
            m["review_coverage"].set(float(review_coverage))
    except Exception:
        logger.debug("[ML_METRICS] state update failed", exc_info=True)

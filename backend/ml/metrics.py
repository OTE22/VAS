"""
Shadow-ML observability — Prometheus collectors for the ML subsystem.

Created lazily with duplicate protection (same discipline as
backend/core/metrics.py). Low-cardinality labels only: bands, reasons and
model versions — never entity ids or subject ids.
"""

import logging
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

_metrics = {}


def _safe(metric_class, name, documentation, labelnames=None, **kwargs):
    """Create a collector (labelnames positional or keyword), returning the
    existing one on a duplicate registration. NOTE: this signature used to
    take no labels argument while every collector passed one - the TypeError
    was swallowed by each caller's except, so no ML metric had ever been
    registered. The acceptance test now instantiates them explicitly."""
    try:
        if labelnames:
            return metric_class(name, documentation, labelnames=list(labelnames), **kwargs)
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
        # --- added by the production shadow-readiness audit ---------------
        "model_load_failures": _safe(Counter, "ml_model_load_failures_total",
                                     "Registry refusals when loading the shadow artifact, by code", ["reason"]),
        "training_jobs": _safe(Counter, "ml_training_jobs_total",
                               "Training jobs by final status (completed | failed:<code>)", ["status"]),
        "mode_rejections": _safe(Counter, "ml_decision_mode_rejections_total",
                                 "Mode changes refused by the gate (MODE_GATED), by requested mode", ["target_mode"]),
        "outcomes_linked": _safe(Counter, "ml_evidence_outcomes_linked_total",
                                 "Outcome labels linked to shadow predictions, by label kind", ["label_kind"]),
        "collector_rows": _safe(Counter, "ml_collector_rows_scanned_total",
                                "Appearance rows scanned by the feature collector"),
        "collector_watermark_age": _safe(Gauge, "ml_collector_watermark_age_seconds",
                                         "Seconds between now and the collector watermark (lag)"),
        # storage growth of the evidence tables (estimates from pg_class /
        # pg_total_relation_size: cheap, refreshed on scrape)
        "table_bytes": _safe(Gauge, "ml_evidence_table_bytes",
                             "Total on-disk size of an ML evidence table (incl. indexes/toast)", ["table"]),
        "table_rows": _safe(Gauge, "ml_evidence_table_rows",
                            "Estimated row count of an ML evidence table (pg_class.reltuples)", ["table"]),
        "state_refreshed_at": _safe(Gauge, "ml_state_refreshed_timestamp_seconds",
                                    "Unix time of the last ML state gauge refresh (event or scrape)"),
    })
    return _metrics


EVIDENCE_TABLES = ("ml_predictions", "ml_shadow_comparisons", "ml_labels", "ml_feature_snapshots",
                   "threat_assessments")

# The state gauges (authority, mapping, active model, review coverage,
# watermark age, table sizes) are refreshed by EVERY state-changing
# application operation (mode change / pause / registry transition /
# threshold activation / label review / collection end) AND, because those
# events land in one worker process, by a throttled scrape-time refresh in
# each process. The overview page is never a correctness dependency.
SCRAPE_REFRESH_MIN_INTERVAL_S = 30.0
_last_refresh_monotonic = 0.0


def last_refresh_age_seconds() -> Optional[float]:
    import time
    return (time.monotonic() - _last_refresh_monotonic) if _last_refresh_monotonic else None


async def refresh_state(db, *, reason: str = "event") -> Dict[str, Any]:
    """Recompute the state gauges from the database with a handful of cheap
    queries (registry row, threshold row, mapping rows, one aggregate over
    the shadow model's predictions, the checkpoint row, pg_class sizes).
    Never raises; returns what was observed."""
    import time
    from datetime import datetime
    from sqlalchemy import select, text as sa_text
    global _last_refresh_monotonic
    observed: Dict[str, Any] = {"reason": reason}
    m = get()
    if not m:
        return observed
    try:
        from backend.ml.constants import MODEL_TYPE_BEHAVIOR_ANOMALY
        from backend.ml.decision_service import decision_service
        from backend.ml.registry_service import registry_service
        shadow = await registry_service.get_stage_model(db, MODEL_TYPE_BEHAVIOR_ANOMALY, "shadow")
        mapping = None
        if shadow is not None:
            try:
                mapping = await decision_service._mapping_policy(db)
            except Exception:
                mapping = None
        mode = decision_service.current_mode()
        ml_authority = bool(mode == "ml" and shadow is not None and mapping is not None)
        coverage = None
        if shadow is not None:
            from db_models import MLLabel, MLPrediction
            from backend.ml.evidence_grade import evidence_filter
            total = (await db.execute(sa_text(
                "SELECT count(*) FROM ml_predictions WHERE model_id = :m AND ml_anomaly_band IS NOT NULL"),
                {"m": str(shadow.id)})).scalar() or 0
            reviewed = (await db.execute(
                select(sa_text("count(*)")).select_from(MLPrediction)
                .join(MLLabel, MLLabel.id == MLPrediction.outcome_label_id)
                .where(MLPrediction.model_id == shadow.id, MLPrediction.ml_anomaly_band.isnot(None),
                       evidence_filter(MLLabel)))).scalar() or 0
            coverage = (float(reviewed) / float(total)) if total else 0.0
            observed.update({"predictions": int(total), "reviewed": int(reviewed)})
        set_state(ml_authority=ml_authority, mapping_validated=mapping is not None,
                  shadow_model=shadow, review_coverage=coverage)
        observed.update({"ml_authority": ml_authority, "mapping_validated": mapping is not None,
                         "shadow_model": (shadow.version if shadow is not None else None),
                         "review_coverage": coverage, "mode": mode})
        # collector lag from the checkpoint row
        wm = (await db.execute(sa_text(
            "SELECT watermark_event_time FROM ml_collection_checkpoints ORDER BY id LIMIT 1"))).scalar()
        if wm is not None:
            age = (datetime.utcnow() - wm).total_seconds()
            m["collector_watermark_age"].set(max(0.0, age))
            observed["watermark_age_seconds"] = age
        # storage growth (estimates; no table scans)
        rows = (await db.execute(sa_text(
            "SELECT c.relname, pg_total_relation_size(c.oid), c.reltuples FROM pg_class c "
            "JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = 'public' "
            "AND c.relkind = 'r' AND c.relname = ANY(:names)"), {"names": list(EVIDENCE_TABLES)})).all()
        sizes = {}
        for name, total_bytes, reltuples in rows:
            m["table_bytes"].labels(table=str(name)).set(float(total_bytes or 0))
            m["table_rows"].labels(table=str(name)).set(max(0.0, float(reltuples or 0)))
            sizes[str(name)] = {"bytes": int(total_bytes or 0), "rows_estimate": int(max(0, reltuples or 0))}
        observed["tables"] = sizes
        m["state_refreshed_at"].set(time.time())
        _last_refresh_monotonic = time.monotonic()
    except Exception:
        logger.debug("[ML_METRICS] state refresh failed (%s)", reason, exc_info=True)
    return observed


async def refresh_state_if_stale(db, *, max_age_seconds: float = SCRAPE_REFRESH_MIN_INTERVAL_S,
                                 reason: str = "scrape") -> bool:
    """Scrape-time refresh, throttled per process so /metrics never runs
    the queries more often than every max_age_seconds."""
    import time
    if _last_refresh_monotonic and (time.monotonic() - _last_refresh_monotonic) < max_age_seconds:
        return False
    await refresh_state(db, reason=reason)
    return True


async def refresh_state_with_own_session(reason: str = "event") -> None:
    """For call sites that have committed and hold no usable session."""
    try:
        from db_connection import db_manager
        async with db_manager.get_session() as db:
            await refresh_state(db, reason=reason)
    except Exception:
        logger.debug("[ML_METRICS] refresh with own session failed", exc_info=True)


_BOUNDED = {"reason", "status", "target_mode", "label_kind", "band", "outcome",
            "model_version", "feature_set_version", "algorithm"}


def label_names() -> dict:
    """Label names per collector — pinned by tests: bounded vocabularies only."""
    out = {}
    for key, metric in (get() or {}).items():
        out[key] = list(getattr(metric, "_labelnames", ()) or ())
    return out


def observe_model_load_failure(code: Optional[str]) -> None:
    m = get()
    if m and code:
        try:
            m["model_load_failures"].labels(reason=str(code)[:64]).inc()
        except Exception:
            pass


def observe_training_job(status: str) -> None:
    """status: 'completed' or 'failed:<stable code>' (codes are an enum-sized set)."""
    m = get()
    if m and status:
        try:
            m["training_jobs"].labels(status=str(status)[:64]).inc()
        except Exception:
            pass


def observe_mode_rejection(target_mode: Optional[str]) -> None:
    m = get()
    if m and target_mode:
        try:
            m["mode_rejections"].labels(target_mode=str(target_mode)[:16]).inc()
        except Exception:
            pass


def observe_outcome_linked(label_kind: Optional[str], count: int = 1) -> None:
    m = get()
    if m and label_kind and count > 0:
        try:
            m["outcomes_linked"].labels(label_kind=str(label_kind)[:16]).inc(count)
        except Exception:
            pass


def observe_collector(rows_scanned: int, watermark_age_seconds: Optional[float]) -> None:
    m = get()
    if not m:
        return
    try:
        if rows_scanned:
            m["collector_rows"].inc(int(rows_scanned))
        if watermark_age_seconds is not None:
            m["collector_watermark_age"].set(float(watermark_age_seconds))
    except Exception:
        pass


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
        # Exactly one active series: clear the previous version's labels first
        # so several versions never report "active" at once.
        try:
            m["model_info"].clear()
        except Exception:
            pass
        if shadow_model is not None:
            m["model_info"].labels(model_version=str(shadow_model.version),
                                   feature_set_version=str(shadow_model.feature_set_version),
                                   algorithm=str(shadow_model.algorithm)).set(1)
        if review_coverage is not None:
            m["review_coverage"].set(float(review_coverage))
    except Exception:
        logger.debug("[ML_METRICS] state update failed", exc_info=True)

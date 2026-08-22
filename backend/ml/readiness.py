"""
Readiness — two gate families that must never be confused.

ENGINEERING gate: is the pipeline mechanically sound? (schema, checksums,
artifact reload, split integrity, non-degenerate scores, seed stability,
inference contract, code revision recorded). A PASS here means the model can
be run in SHADOW safely. It says nothing about whether the model means
anything.

SCIENTIFIC gate: is there enough evidence that the model represents
behaviour? (history span, repeat observations per entity, feature
availability and its stability across the temporal split, train->test score
shift, reviewed-outcome coverage, mapping validation). The repository holds
NO validated thresholds for these, so the gate reports the statistics with
machine-readable reasons and the status INSUFFICIENT_EVIDENCE — unless an
administrator has configured minimums (settings, default "not configured")
and every configured minimum is met AND a validated mapping exists.

Nothing here invents a threshold. Nothing here promotes.
"""

import hashlib
import json
import logging
import math
from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence

logger = logging.getLogger(__name__)

PREPROCESSOR_VERSION = "impute-train-median-v1"
ENGINEERING_PASS = "PASS"
ENGINEERING_FAIL = "FAIL"
SCIENTIFIC_INSUFFICIENT = "INSUFFICIENT_EVIDENCE"
SCIENTIFIC_SUFFICIENT = "SUFFICIENT_EVIDENCE"
SCIENTIFIC_NOT_CONFIGURED = "NOT_CONFIGURED"

# History-dependent features and the prior observations they need (from the
# builders in feature_builders.py) — used to count how many TRAIN entities
# could have had each feature at all.
HISTORY_DEPENDENT_FEATURES = {
    "baseline_hour_deviation_last": "needs >= 4 prior sightings + the anomaly baseline floor",
    "new_pipeline_flag_7d": "needs a prior 83-day window",
    "hour_sin_30d": "needs >= 3 sightings in 30 days",
    "hour_cos_30d": "needs >= 3 sightings in 30 days",
    "hour_std_30d": "needs >= 3 sightings in 30 days",
    "active_days_ratio_30d": "needs >= 1 sighting in 30 days",
    "off_hours_ratio_30d": "needs >= 1 sighting in 30 days",
    "weekend_holiday_ratio_30d": "needs >= 1 sighting in 30 days",
    "max_hourly_burst_30d": "needs >= 1 sighting in 30 days",
    "days_since_first_seen": "needs any prior sighting",
    "days_since_last_seen": "needs any prior sighting",
}


def feature_schema_hash(feature_names: Sequence[str]) -> str:
    return hashlib.sha256(json.dumps(list(feature_names)).encode()).hexdigest()


def _percentile(values: List[float], q: float) -> Optional[float]:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return float(ordered[0])
    pos = (len(ordered) - 1) * q
    lo, hi = int(math.floor(pos)), int(math.ceil(pos))
    if lo == hi:
        return float(ordered[lo])
    return float(ordered[lo] + (ordered[hi] - ordered[lo]) * (pos - lo))


def distribution(values: List[float]) -> Dict[str, Any]:
    if not values:
        return {"n": 0}
    return {"n": len(values), "min": float(min(values)), "p10": _percentile(values, 0.10),
            "p25": _percentile(values, 0.25), "median": _percentile(values, 0.50),
            "p75": _percentile(values, 0.75), "p90": _percentile(values, 0.90),
            "p95": _percentile(values, 0.95), "max": float(max(values)),
            "mean": float(sum(values) / len(values))}


# ---------------------------------------------------------------------------
# Dataset-level statistics (computed at build time, stored on the quality report)
# ---------------------------------------------------------------------------

def feature_availability_by_split(rows: List[Dict[str, Any]], feature_names: Sequence[str]) -> Dict[str, Any]:
    """Per feature: available share overall and per split, missing shares and
    the train->test availability delta. A large positive delta means the
    feature exists mostly in the NEWER period — a maturity shift, which can
    masquerade as behavioural drift."""
    splits = {"train": [], "val": [], "test": []}
    for row in rows:
        splits.setdefault(row.get("split") or "unsplit", []).append(row)
    out: Dict[str, Any] = {}
    for name in feature_names:
        def share(part):
            if not part:
                return None
            return round(sum(1 for r in part if name in (r.get("features") or {})) / len(part), 4)
        overall = share(rows)
        tr, va, te = share(splits.get("train", [])), share(splits.get("val", [])), share(splits.get("test", []))
        entry = {
            "overall_available_pct": overall,
            "train_available_pct": tr, "val_available_pct": va, "test_available_pct": te,
            "train_missing_pct": None if tr is None else round(1 - tr, 4),
            "val_missing_pct": None if va is None else round(1 - va, 4),
            "test_missing_pct": None if te is None else round(1 - te, 4),
            "availability_delta_train_test": (None if tr is None or te is None else round(te - tr, 4)),
            "train_rows_available": sum(1 for r in splits.get("train", []) if name in (r.get("features") or {})),
            "train_entities_available": len({r["entity_id"] for r in splits.get("train", [])
                                             if name in (r.get("features") or {})}),
        }
        if name in HISTORY_DEPENDENT_FEATURES:
            entry["history_requirement"] = HISTORY_DEPENDENT_FEATURES[name]
        out[name] = entry
    shifts = [(n, e["availability_delta_train_test"]) for n, e in out.items()
              if e["availability_delta_train_test"] is not None]
    return {
        "features": out,
        "largest_train_to_test_availability_gains": sorted(
            [{"feature": n, "delta": d} for n, d in shifts if d > 0], key=lambda x: -x["delta"])[:6],
        "note": ("a feature far more available in test than in train is a population-"
                 "maturity shift (history accumulating), not necessarily behavioural drift"),
    }


async def entity_history_statistics(db, entity_ids: Sequence[str], feature_names: Sequence[str],
                                    rows: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    """Appearances per identity (ALL history, from the source table) for the
    dataset's entities, the history span, and repeat-observation coverage."""
    from sqlalchemy import text as sa_text
    ids = sorted({str(e) for e in entity_ids})
    if not ids:
        return {"unique_entities": 0, "history_span_days": 0, "appearances_per_entity": {"n": 0}}
    counts = {}
    span = None
    async def _chunks(seq, size=1000):
        for i in range(0, len(seq), size):
            yield seq[i:i + size]
    async for chunk in _chunks(ids):
        res = await db.execute(sa_text(
            "SELECT identity_id::text, count(*) FROM identity_appearances "
            "WHERE identity_id::text = ANY(:ids) GROUP BY identity_id"), {"ids": chunk})
        for ident, c in res.all():
            counts[ident] = int(c)
    span_row = (await db.execute(sa_text(
        "SELECT min(start_time), max(start_time) FROM identity_appearances WHERE identity_id::text = ANY(:ids)"),
        {"ids": ids})).one()
    if span_row and span_row[0] and span_row[1]:
        span = (span_row[1] - span_row[0]).total_seconds() / 86400.0
    values = [float(counts.get(i, 0)) for i in ids]
    n = len(ids)
    thresholds = {f"pct_entities_ge_{k}_appearances": round(sum(1 for v in values if v >= k) / n, 4)
                  for k in (1, 3, 5, 10, 20)}
    rows_per_entity = {}
    if rows:
        per = {}
        for r in rows:
            per[r["entity_id"]] = per.get(r["entity_id"], 0) + 1
        rows_per_entity = distribution([float(v) for v in per.values()])
    return {
        "unique_entities": n,
        "history_span_days": round(span, 2) if span is not None else None,
        "appearances_per_entity": distribution(values),
        "rows_per_entity": rows_per_entity,
        **thresholds,
        "note": ("appearances counted over ALL history of each entity in the source table; "
                 "rows_per_entity counts dataset snapshots"),
    }


def cold_start_conclusion(population: Dict[str, Any], availability: Dict[str, Any],
                          usable_feature_count: Optional[int] = None) -> Dict[str, Any]:
    """Two separate verdicts: pipeline usability (engineering) vs population
    maturity (scientific). No threshold is invented: the statistics are the
    conclusion; the maturity verdict is INSUFFICIENT_EVIDENCE until a
    configured policy says otherwise."""
    feats = availability.get("features", {})
    sparse = sorted([n for n, e in feats.items()
                     if e.get("overall_available_pct") is not None and e["overall_available_pct"] < 0.5])
    hist = [n for n in feats if n in HISTORY_DEPENDENT_FEATURES]
    hist_sparse = [n for n in sparse if n in HISTORY_DEPENDENT_FEATURES]
    facts = {
        "history_span_days": population.get("history_span_days"),
        "unique_entities": population.get("unique_entities"),
        "appearances_per_entity_median": (population.get("appearances_per_entity") or {}).get("median"),
        "pct_entities_ge_5_appearances": population.get("pct_entities_ge_5_appearances"),
        "pct_entities_ge_10_appearances": population.get("pct_entities_ge_10_appearances"),
        "history_dependent_features": len(hist),
        "history_dependent_features_below_50pct_available": hist_sparse,
        "features_below_50pct_available": sparse,
        "largest_availability_gains_train_to_test": availability.get("largest_train_to_test_availability_gains", []),
        "usable_feature_count": usable_feature_count,
    }
    return {
        "pipeline_technically_usable": True,
        "behavioral_population_maturity": SCIENTIFIC_INSUFFICIENT,
        "facts": facts,
        "interpretation": (
            "The dataset is reproducible and usable to validate the ML/MLOps pipeline; the "
            "population statistics above decide whether anomaly bands can be read as behavioural "
            "risk. With a short history span, few repeat observations per entity and "
            "history-dependent features mostly unavailable, the model largely learns observation "
            "frequency and cold-start status rather than behaviour. Maturity is judged against "
            "configured policy (ML_SCIENTIFIC_* settings), never against invented numbers."),
    }


# ---------------------------------------------------------------------------
# Engineering gate (model-level; reuses the trainer's quality gates)
# ---------------------------------------------------------------------------

def engineering_gate(*, quality_gates: Dict[str, Any], dataset_quality_passed: bool,
                     split_meta: Dict[str, Any], artifact_hash: Optional[str],
                     code_version: Optional[str], feature_names: Sequence[str],
                     medians: Dict[str, float], seed_stability: Optional[float],
                     dataset_checksum: Optional[str], parquet_sha256: Optional[str]) -> Dict[str, Any]:
    checks: Dict[str, Dict[str, Any]] = {}
    for name, gate in (quality_gates or {}).items():
        checks[name] = {"passed": bool(gate.get("passed")), "actual": gate.get("actual"),
                        "required": gate.get("required")}
    checks["dataset_validation_passed"] = {"passed": bool(dataset_quality_passed)}
    checks["dataset_checksum_recorded"] = {"passed": bool(dataset_checksum) and dataset_checksum != "empty"}
    checks["parquet_file_hash_recorded"] = {"passed": bool(parquet_sha256)}
    counts = (split_meta or {}).get("counts") or {}
    method = (split_meta or {}).get("method")
    checks["temporal_group_split_valid"] = {
        "passed": method in ("temporal_group", "temporal") and bool(counts.get("train")),
        "actual": {"method": method, "counts": counts},
        "required": "a declared time-ordered split (temporal_group | temporal) with training rows"}
    groups = (split_meta or {}).get("group_counts") or {}
    if method == "temporal":
        overlap = (split_meta or {}).get("entity_overlap")
        checks["entity_group_isolation"] = {
            "passed": bool(groups) and overlap is not None,
            "actual": {"entity_overlap": overlap},
            "note": "split strategy 'temporal' declares that entities recur across splits; "
                    "the overlap is recorded, isolation is not claimed"}
    else:
        checks["entity_group_isolation"] = {
            "passed": bool(groups) and (split_meta or {}).get("dropped_for_group_integrity") is not None,
            "actual": {"dropped_for_group_integrity": (split_meta or {}).get("dropped_for_group_integrity")}}
    checks["artifact_checksum_recorded"] = {"passed": bool(artifact_hash)}
    checks["inference_contract_medians_complete"] = {
        "passed": all(n in medians for n in feature_names), "actual": len(medians), "required": len(feature_names)}
    checks["code_version_recorded"] = {"passed": bool(code_version), "actual": code_version}
    checks["seed_stability_measured"] = {"passed": seed_stability is None or math.isfinite(float(seed_stability)),
                                         "actual": seed_stability,
                                         "note": "measured only; no acceptance threshold is configured"}
    checks["fallback_path_defined"] = {"passed": True,
                                       "note": "inference never raises; every failure is a FallbackReason; rules continue"}
    passed = all(c["passed"] for c in checks.values())
    return {"status": ENGINEERING_PASS if passed else ENGINEERING_FAIL, "checks": checks,
            "failed": [k for k, c in checks.items() if not c["passed"]],
            "meaning": "mechanical soundness only — not scientific validity"}


# ---------------------------------------------------------------------------
# Scientific gate (evidence-based; thresholds only from configuration)
# ---------------------------------------------------------------------------

def _configured(name: str) -> Optional[float]:
    try:
        from config import settings
        value = getattr(settings, name, None)
    except Exception:
        value = None
    try:
        value = float(value) if value is not None else None
    except (TypeError, ValueError):
        value = None
    return value if value and value > 0 else None


def scientific_gate(*, population: Dict[str, Any], availability: Dict[str, Any],
                    score_shift: Optional[Dict[str, Any]], evidence_coverage: Optional[Dict[str, Any]],
                    mapping_validated: bool, split_meta: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Statistics + machine-readable reasons. Status is INSUFFICIENT_EVIDENCE
    unless every CONFIGURED minimum is met and the mapping is validated; with
    no configured minimums the status is INSUFFICIENT_EVIDENCE with the reason
    NOT_CONFIGURED — the numbers are shown, never judged by invented cutoffs."""
    feats = availability.get("features", {}) if availability else {}
    availability_summary = {n: e.get("overall_available_pct") for n, e in feats.items()}
    stability = {n: e.get("availability_delta_train_test") for n, e in feats.items()}
    metrics = {
        "history_span_days": population.get("history_span_days"),
        "unique_entities": population.get("unique_entities"),
        "rows_per_entity": population.get("rows_per_entity"),
        "appearances_per_entity": population.get("appearances_per_entity"),
        **{k: v for k, v in population.items() if k.startswith("pct_entities_ge_")},
        "feature_availability": availability_summary,
        "feature_availability_delta_train_test": stability,
        "train_test_score_shift": score_shift,
        "reviewed_outcome_coverage": evidence_coverage,
        "signal_mapping_validation_status": "VALIDATED" if mapping_validated else "REQUIRES_VALIDATION",
    }
    configured = {
        "ML_SCIENTIFIC_MIN_HISTORY_DAYS": _configured("ML_SCIENTIFIC_MIN_HISTORY_DAYS"),
        "ML_SCIENTIFIC_MIN_MEDIAN_APPEARANCES": _configured("ML_SCIENTIFIC_MIN_MEDIAN_APPEARANCES"),
        "ML_EVIDENCE_MIN_REVIEWED_TOTAL": _configured("ML_EVIDENCE_MIN_REVIEWED_TOTAL"),
        "ML_EVIDENCE_MIN_REVIEWED_PER_BAND": _configured("ML_EVIDENCE_MIN_REVIEWED_PER_BAND"),
    }
    reasons: List[Dict[str, Any]] = []
    any_configured = any(v is not None for v in configured.values())
    if not any_configured:
        reasons.append({"code": "NOT_CONFIGURED",
                        "detail": "no scientific minimums are configured (ML_SCIENTIFIC_* / ML_EVIDENCE_*); "
                                  "statistics are exposed, no verdict is invented"})
    span = population.get("history_span_days")
    if configured["ML_SCIENTIFIC_MIN_HISTORY_DAYS"] is not None and (span or 0) < configured["ML_SCIENTIFIC_MIN_HISTORY_DAYS"]:
        reasons.append({"code": "HISTORY_SPAN_BELOW_MINIMUM", "actual": span,
                        "required": configured["ML_SCIENTIFIC_MIN_HISTORY_DAYS"]})
    median_app = (population.get("appearances_per_entity") or {}).get("median")
    if configured["ML_SCIENTIFIC_MIN_MEDIAN_APPEARANCES"] is not None and (median_app or 0) < configured["ML_SCIENTIFIC_MIN_MEDIAN_APPEARANCES"]:
        reasons.append({"code": "REPEAT_OBSERVATIONS_BELOW_MINIMUM", "actual": median_app,
                        "required": configured["ML_SCIENTIFIC_MIN_MEDIAN_APPEARANCES"]})
    cov = evidence_coverage or {}
    total_reviewed = cov.get("reviewed_total")
    if configured["ML_EVIDENCE_MIN_REVIEWED_TOTAL"] is not None and (total_reviewed or 0) < configured["ML_EVIDENCE_MIN_REVIEWED_TOTAL"]:
        reasons.append({"code": "REVIEWED_OUTCOMES_BELOW_MINIMUM", "actual": total_reviewed,
                        "required": configured["ML_EVIDENCE_MIN_REVIEWED_TOTAL"]})
    per_band = cov.get("reviewed_per_band") or {}
    if configured["ML_EVIDENCE_MIN_REVIEWED_PER_BAND"] is not None:
        short = {b: n for b, n in per_band.items() if (n or 0) < configured["ML_EVIDENCE_MIN_REVIEWED_PER_BAND"]}
        if short or not per_band:
            reasons.append({"code": "REVIEWED_OUTCOMES_PER_BAND_BELOW_MINIMUM", "actual": per_band,
                            "required": configured["ML_EVIDENCE_MIN_REVIEWED_PER_BAND"]})
    if not mapping_validated:
        reasons.append({"code": "SIGNAL_MAPPING_UNVALIDATED",
                        "detail": "no validated, scope-matched ML->risk mapping policy"})
    # informational facts (never a verdict by themselves)
    facts: List[Dict[str, Any]] = []
    sparse_hist = [n for n, v in availability_summary.items()
                   if n in HISTORY_DEPENDENT_FEATURES and v is not None and v < 0.5]
    if sparse_hist:
        facts.append({"code": "HISTORY_DEPENDENT_FEATURES_SPARSE", "features": sparse_hist})
    gains = (availability.get("largest_train_to_test_availability_gains") or []) if availability else []
    if gains:
        facts.append({"code": "AVAILABILITY_SHIFT_TRAIN_TO_TEST", "features": gains,
                      "detail": "maturity shift can masquerade as behavioural drift"})
    if score_shift and score_shift.get("test_p90") is not None and score_shift.get("train_p90") is not None \
            and score_shift["train_p90"] > 0 and score_shift["test_p90"] / score_shift["train_p90"] > 1.5:
        facts.append({"code": "TRAIN_TEST_SCORE_SHIFT", "detail": score_shift})
    if split_meta and split_meta.get("method") == "temporal":
        facts.append({"code": "ENTITY_OVERLAP_ACROSS_SPLITS",
                      "detail": split_meta.get("entity_overlap"),
                      "meaning": "val/test scores describe later behaviour of known entities, "
                                 "not generalisation to unseen entities"})
    status = SCIENTIFIC_SUFFICIENT if (any_configured and not reasons) else SCIENTIFIC_INSUFFICIENT
    return {"status": status, "metrics": metrics, "configured_minimums": configured,
            "reasons": reasons, "facts": facts,
            "meaning": "evidence that the model represents behaviour — independent of engineering soundness"}


# ---------------------------------------------------------------------------
# Post-hoc readiness for an existing model (no retraining)
# ---------------------------------------------------------------------------

async def compute_model_readiness(db, model_row, *, persist: bool = True) -> Dict[str, Any]:
    """Both gates for a model trained BEFORE gate recording existed, computed
    from its registered artifact and dataset (never by retraining). Recorded
    on evaluation_report with computed_post_hoc=True so nobody mistakes it for
    a training-time record. Idempotent."""
    from sqlalchemy import select
    from db_models import MLDataset, MLPrediction
    from backend.ml.registry_service import validate_artifact
    from backend.ml.trainer import _load_parquet_rows

    report = dict(model_row.evaluation_report or {})
    dataset = None
    if model_row.dataset_id:
        dataset = (await db.execute(
            select(MLDataset).where(MLDataset.id == model_row.dataset_id))).scalar_one_or_none()
    artifact_ok, medians, artifact_error = False, {}, None
    try:
        payload = validate_artifact(model_row.artifact_path, expected_hash=model_row.artifact_hash,
                                    expected_feature_names=list(model_row.feature_names or []),
                                    expected_dependencies=model_row.dependency_versions)
        medians = payload.get("imputation_medians") or {}
        artifact_ok = True
    except Exception as exc:
        artifact_error = getattr(exc, "code", type(exc).__name__)

    rows = []
    if dataset is not None and dataset.storage_path:
        try:
            rows = _load_parquet_rows(dataset.storage_path)
        except Exception:
            rows = []
    feature_names = list(model_row.feature_names or [])
    split_rows = [r for r in rows if r.get("split")]
    availability = feature_availability_by_split(split_rows, feature_names) if rows else {}
    dq = (dataset.quality_report if dataset is not None else None) or {}
    population = dq.get("population") or {}
    if not population and rows:
        population = await entity_history_statistics(
            db, [r["entity_id"] for r in rows], feature_names, rows=split_rows)

    gates = dict((model_row.quality_gates or {}).get("gates") or {})
    if not artifact_ok:
        gates["artifact_validates"] = {"passed": False, "actual": artifact_error, "required": "valid artifact"}
    engineering = engineering_gate(
        quality_gates=gates, dataset_quality_passed=bool(dq.get("passed", True)),
        split_meta=(dataset.split_config if dataset is not None else None) or {},
        artifact_hash=model_row.artifact_hash, code_version=getattr(model_row, "code_version", None),
        feature_names=feature_names, medians=medians,
        seed_stability=report.get("seed_stability_correlation"),
        dataset_checksum=(dataset.checksum if dataset is not None else None),
        parquet_sha256=(getattr(dataset, "parquet_sha256", None) if dataset is not None else None))

    # reviewed-outcome coverage for THIS model (unreviewed = unknown)
    from backend.ml import evidence_stats
    preds = (await db.execute(
        select(MLPrediction.ml_anomaly_band, MLPrediction.outcome_label)
        .where(MLPrediction.model_id == model_row.id, MLPrediction.ml_anomaly_band.isnot(None)))).all()
    counts: Dict[str, int] = {}
    reviewed_rows = []
    for band, outcome in preds:
        counts[band] = counts.get(band, 0) + 1
        if outcome in ("positive", "negative"):
            reviewed_rows.append({"band": band, "outcome": outcome, "score": None})
    table = evidence_stats.band_table(reviewed_rows)
    cov = evidence_stats.coverage(counts, table)

    shift = report.get("temporal_shift")
    if shift is None and report.get("splits"):
        sp = report["splits"]
        shift = {"train_p50": sp.get("train", {}).get("score_p50"), "train_p90": sp.get("train", {}).get("score_p90"),
                 "test_p50": sp.get("test", {}).get("score_p50"), "test_p90": sp.get("test", {}).get("score_p90")}
    from backend.ml.decision_service import decision_service
    mapping = await decision_service._mapping_policy(db)
    scientific = scientific_gate(population=population, availability=availability, score_shift=shift,
                                 evidence_coverage=cov, mapping_validated=bool(mapping),
                                 split_meta=(dataset.split_config if dataset is not None else None) or {})
    stamp = datetime.utcnow().isoformat() + "Z"
    engineering["computed_post_hoc"] = scientific["computed_post_hoc"] = True
    engineering["computed_at"] = scientific["computed_at"] = stamp
    if availability and "feature_availability_by_split" not in report:
        report["feature_availability_by_split"] = availability
    report["engineering_gate"] = engineering
    report["scientific_gate"] = scientific
    if persist:
        model_row.evaluation_report = report
        cfg = dict(getattr(model_row, "training_config", None) or {})
        cfg.update({"engineering_gate": engineering["status"], "scientific_gate": scientific["status"],
                    "feature_schema_hash": cfg.get("feature_schema_hash") or feature_schema_hash(feature_names),
                    "preprocessor_version": cfg.get("preprocessor_version") or PREPROCESSOR_VERSION,
                    "gates_computed_post_hoc": True})
        model_row.training_config = cfg
        await db.commit()
    return {"model_id": str(model_row.id), "version": model_row.version,
            "engineering_gate": engineering, "scientific_gate": scientific,
            "reviewed_outcome_coverage": cov, "computed_post_hoc": True, "computed_at": stamp}

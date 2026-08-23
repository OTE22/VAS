"""
Candidate-vs-incumbent evaluation — descriptive, task-aware, never a verdict.

A freshly trained candidate is scored side by side with the model currently
in SHADOW on the SAME rows of the SAME dataset split. The comparison is
only meaningful when the two models answer the same question with the
same score semantics; otherwise it is reported as NOT_APPLICABLE rather
than comparing unlike quantities.

What is measured is what can be measured without ground truth: score
quantiles, band distributions under each model's own cutpoints, rank
agreement between the two score orderings, scoring latency, artifact size,
feature-schema compatibility and scoring failure rate. No threshold in this
module decides promotion — `promotion_decision` is always REQUIRES_VALIDATION
because the repository holds no validated promotion criteria.
"""

import logging
import os
import time
from typing import Any, Dict, List, Optional

from backend.ml.constants import ANOMALY_BANDS

logger = logging.getLogger(__name__)

COMPATIBILITY_FIELDS = ("model_type", "model_purpose", "score_type", "feature_set_version")


def _band_for(score: float, cutpoints: Dict[str, float]) -> str:
    if score >= cutpoints["highly_unusual"]:
        return "highly_unusual"
    if score >= cutpoints["unusual"]:
        return "unusual"
    if score >= cutpoints["elevated"]:
        return "elevated"
    return "normal"


def _quantiles(scores) -> Dict[str, float]:
    import numpy as np
    arr = np.asarray(scores, dtype=float)
    return {"p50": float(np.quantile(arr, 0.5)), "p90": float(np.quantile(arr, 0.9)),
            "p99": float(np.quantile(arr, 0.99))}


def _spearman(a, b) -> Optional[float]:
    import numpy as np
    if len(a) < 3:
        return None
    try:
        from scipy.stats import spearmanr
        rho = spearmanr(a, b).correlation
        return None if rho is None or np.isnan(rho) else round(float(rho), 4)
    except Exception:
        return None


def _matrix(rows: List[Dict[str, Any]], feature_names: List[str], medians: Dict[str, float]):
    """Impute exactly as training and inference do: a missing feature takes
    the ARTIFACT's training median for that feature."""
    import numpy as np
    from backend.ml.registry_service import preprocess_feature_vector
    contract = {"feature_names": feature_names, "imputation_medians": medians}
    out = [preprocess_feature_vector(contract, row["features"])[0] for row in rows]
    return np.array(out) if out else np.empty((0, len(feature_names)))


def not_applicable(reason: str, **extra) -> Dict[str, Any]:
    out = {"status": "NOT_APPLICABLE", "reason": reason,
           "promotion_decision": "REQUIRES_VALIDATION"}
    out.update(extra)
    return out


def compare_with_incumbent(*, candidate_payload: Dict[str, Any],
                           candidate_meta: Dict[str, Any],
                           incumbent_row, incumbent_payload: Optional[Dict[str, Any]],
                           rows_by_split: Dict[str, List[Dict[str, Any]]]) -> Dict[str, Any]:
    """candidate_meta: {model_type, model_purpose, score_type, feature_set_version,
    artifact_size_bytes}. incumbent_payload is the VALIDATED artifact of the
    incumbent (None when it could not be loaded — reported, not guessed)."""
    from backend.ml.registry_service import score_with_payload

    if incumbent_row is None:
        return not_applicable("no incumbent model in shadow for this model type")
    incompatible = {
        field: {"candidate": candidate_meta.get(field), "incumbent": getattr(incumbent_row, field, None)}
        for field in COMPATIBILITY_FIELDS
        if candidate_meta.get(field) != getattr(incumbent_row, field, None)}
    if incompatible:
        return not_applicable("candidate and incumbent answer different questions or use "
                              "different score semantics / feature contracts",
                              incompatible_fields=incompatible,
                              incumbent_model_id=str(incumbent_row.id),
                              incumbent_version=incumbent_row.version)
    if incumbent_payload is None:
        return not_applicable("incumbent artifact could not be validated/loaded",
                              incumbent_model_id=str(incumbent_row.id),
                              incumbent_version=incumbent_row.version)

    cand_names = list(candidate_payload["feature_names"])
    inc_names = list(incumbent_payload["feature_names"])
    feature_compat = {
        "shared": sorted(set(cand_names) & set(inc_names)),
        "candidate_only": sorted(set(cand_names) - set(inc_names)),
        "incumbent_only": sorted(set(inc_names) - set(cand_names)),
    }

    report: Dict[str, Any] = {
        "status": "computed",
        "promotion_decision": "REQUIRES_VALIDATION",
        "note": ("descriptive comparison on identical rows; both models score their "
                 "own feature contract with their own training medians and are "
                 "banded by their own training cutpoints; no criterion here promotes"),
        "incumbent": {"model_id": str(incumbent_row.id), "version": incumbent_row.version,
                      "algorithm": incumbent_row.algorithm,
                      "artifact_size_bytes": incumbent_row.artifact_size_bytes},
        "candidate": {"algorithm": candidate_payload.get("algorithm"),
                      "artifact_size_bytes": candidate_meta.get("artifact_size_bytes")},
        "feature_compatibility": feature_compat,
        "splits": {},
    }
    for split_name, rows in rows_by_split.items():
        if not rows:
            report["splits"][split_name] = {"rows": 0, "insufficient_data": True}
            continue
        cand_m = _matrix(rows, cand_names, candidate_payload["imputation_medians"])
        inc_m = _matrix(rows, inc_names, incumbent_payload["imputation_medians"])
        cand_scores, inc_scores = [], []
        failures = {"candidate": 0, "incumbent": 0}
        t0 = time.monotonic()
        try:
            cand_scores = list(score_with_payload(candidate_payload, cand_m))
        except Exception as e:  # scoring failure is a finding, not a crash
            failures["candidate"] = len(rows)
            logger.warning("[ML_OPS] candidate scoring failed on %s: %s", split_name, e)
        t1 = time.monotonic()
        try:
            inc_scores = list(score_with_payload(incumbent_payload, inc_m))
        except Exception as e:
            failures["incumbent"] = len(rows)
            logger.warning("[ML_OPS] incumbent scoring failed on %s: %s", split_name, e)
        t2 = time.monotonic()
        entry: Dict[str, Any] = {
            "rows": len(rows),
            "failure_rate": {k: round(v / len(rows), 4) for k, v in failures.items()},
            "latency_ms_per_row": {
                "candidate": round((t1 - t0) * 1000.0 / len(rows), 4),
                "incumbent": round((t2 - t1) * 1000.0 / len(rows), 4)},
        }
        if cand_scores and inc_scores:
            cand_bands = [_band_for(s, candidate_payload["band_cutpoints"]) for s in cand_scores]
            inc_bands = [_band_for(s, incumbent_payload["band_cutpoints"]) for s in inc_scores]
            entry.update({
                "score_quantiles": {"candidate": _quantiles(cand_scores),
                                    "incumbent": _quantiles(inc_scores)},
                "band_counts": {"candidate": {b: cand_bands.count(b) for b in ANOMALY_BANDS},
                                "incumbent": {b: inc_bands.count(b) for b in ANOMALY_BANDS}},
                "rank_agreement_spearman": _spearman(cand_scores, inc_scores),
                "band_agreement_rate": round(
                    sum(1 for a, b in zip(cand_bands, inc_bands) if a == b) / len(rows), 4),
            })
        report["splits"][split_name] = entry
    return report


async def load_incumbent(db, model_type: str):
    """(row, validated payload | None) for the model in SHADOW of this type."""
    from backend.ml.registry_service import registry_service, validate_artifact
    row = await registry_service.get_stage_model(db, model_type, "shadow")
    if row is None:
        return None, None
    try:
        payload = validate_artifact(
            row.artifact_path, expected_hash=row.artifact_hash,
            expected_feature_names=list(row.feature_names or []),
            expected_dependencies=row.dependency_versions)
    except Exception as e:
        logger.warning("[ML_OPS] incumbent %s artifact refused: %s", row.id, e)
        payload = None
    return row, payload


def dataset_drift_vs_incumbent(candidate_rows: List[Dict[str, Any]],
                               incumbent_rows: List[Dict[str, Any]],
                               feature_names: List[str]) -> Dict[str, Any]:
    """PSI per feature between the candidate dataset and the dataset the
    incumbent was trained on — an observation recorded on the candidate
    dataset's quality report. It never triggers anything."""
    from backend.ml.drift_service import psi
    if not candidate_rows or not incumbent_rows:
        return {"status": "NOT_APPLICABLE",
                "reason": "one of the datasets has no rows"}
    out: Dict[str, Any] = {"status": "computed", "features": {},
                           "candidate_rows": len(candidate_rows),
                           "incumbent_rows": len(incumbent_rows),
                           "note": "observation only; drift never triggers retraining"}
    worst = None
    for name in feature_names:
        a = [float(r["features"][name]) for r in incumbent_rows if name in r["features"]]
        b = [float(r["features"][name]) for r in candidate_rows if name in r["features"]]
        value = psi(a, b) if a and b else None
        out["features"][name] = {"psi": value, "baseline_n": len(a), "current_n": len(b)}
        if value is not None and (worst is None or value > worst):
            worst = value
    out["worst_psi"] = worst
    return out


# ---------------------------------------------------------------------------
# Shadow evidence — the offline dataset a human uses to judge a signal mapping
# ---------------------------------------------------------------------------

async def shadow_evidence_report(db, *, days: int = 90, model_id: Optional[str] = None) -> Dict[str, Any]:
    """Everything SHADOW has collected, organised for offline review of a
    future ML->risk signal mapping: per model version and anomaly band, how
    many predictions exist, how many carry a REVIEWED manual outcome, and
    how those outcomes split; the rule-severity x band crosstab; the
    operational-disagreement mix; and the score quantiles of positive vs
    negative reviewed outcomes. Descriptive only. The repository has no
    validated mapping, so `mapping_decision` is always REQUIRES_VALIDATION
    and no coverage floor is invented — the counts are shown and the
    reviewer judges whether they are enough."""
    import uuid as uuid_mod
    from datetime import datetime, timedelta
    from sqlalchemy import select
    from db_models import MLLabel, MLModel, MLPrediction, MLShadowComparison

    from backend.ml import evidence_grade

    floor = datetime.utcnow() - timedelta(days=days)
    query = (select(MLPrediction, MLLabel)
             .outerjoin(MLLabel, MLLabel.id == MLPrediction.outcome_label_id)
             .where(MLPrediction.created_at >= floor,
                    MLPrediction.behavioral_anomaly_score.isnot(None),
                    MLPrediction.ml_anomaly_band.isnot(None)))
    if model_id:
        try:
            query = query.where(MLPrediction.model_id == uuid_mod.UUID(str(model_id)))
        except (ValueError, TypeError):
            return {"status": "failed", "code": "INVALID_MODEL_ID"}
    pred_rows = (await db.execute(query.order_by(MLPrediction.created_at.desc()).limit(50000))).all()

    # Comparisons: analytically ONE per prediction (the earliest). Historical
    # duplicate rows are never deleted; they are counted and reported as a
    # data-quality fact instead of inflating every statistic through the join.
    comparison_of: Dict[Any, Any] = {}
    duplicate_comparisons = 0
    if pred_rows:
        ids = [pred.id for pred, _ in pred_rows]
        cmp_rows = (await db.execute(
            select(MLShadowComparison)
            .where(MLShadowComparison.prediction_id.in_(ids))
            .order_by(MLShadowComparison.prediction_id, MLShadowComparison.created_at))).scalars().all()
        for cmp in cmp_rows:
            if cmp.prediction_id in comparison_of:
                duplicate_comparisons += 1
                continue
            comparison_of[cmp.prediction_id] = cmp
    rows = [(pred, comparison_of.get(pred.id), label) for pred, label in pred_rows]

    report: Dict[str, Any] = {
        "status": "ok",
        "window_days": days,
        "mapping_decision": "REQUIRES_VALIDATION",
        "evidence_grade_definition": evidence_grade.DEFINITION_TEXT,
        "note": ("descriptive evidence only: reviewed outcomes are analyst decisions about "
                 "assessments, not ground truth about anomaly; rule severity and anomaly "
                 "bands are different concepts; no score delta and no threshold is derived"),
        "predictions": len(rows),
        "truncated": len(rows) >= 50000,
        "data_quality": {
            "duplicate_comparisons": duplicate_comparisons,
            "note": ("duplicate comparison rows are historical and left in place; the report "
                     "uses the earliest comparison per prediction"),
        },
        # every linked label lands in exactly one population; predictions
        # without any label are 'unreviewed' — populations are never mixed
        "populations": evidence_grade.empty_population_counts(),
        "excluded_non_evidence_labels": {},
        "models": {},
    }
    by_model: Dict[str, Dict[str, Any]] = {}
    for pred, cmp, label in rows:
        key = str(pred.model_id) if pred.model_id else "unknown"
        entry = by_model.setdefault(key, {
            "model_id": key, "model_version_label": pred.model_version_label,
            "threshold_versions": set(),
            "predictions": 0, "with_reviewed_outcome": 0,
            "bands": {}, "rule_severity_x_band": {}, "operational_disagreement": {},
            "scores": {"positive": [], "negative": [], "all": []},
            "_reviewed_rows": [], "_selection_methods": {},
            "populations": evidence_grade.empty_population_counts(),
        })
        entry["predictions"] += 1
        # Raw-source retention: identity deletion cascades the raw appearances
        # and SET NULLs person_id; the prediction, its immutable snapshot and
        # any outcome survive (audit reproducibility) but the features can no
        # longer be recomputed from source events (raw recomputability).
        if pred.subject_type == "identity" and pred.person_id is None:
            entry["source_events_not_retained"] = entry.get("source_events_not_retained", 0) + 1
        population = evidence_grade.population_of(label)
        entry["populations"][population] += 1
        report["populations"][population] += 1
        if label is not None and evidence_grade.is_non_evidence_source(label.source):
            src = str(label.source)[:64]
            report["excluded_non_evidence_labels"][src] = report["excluded_non_evidence_labels"].get(src, 0) + 1
        if pred.threshold_version:
            entry["threshold_versions"].add(pred.threshold_version)
        band = pred.ml_anomaly_band
        b = entry["bands"].setdefault(band, {"n": 0, "with_reviewed_outcome": 0,
                                              "positive": 0, "negative": 0, "unknown": 0})
        b["n"] += 1
        entry["scores"]["all"].append(float(pred.behavioral_anomaly_score))
        reviewed = evidence_grade.is_evidence_grade(label)
        if reviewed:
            entry["with_reviewed_outcome"] += 1
            b["with_reviewed_outcome"] += 1
            b[label.label] = b.get(label.label, 0) + 1
            if label.label in ("positive", "negative"):
                entry["scores"][label.label].append(float(pred.behavioral_anomaly_score))
                entry["_reviewed_rows"].append({"band": band, "outcome": label.label,
                                                "score": float(pred.behavioral_anomaly_score)})
                sel = getattr(label, "selection", None) or {}
                method = str(sel.get("method") or "natural") if isinstance(sel, dict) else "natural"
                entry["_selection_methods"][method] = entry["_selection_methods"].get(method, 0) + 1
                # recorded blind vs revealed (independent of who reviewed it)
                revealed = sel.get("ml_observation_revealed") if isinstance(sel, dict) else None
                key_rv = "_recorded_revealed" if revealed is True else "_recorded_blind"
                entry[key_rv] = entry.get(key_rv, 0) + 1
        if cmp is not None:
            sev = cmp.rule_threat_severity or "unknown"
            xb = entry["rule_severity_x_band"].setdefault(sev, {})
            xb[band] = xb.get(band, 0) + 1
            od = cmp.operational_disagreement or "unknown"
            entry["operational_disagreement"][od] = entry["operational_disagreement"].get(od, 0) + 1

    from backend.ml import evidence_stats
    for key, entry in by_model.items():
        for band, b in entry["bands"].items():
            b["positive_share_of_reviewed"] = (round(b["positive"] / b["with_reviewed_outcome"], 4)
                                               if b["with_reviewed_outcome"] else None)
        # ---- statistical evidence (reviewed outcomes only; unreviewed = unknown)
        reviewed_rows = entry.pop("_reviewed_rows")
        selection_methods = entry.pop("_selection_methods")
        table = evidence_stats.band_table(reviewed_rows)
        prediction_counts = {band: b["n"] for band, b in entry["bands"].items()}
        cov = evidence_stats.coverage(prediction_counts, table)
        entry["evidence"] = {
            "bands": table,
            "coverage": cov,
            "adequacy": evidence_stats.adequacy(cov),
            "monotonicity_trend": evidence_stats.cochran_armitage_trend(table),
            "spearman_score_vs_outcome": evidence_stats.spearman_score_outcome(reviewed_rows),
            "ranking": evidence_stats.ranking_metrics(reviewed_rows),
            "band_separation": evidence_stats.band_separation_note(table),
            "review_selection_methods": selection_methods,
            "populations": entry["populations"],
            # population split (each label in exactly one; self-review wins)
            "blind_reviewed": entry["populations"][evidence_grade.POP_BLIND_REVIEWED],
            "revealed_reviewed": entry["populations"][evidence_grade.POP_REVEALED_REVIEWED],
            "self_reviewed": entry["populations"][evidence_grade.POP_SELF_REVIEWED],
            # how evidence-grade outcomes were RECORDED (blind vs revealed),
            # regardless of reviewer identity
            "recorded_blind": entry.pop("_recorded_blind", 0),
            "recorded_revealed": entry.pop("_recorded_revealed", 0),
            "sampling_caveat": ("reviewed outcomes under any method other than 'natural' are a "
                                "stratified subset: positive rates are conditional on selection, "
                                "not population prevalence" if any(m != "natural" for m in selection_methods)
                                else "reviews recorded without explicit selection metadata (treated as natural)"),
            "note": ("statistics with sample sizes; INSUFFICIENT_SAMPLE means the inputs cannot "
                     "support the statistic; none of these is a product decision"),
        }
        scores = entry.pop("scores")
        entry["score_quantiles"] = {
            name: (_quantiles(vals) if vals else None) for name, vals in scores.items()}
        entry["reviewed_outcome_coverage"] = (round(entry["with_reviewed_outcome"] / entry["predictions"], 4)
                                              if entry["predictions"] else None)
        entry["source_retention"] = {
            "predictions_with_deleted_subject": entry.pop("source_events_not_retained", 0),
            "audit_reproducibility": "prediction, snapshot features, comparison and outcome rows are immutable "
                                     "and survive identity deletion",
            "raw_recomputability": "NOT available for a deleted subject: its raw appearances are deleted with "
                                   "the identity, so features cannot be recomputed from source events",
        }
        entry["threshold_versions"] = sorted(entry["threshold_versions"])
        report["models"][key] = entry

    # registry context for each model referenced (version/stage/algorithm), path-free
    if by_model:
        ids = [uuid_mod.UUID(k) for k in by_model if k != "unknown"]
        if ids:
            regs = (await db.execute(select(MLModel).where(MLModel.id.in_(ids)))).scalars().all()
            for reg in regs:
                report["models"][str(reg.id)]["registry"] = {
                    "version": reg.version, "stage": reg.stage, "algorithm": reg.algorithm,
                    "feature_set_version": reg.feature_set_version,
                    "dataset_id": str(reg.dataset_id) if reg.dataset_id else None}
    return report

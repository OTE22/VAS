"""
Dataset validation — fail-safe, structured, machine + human readable.

The report shape mirrors the similarity system's readiness gates:
{passed, checks: {name: {passed, current, required, detail}}, warnings}.
Training refuses to proceed when passed is False. Leakage checks are hard
failures, never warnings: a target_adjacent feature in a supervised set, or
an example whose features were computed AFTER its label's event time, kills
the build.
"""

import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

MAX_NULL_RATE = 0.5          # a feature missing in >50% of rows is unusable
MIN_ROWS_UNSUPERVISED = 10
MIN_ROWS_SUPERVISED = 50     # additionally gated by reviewed-label minimums
MIN_MINORITY_RATIO = 0.1


def _check(current, required, passed, detail=None) -> Dict[str, Any]:
    out = {"passed": bool(passed), "current": current, "required": required}
    if detail:
        out["detail"] = detail
    return out


def validate_rows(rows: List[Dict[str, Any]], *, kind: str,
                  definitions: List[Dict[str, Any]],
                  now: Optional[datetime] = None) -> Dict[str, Any]:
    """rows: [{entity_id, as_of (datetime), features {name: value},
    unavailable {name: reason}, label?, label_event_time?}]"""
    now = now or datetime.utcnow()
    checks: Dict[str, Dict[str, Any]] = {}
    warnings: List[str] = []

    min_rows = MIN_ROWS_SUPERVISED if kind == "supervised" else MIN_ROWS_UNSUPERVISED
    checks["minimum_rows"] = _check(len(rows), min_rows, len(rows) >= min_rows)

    # Schema: every row's feature keys must be defined; no undefined extras.
    defined = {d["name"] for d in definitions}
    undefined_seen = set()
    for row in rows:
        undefined_seen |= set(row.get("features", {})) - defined
    checks["schema_known_features"] = _check(
        sorted(undefined_seen)[:5], [], not undefined_seen,
        detail="feature keys not present in active definitions")

    # Feature-less rows are not training data. Definitions are migration-seeded,
    # so an empty features dict is a configuration defect, never a valid row.
    empty_rows = sum(1 for row in rows if not row.get("features"))
    checks["no_empty_feature_rows"] = _check(
        empty_rows, 0, empty_rows == 0,
        detail="rows whose feature dict is empty (no active definitions were computed)")

    # Duplicates on (entity_id, as_of)
    seen = set()
    duplicates = 0
    for row in rows:
        key = (row["entity_id"], row["as_of"])
        if key in seen:
            duplicates += 1
        seen.add(key)
    checks["no_duplicates"] = _check(duplicates, 0, duplicates == 0)

    # Impossible timestamps
    future_rows = sum(1 for row in rows if row["as_of"] > now)
    checks["no_future_as_of"] = _check(future_rows, 0, future_rows == 0)

    # Null / unavailable rates per SAFE feature. Missingness is HONEST data
    # at bootstrap scale (sparse-history features are legitimately
    # unavailable), so:
    #  - supervised: >MAX_NULL_RATE is a HARD failure (a model trained on a
    #    mostly-absent feature is fiction);
    #  - unsupervised: rates are reported + warned; the trainer decides which
    #    features carry enough coverage (missingness handling is part of the
    #    anomaly design, never papered over here).
    safe_definitions = [d for d in definitions if d.get("leakage_class", "safe") == "safe"]
    if rows:
        worst_feature, worst_rate = None, 0.0
        for definition in safe_definitions:
            name = definition["name"]
            missing = sum(
                1 for row in rows if name not in (row.get("features") or {}))
            rate = missing / len(rows)
            if rate > worst_rate:
                worst_feature, worst_rate = name, rate
            if rate > 0.2:
                warnings.append(f"{name} unavailable in {rate:.0%} of rows")
        hard = kind == "supervised"
        checks["max_null_rate"] = _check(
            {"feature": worst_feature, "rate": round(worst_rate, 3),
             "enforced": hard},
            MAX_NULL_RATE,
            (worst_rate <= MAX_NULL_RATE) if hard else True,
            detail=None if hard else "reported only for unsupervised datasets")
    else:
        checks["max_null_rate"] = _check(None, MAX_NULL_RATE, False, "no rows")

    # Value ranges: ratios must live in [0, 1]
    out_of_range = 0
    for row in rows:
        for name, value in (row.get("features") or {}).items():
            if name.endswith("_ratio") or name.endswith("_ratio_30d"):
                if value is not None and not (0.0 <= float(value) <= 1.0):
                    out_of_range += 1
    checks["ratio_ranges"] = _check(out_of_range, 0, out_of_range == 0)

    # LEAKAGE (hard failures)
    target_adjacent = sorted({
        d["name"] for d in definitions if d.get("leakage_class") != "safe"})
    if kind == "supervised":
        leaked_features = [
            name for name in target_adjacent
            if any(name in (row.get("features") or {}) for row in rows)]
        checks["no_target_adjacent_features"] = _check(
            leaked_features, [], not leaked_features,
            detail="target_adjacent features are banned from supervised datasets")
        temporal_leaks = sum(
            1 for row in rows
            if row.get("label_event_time") is not None
            and row["as_of"] > row["label_event_time"])
        checks["point_in_time_label_anchor"] = _check(
            temporal_leaks, 0, temporal_leaks == 0,
            detail="features must be computed at or before the labeled event")

        labels = [row.get("label") for row in rows if row.get("label") in ("positive", "negative")]
        positives = sum(1 for l in labels if l == "positive")
        negatives = len(labels) - positives
        minority = min(positives, negatives)
        ratio = (minority / len(labels)) if labels else 0.0
        checks["class_balance"] = _check(
            {"positive": positives, "negative": negatives, "ratio": round(ratio, 3)},
            {"minimum_ratio": MIN_MINORITY_RATIO}, ratio >= MIN_MINORITY_RATIO)

    passed = all(c["passed"] for c in checks.values())
    report = {
        "passed": passed,
        "kind": kind,
        "row_count": len(rows),
        "checks": checks,
        "warnings": warnings,
        "validated_at": now.isoformat() + "Z",
    }
    if not passed:
        failed = [name for name, c in checks.items() if not c["passed"]]
        report["failed_checks"] = failed
        logger.warning("[ML_OPS] dataset validation FAILED: %s", failed)
    return report

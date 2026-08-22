"""
Evidence statistics for the shadow model — numbers with their uncertainty.

Every function here returns a statistic WITH its sample size and, when the
inputs cannot support the statistic mathematically, the literal status
INSUFFICIENT_SAMPLE instead of a number. Nothing here decides anything:
whether the evidence is "enough" is a policy question answered elsewhere
(configured minimums), never by a p-value.

Only REVIEWED manual outcomes are evidence. Unreviewed predictions are
unknown, never negative.
"""

import math
from typing import Any, Dict, List, Optional, Sequence

from backend.ml.constants import ANOMALY_BANDS

INSUFFICIENT = "INSUFFICIENT_SAMPLE"
Z95 = 1.959963984540054


def wilson_interval(positive: int, n: int, z: float = Z95) -> Optional[Dict[str, float]]:
    if n <= 0:
        return None
    p = positive / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return {"low": round(max(0.0, centre - half), 4), "high": round(min(1.0, centre + half), 4)}


def band_table(reviewed: Sequence[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """reviewed: [{band, outcome('positive'|'negative'), score}] — reviewed rows only."""
    out: Dict[str, Dict[str, Any]] = {}
    for band in ANOMALY_BANDS:
        rows = [r for r in reviewed if r.get("band") == band]
        pos = sum(1 for r in rows if r.get("outcome") == "positive")
        neg = sum(1 for r in rows if r.get("outcome") == "negative")
        n = pos + neg
        out[band] = {
            "reviewed_count": n, "positive_count": pos, "negative_count": neg,
            "positive_rate": round(pos / n, 4) if n else None,
            "wilson_95": wilson_interval(pos, n) if n else None,
        }
    return out


def cochran_armitage_trend(table: Dict[str, Dict[str, Any]],
                           order: Sequence[str] = ANOMALY_BANDS) -> Dict[str, Any]:
    """Trend of positive rate across ordered bands (scores 0..k-1). Valid only
    with >= 2 bands that carry reviewed outcomes and both outcomes present
    overall; otherwise INSUFFICIENT_SAMPLE."""
    bands = [b for b in order if table.get(b, {}).get("reviewed_count", 0) > 0]
    total = sum(table[b]["reviewed_count"] for b in bands)
    positives = sum(table[b]["positive_count"] for b in bands)
    if len(bands) < 2 or positives == 0 or positives == total:
        return {"status": INSUFFICIENT, "bands_with_reviews": len(bands), "reviewed_total": total,
                "positives": positives}
    scores = {b: float(i) for i, b in enumerate(order)}
    n_i = [table[b]["reviewed_count"] for b in bands]
    r_i = [table[b]["positive_count"] for b in bands]
    t_i = [scores[b] for b in bands]
    p_bar = positives / total
    t_bar = sum(t * n for t, n in zip(t_i, n_i)) / total
    num = sum(t * (r - n * p_bar) for t, r, n in zip(t_i, r_i, n_i))
    var = p_bar * (1 - p_bar) * sum(n * (t - t_bar) ** 2 for t, n in zip(t_i, n_i))
    if var <= 0:
        return {"status": INSUFFICIENT, "reason": "no variance across bands", "reviewed_total": total}
    z = num / math.sqrt(var)
    p_two_sided = 2 * (1 - _norm_cdf(abs(z)))
    return {"status": "computed", "z": round(z, 4), "p_value": round(p_two_sided, 6),
            "direction": "increasing" if z > 0 else "decreasing" if z < 0 else "flat",
            "reviewed_total": total, "bands_with_reviews": bands,
            "note": "tests a monotone trend in positive rate across ordered bands; not a decision"}


def _norm_cdf(x: float) -> float:
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def spearman_score_outcome(reviewed: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """Spearman between continuous anomaly score and the binary reviewed outcome."""
    pairs = [(float(r["score"]), 1.0 if r.get("outcome") == "positive" else 0.0)
             for r in reviewed if r.get("score") is not None and r.get("outcome") in ("positive", "negative")]
    n = len(pairs)
    if n < 3 or len({o for _, o in pairs}) < 2 or len({s for s, _ in pairs}) < 2:
        return {"status": INSUFFICIENT, "n": n}
    try:
        from scipy.stats import spearmanr
        res = spearmanr([s for s, _ in pairs], [o for _, o in pairs])
        rho = float(res.correlation)
        p = float(res.pvalue)
        if math.isnan(rho):
            return {"status": INSUFFICIENT, "n": n}
        return {"status": "computed", "rho": round(rho, 4), "p_value": round(p, 6), "n": n}
    except Exception:
        return {"status": INSUFFICIENT, "n": n, "reason": "scipy unavailable"}


def ranking_metrics(reviewed: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """ROC-AUC, PR-AUC (average precision), precision@k and lift@k for the
    highest-scoring k% of REVIEWED observations. Suppressed (INSUFFICIENT_SAMPLE)
    unless both outcomes are present with at least two cases each; even then
    the sample size is attached — read n before the number."""
    rows = [(float(r["score"]), 1 if r.get("outcome") == "positive" else 0)
            for r in reviewed if r.get("score") is not None and r.get("outcome") in ("positive", "negative")]
    n = len(rows)
    pos = sum(o for _, o in rows)
    neg = n - pos
    if pos < 2 or neg < 2:
        return {"status": INSUFFICIENT, "n": n, "positives": pos, "negatives": neg}
    try:
        from sklearn.metrics import average_precision_score, roc_auc_score
        scores = [s for s, _ in rows]
        outcomes = [o for _, o in rows]
        out: Dict[str, Any] = {
            "status": "computed", "n": n, "positives": pos, "negatives": neg,
            "prevalence_in_reviewed": round(pos / n, 4),
            "roc_auc": round(float(roc_auc_score(outcomes, scores)), 4),
            "pr_auc": round(float(average_precision_score(outcomes, scores)), 4),
            "note": ("PR-AUC is the primary metric (positives are rare); the baseline PR-AUC "
                     "equals the prevalence. Reviewed rows only; if reviews were stratified "
                     "the prevalence is NOT the population prevalence."),
        }
        ordered = sorted(rows, key=lambda x: -x[0])
        base_rate = pos / n
        for pct in (5, 10):
            k = max(1, int(math.ceil(n * pct / 100.0)))
            top = ordered[:k]
            precision = sum(o for _, o in top) / k
            out[f"precision_at_top_{pct}_pct"] = round(precision, 4)
            out[f"lift_at_top_{pct}_pct"] = round(precision / base_rate, 4) if base_rate > 0 else None
            out[f"k_at_top_{pct}_pct"] = k
        return out
    except Exception as exc:
        return {"status": INSUFFICIENT, "n": n, "reason": f"metric library unavailable: {exc}"[:120]}


def coverage(prediction_counts: Dict[str, int], table: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    total_pred = sum(prediction_counts.values())
    total_rev = sum(v["reviewed_count"] for v in table.values())
    return {
        "predictions_total": total_pred,
        "reviewed_total": total_rev,
        "review_coverage_overall": round(total_rev / total_pred, 4) if total_pred else None,
        "reviewed_per_band": {b: table[b]["reviewed_count"] for b in ANOMALY_BANDS},
        "predictions_per_band": {b: int(prediction_counts.get(b, 0)) for b in ANOMALY_BANDS},
        "review_coverage_per_band": {
            b: (round(table[b]["reviewed_count"] / prediction_counts[b], 4)
                if prediction_counts.get(b) else None) for b in ANOMALY_BANDS},
    }


def adequacy(cov: Dict[str, Any]) -> Dict[str, Any]:
    """Compare coverage with CONFIGURED minimums (settings, default 0 = not
    configured). Never invents a number."""
    from backend.ml.readiness import _configured
    min_total = _configured("ML_EVIDENCE_MIN_REVIEWED_TOTAL")
    min_band = _configured("ML_EVIDENCE_MIN_REVIEWED_PER_BAND")
    if min_total is None and min_band is None:
        return {"status": "NOT_CONFIGURED",
                "detail": "no reviewed-outcome minimums configured (ML_EVIDENCE_MIN_REVIEWED_TOTAL / "
                          "ML_EVIDENCE_MIN_REVIEWED_PER_BAND); evidence is reported, not judged"}
    short = []
    if min_total is not None and (cov.get("reviewed_total") or 0) < min_total:
        short.append({"code": "REVIEWED_TOTAL_BELOW_MINIMUM", "actual": cov.get("reviewed_total"), "required": min_total})
    if min_band is not None:
        for band, n in (cov.get("reviewed_per_band") or {}).items():
            if (n or 0) < min_band:
                short.append({"code": "REVIEWED_PER_BAND_BELOW_MINIMUM", "band": band, "actual": n, "required": min_band})
    return {"status": "ADEQUATE" if not short else "INSUFFICIENT_EVIDENCE", "shortfalls": short,
            "configured": {"min_reviewed_total": min_total, "min_reviewed_per_band": min_band}}


def band_separation_note(table: Dict[str, Dict[str, Any]]) -> str:
    """Plain-language reminder: four bands are a display convention, not four
    validated risk groups. Separation must be shown, not assumed."""
    rates = [(b, table[b]["positive_rate"], table[b]["reviewed_count"]) for b in ANOMALY_BANDS]
    known = [(b, r, n) for b, r, n in rates if r is not None]
    if len(known) < 2:
        return ("fewer than two bands carry reviewed outcomes; no statement about separation "
                "between bands is possible")
    return ("four bands are a display convention; operational weights require empirical "
            "separation and ordering between bands (see trend test and per-band intervals) — "
            "evidence may support fewer effective groups")

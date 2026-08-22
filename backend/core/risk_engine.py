"""
Unified Risk Engine
===================
ONE scoring service for every risk number on the security page. The three
rubrics that used to live inline (threat assessment, network node risk, map
movement risk) are now PROFILES of this engine: same 0-100 range, same
severity bands, DB-configurable weights (``risk_model_versions``), one
version string, one honesty contract.

Severity bands (unified):
    0-24   low
    25-49  moderate
    50-74  high
    75-100 critical

Honesty contract (returned on every result):
    score_type="heuristic", is_probability=False,
    calibration_status="uncalibrated"
A score of 80 is NOT an 80% probability of anything — it is a weighted
heuristic. The calibration interface (risk_model_versions.calibration_*)
exists for a FUTURE validated calibration; nothing here fabricates one.

Weights: loaded from the active ``risk_model_versions`` row per profile
(cached ~60s), falling back to the seeded defaults compiled in below —
which mirror the previous hard-coded rubrics, so scores did not change at
cutover.
"""

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

RISK_ENGINE_VERSION = "risk-engine-v1"

SEVERITY_BANDS = (
    (75.0, "critical"),
    (50.0, "high"),
    (25.0, "moderate"),
    (0.0, "low"),
)

# Legacy names still used by pre-unification consumers (threat_level values
# the frontend already styles). moderate<->medium is the only rename.
LEGACY_SEVERITY = {"low": "low", "moderate": "medium", "high": "high", "critical": "critical"}

# Fallback weights when the DB is unreachable — identical to the seeded
# risk_model_versions rows (which mirror the previous hard-coded rubrics).
DEFAULT_MODELS: Dict[str, Dict[str, Any]] = {
    "identity_threat": {
        "weights": {
            "unknown_identity": 30.0,
            "connections_high": 25.0,
            "connections_moderate": 15.0,
            "anomaly_per_item": 10.0,
            "anomaly_cap": 30.0,
            "activity_high": 15.0,
        },
        "thresholds": {
            "connections_high": 10,
            "connections_moderate": 5,
            "activity_high": 100,
        },
    },
    "network_node": {
        "weights": {
            "unknown_identity": 30.0,
            "connections_high": 20.0,
            "connections_moderate": 10.0,
            "activity_high": 15.0,
            "activity_moderate": 8.0,
            "recent_activity_day": 10.0,
            "recent_activity_week": 5.0,
        },
        "thresholds": {
            "connections_high": 10,
            "connections_moderate": 5,
            "activity_high": 100,
            "activity_moderate": 50,
        },
    },
    "movement_map": {
        "weights": {
            "watchlist_cap": 50.0,
            "pattern_cap": 50.0,
            "zone_cap": 50.0,
            "speed_cap": 5.0,
            "base": 1.0,
            "scale": 5.0,
        },
        "thresholds": {},
    },
}

_MODEL_CACHE_TTL_SECONDS = 60.0


def severity_for(score: float) -> str:
    """Unified 0-100 severity mapping — the ONLY band map in the codebase."""
    score = max(0.0, min(100.0, float(score)))
    for floor, label in SEVERITY_BANDS:
        if score >= floor:
            return label
    return "low"


@dataclass
class Signal:
    """One contributing signal: score is the contribution (0..weight)."""
    name: str
    score: float
    weight: float
    raw_value: Any = None
    explanation: str = ""

    def as_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "score": round(self.score, 2),
            "weight": self.weight,
            "raw_value": self.raw_value,
            "explanation": self.explanation,
        }


@dataclass
class RiskResult:
    """The engine's full, honest output."""
    profile: str
    total_score: float
    severity: str
    signals: List[Signal]
    confidence: float
    model_version: str
    weights: Dict[str, float]
    explanation: str
    limitations: List[str] = field(default_factory=list)
    threshold_version: Optional[str] = None
    score_type: str = "heuristic"
    is_probability: bool = False
    calibration_status: str = "uncalibrated"
    computed_at: datetime = field(default_factory=datetime.utcnow)
    # Where the behavioral-anomaly INPUT came from: "rules" (statistical
    # detector) or "ml" (the anomaly model through a validated mapping). The
    # engine computing the score is this one either way.
    anomaly_signal_source: str = "rules"
    signal_mapping_version: Optional[str] = None

    @property
    def legacy_severity(self) -> str:
        return LEGACY_SEVERITY.get(self.severity, self.severity)

    def labeling(self) -> Dict[str, Any]:
        """The honesty fields every API response must carry."""
        return {
            "score_type": self.score_type,
            "is_probability": self.is_probability,
            "calibration_status": self.calibration_status,
            "model_version": self.model_version,
            "limitations": list(self.limitations),
            "anomaly_signal_source": self.anomaly_signal_source,
            "signal_mapping_version": self.signal_mapping_version,
        }

    def as_payload(self) -> Dict[str, Any]:
        payload = {
            "profile": self.profile,
            "total_score": round(self.total_score, 1),
            "severity": self.severity,
            "signals": [s.as_dict() for s in self.signals],
            "signal_scores": {s.name: round(s.score, 2) for s in self.signals},
            "weights": self.weights,
            "confidence": round(self.confidence, 3),
            "explanation": self.explanation,
            "threshold_version": self.threshold_version,
        }
        payload.update(self.labeling())
        return payload


class RiskEngine:
    """Loads per-profile weights (DB, cached) and turns signals into scores."""

    def __init__(self):
        self._cache: Dict[str, Tuple[float, Dict[str, Any], str]] = {}

    async def get_model(self, db: Optional[AsyncSession], profile: str) -> Tuple[Dict[str, Any], str]:
        """(model config, version). DB row wins; compiled defaults are the
        documented fallback (and identical to the seeded rows)."""
        cached = self._cache.get(profile)
        now = time.monotonic()
        if cached and now - cached[0] < _MODEL_CACHE_TTL_SECONDS:
            return cached[1], cached[2]

        config = dict(DEFAULT_MODELS.get(profile) or {"weights": {}, "thresholds": {}})
        version = RISK_ENGINE_VERSION
        if db is not None:
            try:
                from db_models import RiskModelVersion
                row = (await db.execute(
                    select(RiskModelVersion)
                    .where(RiskModelVersion.profile == profile,
                           RiskModelVersion.status == "active")
                    .order_by(RiskModelVersion.activated_at.desc().nullslast())
                    .limit(1)
                )).scalar_one_or_none()
                if row is not None:
                    config = {"weights": dict(row.weights or {}),
                              "thresholds": dict(row.thresholds or {})}
                    version = row.version
            except Exception as e:
                logger.warning("[RISK_ENGINE] model load failed for %s (%s) — using compiled defaults",
                               profile, e)
        self._cache[profile] = (now, config, version)
        return config, version

    def invalidate_cache(self) -> None:
        self._cache.clear()

    def peek_model(self, profile: str) -> Tuple[Dict[str, Any], str]:
        """Synchronous model access for sync call paths (map rendering,
        per-node batch fallbacks): cached DB config when a recent async load
        populated it, compiled defaults otherwise. Never touches the DB."""
        cached = self._cache.get(profile)
        if cached:
            return cached[1], cached[2]
        config = dict(DEFAULT_MODELS.get(profile) or {"weights": {}, "thresholds": {}})
        return config, RISK_ENGINE_VERSION

    def build_result(self, profile: str, signals: List[Signal], *,
                     model_version: str, weights: Dict[str, float],
                     confidence: float, explanation: str,
                     limitations: Optional[List[str]] = None,
                     threshold_version: Optional[str] = None) -> RiskResult:
        total = min(100.0, max(0.0, sum(s.score for s in signals)))
        lims = list(limitations or [])
        lims.append("Scores are weighted heuristics, not probabilities; "
                    "severity bands are operational triage labels.")
        return RiskResult(
            profile=profile,
            total_score=total,
            severity=severity_for(total),
            signals=signals,
            confidence=max(0.0, min(1.0, confidence)),
            model_version=model_version,
            weights=weights,
            explanation=explanation,
            limitations=lims,
            threshold_version=threshold_version,
        )

    # ------------------------------------------------------------------
    # Profile: identity_threat (full assessment — anomalies included)
    # ------------------------------------------------------------------
    async def score_identity_threat(
        self, db: Optional[AsyncSession], *,
        is_unknown: bool,
        connections_count: int,
        connections_source: str,
        anomaly_count: Optional[int],
        baseline_sufficient: bool,
        baseline_samples: int,
        real_appearance_count: int,
        threshold_version: Optional[str] = None,
        anomaly_signal=None,
    ) -> RiskResult:
        """anomaly_signal: an MLAnomalySignal (backend.ml.signal_mapping_service)
        when the decision router has a VALIDATED mapping and a healthy
        prediction — it replaces ONLY the behavioral-anomaly input; every
        other signal, cap, band and the score semantics are untouched and
        this engine still computes the score."""
        config, version = await self.get_model(db, "identity_threat")
        w, t = config["weights"], config["thresholds"]
        signals: List[Signal] = []

        if is_unknown:
            signals.append(Signal(
                "unknown_identity", w.get("unknown_identity", 30.0),
                w.get("unknown_identity", 30.0), raw_value=True,
                explanation="Identity is not in the known persons database"))

        conn_high = t.get("connections_high", 10)
        conn_moderate = t.get("connections_moderate", 5)
        if connections_count > conn_high:
            signals.append(Signal(
                "connections", w.get("connections_high", 25.0),
                w.get("connections_high", 25.0), raw_value=connections_count,
                explanation=f"Connected to {connections_count} other identities "
                            f"({connections_source}) — above the hub threshold ({conn_high})"))
        elif connections_count > conn_moderate:
            signals.append(Signal(
                "connections", w.get("connections_moderate", 15.0),
                w.get("connections_high", 25.0), raw_value=connections_count,
                explanation=f"Connected to {connections_count} other identities "
                            f"({connections_source})"))

        limitations: List[str] = []
        anomaly_cap = w.get("anomaly_cap", 30.0)
        if anomaly_signal is not None:
            points = max(0.0, min(float(anomaly_signal.points), anomaly_cap))
            signals.append(Signal(
                "behavioral_anomalies", points, anomaly_cap,
                raw_value={"band": anomaly_signal.band, "score": anomaly_signal.score,
                           "model": anomaly_signal.model_version_label,
                           "mapping": anomaly_signal.mapping_version},
                explanation=(f"ML behavioral-anomaly band {anomaly_signal.band} "
                             f"(model {anomaly_signal.model_version_label}) mapped by "
                             f"{anomaly_signal.mapping_version}; scored by {version}")))
        elif anomaly_count is not None and anomaly_count > 0:
            anomaly_score = min(anomaly_count * w.get("anomaly_per_item", 10.0),
                                w.get("anomaly_cap", 30.0))
            signals.append(Signal(
                "behavioral_anomalies", anomaly_score, w.get("anomaly_cap", 30.0),
                raw_value=anomaly_count,
                explanation=f"{anomaly_count} anomalies detected in the recent window"))
        elif not baseline_sufficient:
            signals.append(Signal(
                "behavioral_anomalies", 0.0, w.get("anomaly_cap", 30.0),
                raw_value={"baseline_samples": baseline_samples},
                explanation=(f"Insufficient baseline ({baseline_samples} samples) — "
                             "anomaly analysis not possible yet")))
            limitations.append("Anomaly signal not evaluated: insufficient behavioral baseline.")

        if real_appearance_count > t.get("activity_high", 100):
            signals.append(Signal(
                "activity_level", w.get("activity_high", 15.0),
                w.get("activity_high", 15.0), raw_value=real_appearance_count,
                explanation=f"{real_appearance_count} recorded appearances"))

        # Confidence: evidence-backed share of the rubric — anomaly baseline
        # present, and connections from the cache rank above a live estimate.
        confidence = 0.5
        if baseline_sufficient:
            confidence += 0.3
        if connections_source == "relationship_cache":
            confidence += 0.2
        explanation = (
            "Weighted heuristic over identity type, network connections, "
            "behavioral anomalies and activity level.")
        result = self.build_result(
            "identity_threat", signals, model_version=version, weights=w,
            confidence=confidence, explanation=explanation,
            limitations=limitations, threshold_version=threshold_version)
        if anomaly_signal is not None:
            result.anomaly_signal_source = "ml"
            result.signal_mapping_version = anomaly_signal.mapping_version
        return result

    # ------------------------------------------------------------------
    # Profile: network_node (batch node scoring — no anomaly evaluation)
    # ------------------------------------------------------------------
    def score_network_node_sync(
        self, config: Dict[str, Any], version: str, *,
        is_unknown: bool,
        connections: int,
        real_appearance_count: int,
        last_seen_at: Optional[datetime],
    ) -> RiskResult:
        """Synchronous variant for per-node batch scoring (the graph builder
        scores hundreds of nodes; the model config is loaded once by the
        caller via get_model)."""
        w, t = config["weights"], config["thresholds"]
        signals: List[Signal] = []
        if is_unknown:
            signals.append(Signal("unknown_identity", w.get("unknown_identity", 30.0),
                                  w.get("unknown_identity", 30.0), raw_value=True,
                                  explanation="Unknown identity"))
        if connections > t.get("connections_high", 10):
            signals.append(Signal("connections", w.get("connections_high", 20.0),
                                  w.get("connections_high", 20.0), raw_value=connections,
                                  explanation=f"{connections} graph connections (hub)"))
        elif connections > t.get("connections_moderate", 5):
            signals.append(Signal("connections", w.get("connections_moderate", 10.0),
                                  w.get("connections_high", 20.0), raw_value=connections,
                                  explanation=f"{connections} graph connections"))
        if real_appearance_count > t.get("activity_high", 100):
            signals.append(Signal("activity_level", w.get("activity_high", 15.0),
                                  w.get("activity_high", 15.0), raw_value=real_appearance_count,
                                  explanation=f"{real_appearance_count} appearances"))
        elif real_appearance_count > t.get("activity_moderate", 50):
            signals.append(Signal("activity_level", w.get("activity_moderate", 8.0),
                                  w.get("activity_high", 15.0), raw_value=real_appearance_count,
                                  explanation=f"{real_appearance_count} appearances"))
        if last_seen_at:
            days = (datetime.utcnow() - last_seen_at).days
            if days < 1:
                signals.append(Signal("recent_activity", w.get("recent_activity_day", 10.0),
                                      w.get("recent_activity_day", 10.0), raw_value=days,
                                      explanation="Seen within the last day"))
            elif days < 7:
                signals.append(Signal("recent_activity", w.get("recent_activity_week", 5.0),
                                      w.get("recent_activity_day", 10.0), raw_value=days,
                                      explanation="Seen within the last week"))
        return self.build_result(
            "network_node", signals, model_version=version, weights=w,
            confidence=0.6, explanation="Batch node heuristic (no anomaly evaluation).",
            limitations=["Anomaly signal not evaluated in batch graph scoring."])

    # ------------------------------------------------------------------
    # Profile: movement_map (map overlay risk)
    # ------------------------------------------------------------------
    def score_movement_sync(
        self, config: Dict[str, Any], version: str, *,
        watchlist_points: float,
        pattern_points: float,
        zone_points: float,
        speed_points: float,
    ) -> RiskResult:
        """Map overlay scoring. The legacy rubric summed capped point buckets
        (base 1, watchlist<=50, patterns<=50, zones<=50, speed<=5) and scaled
        by 5 into 0-100. Caps and scale live in the model config, so the
        historical numbers are reproduced exactly (build_result caps at 100).
        """
        w = config["weights"]
        scale = w.get("scale", 5.0)

        def contrib(points: float, cap_key: str, default_cap: float) -> float:
            return min(max(0.0, points), w.get(cap_key, default_cap)) * scale

        signals = [
            Signal("watchlist", contrib(watchlist_points, "watchlist_cap", 50.0),
                   w.get("watchlist_cap", 50.0) * scale,
                   raw_value=watchlist_points, explanation="Watchlist matches on the track"),
            Signal("patterns", contrib(pattern_points, "pattern_cap", 50.0),
                   w.get("pattern_cap", 50.0) * scale,
                   raw_value=pattern_points, explanation="Detected movement patterns"),
            Signal("zones", contrib(zone_points, "zone_cap", 50.0),
                   w.get("zone_cap", 50.0) * scale,
                   raw_value=zone_points, explanation="Security-zone intersections"),
            Signal("speed", contrib(speed_points, "speed_cap", 5.0),
                   w.get("speed_cap", 5.0) * scale,
                   raw_value=speed_points, explanation="Implausible speed segments"),
            Signal("base", w.get("base", 1.0) * scale, w.get("base", 1.0) * scale,
                   raw_value=None, explanation="Base movement-track risk"),
        ]
        return self.build_result(
            "movement_map", signals, model_version=version, weights=w,
            confidence=0.5,
            explanation="Map overlay heuristic over watchlist, patterns, zones and speed.",
            limitations=["Movement coordinates are camera positions, not GPS tracks."])


# Global instance
risk_engine = RiskEngine()

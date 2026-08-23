"""
ML -> risk-engine signal mapping POLICY — a separate, versioned artifact.

The behavioral-anomaly model answers "how unusual is this identity's
behaviour" (score in [0, 1], band). The risk engine consumes a
`behavioral_anomalies` contribution in risk points (capped by
`anomaly_cap`). How one becomes the other is an OPERATIONAL RISK POLICY,
not model output — and the repository holds no validated one. This module
is the slot such a policy lives in once a human has validated it:

    risk_model_versions row, profile = "ml_anomaly_signal_map"
        version             e.g. "ml-anomaly-map-v1"  (independent of model / threshold versions)
        status              "active" (the existing activation path)
        calibration_status  MUST be "validated"
        calibration_data    the evidence it was validated on (non-empty; see shadow evidence)
        weights             the policy payload:
                              {"kind": "band_points",
                               "band_points": {"normal": 0, "elevated": 10, ...},   # ≤ anomaly_cap
                               "scope": {"model_id": ..., "feature_set_version": ...,
                                         "threshold_version": ...}}
                            The SCOPE is mandatory: a mapping is validated for
                            one model, one feature set and one threshold set;
                            any of them changing invalidates it (no silent reuse).

`active_policy()` returns None unless ALL of that holds — an active but
uncalibrated row, or a row with no evidence, is not a policy. Nothing here
seeds a row: no values are invented; ML mode stays
SIGNAL_MAPPING_UNVALIDATED until an administrator activates a validated one.
"""

import logging
from dataclasses import dataclass
from typing import Any, Dict, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.ml.constants import ANOMALY_BANDS

logger = logging.getLogger(__name__)

MAPPING_PROFILE = "ml_anomaly_signal_map"
SUPPORTED_KINDS = ("band_points",)


@dataclass(frozen=True)
class MappingPolicy:
    version: str
    kind: str
    band_points: Dict[str, float]
    calibration_status: str
    scope: Dict[str, Optional[str]]


@dataclass(frozen=True)
class MLAnomalySignal:
    """What the risk engine receives when ML supplies the anomaly signal."""
    points: float                 # risk contribution, already capped by the policy
    band: str
    score: float
    mapping_version: str
    model_id: str
    model_version_label: str
    threshold_version: Optional[str]


class SignalMappingError(ValueError):
    pass


def validate_policy_payload(weights: Dict[str, Any], *, anomaly_cap: float) -> Dict[str, float]:
    if not isinstance(weights, dict):
        raise SignalMappingError("policy payload must be an object")
    kind = weights.get("kind")
    if kind not in SUPPORTED_KINDS:
        raise SignalMappingError(f"unsupported mapping kind {kind!r}")
    points = weights.get("band_points")
    if not isinstance(points, dict) or set(points) != set(ANOMALY_BANDS):
        raise SignalMappingError(f"band_points must cover exactly {ANOMALY_BANDS}")
    out = {}
    for band, value in points.items():
        try:
            value = float(value)
        except (TypeError, ValueError):
            raise SignalMappingError(f"band_points[{band}] is not a number")
        if value < 0 or value > anomaly_cap:
            raise SignalMappingError(f"band_points[{band}]={value} outside [0, anomaly_cap={anomaly_cap}]")
        out[band] = value
    return out


class SignalMappingService:

    async def active_policy(self, db: AsyncSession, *, anomaly_cap: float,
                            model_id: Optional[str] = None,
                            feature_set_version: Optional[str] = None,
                            threshold_version: Optional[str] = None) -> Optional[MappingPolicy]:
        """The policy that is active AND validated (non-empty calibration
        data) AND has a valid payload AND whose scope EXACTLY matches the
        given model / feature set / threshold set — newest such first — or
        None (the normal state today).

        No wildcard: every scope dimension must be known to the caller and
        must match. A caller that cannot name its threshold set (no active
        set) gets no policy. Other active rows of the profile never mask a
        valid one; each is examined and the reasons are logged."""
        from db_models import RiskModelVersion
        expected = {"model_id": model_id, "feature_set_version": feature_set_version,
                    "threshold_version": threshold_version}
        missing = [k for k, v in expected.items() if v in (None, "")]
        if missing:
            logger.info("[ML_OPS] signal mapping lookup without a full scope (%s) — no policy",
                        ", ".join(missing))
            return None
        rows = (await db.execute(
            select(RiskModelVersion)
            .where(RiskModelVersion.profile == MAPPING_PROFILE,
                   RiskModelVersion.status == "active")
            .order_by(RiskModelVersion.activated_at.desc().nullslast(),
                      RiskModelVersion.version.desc()))).scalars().all()
        for row in rows:
            reason = self._policy_rejection(row, expected, anomaly_cap=anomaly_cap)
            if reason is not None:
                logger.warning("[ML_OPS] signal mapping %s ignored: %s", row.version, reason)
                continue
            weights = dict(row.weights or {})
            scope = weights.get("scope")
            band_points = validate_policy_payload(weights, anomaly_cap=anomaly_cap)
            return MappingPolicy(version=str(row.version), kind="band_points",
                                 band_points=band_points, calibration_status=row.calibration_status,
                                 scope={k: str(scope.get(k)) for k in expected})
        return None

    @staticmethod
    def _policy_rejection(row, expected: Dict[str, Optional[str]], *, anomaly_cap: float) -> Optional[str]:
        """Why one active row is NOT the policy — None when it qualifies."""
        if row.calibration_status != "validated":
            return f"calibration_status={row.calibration_status!r} (validated required)"
        if not row.calibration_data:
            return "empty calibration_data"
        weights = dict(row.weights or {})
        try:
            validate_policy_payload(weights, anomaly_cap=anomaly_cap)
        except SignalMappingError as exc:
            return f"invalid payload: {exc}"
        scope = weights.get("scope") if isinstance(weights.get("scope"), dict) else None
        if not scope:
            return "no scope"
        for key, value in expected.items():
            if str(scope.get(key) or "") != str(value):
                return f"scope {key}={scope.get(key)!r} does not match current {value!r}"
        return None

    def apply(self, policy: MappingPolicy, prediction) -> MLAnomalySignal:
        """Turn a successful prediction into the engine's anomaly contribution."""
        band = prediction.ml_anomaly_band
        if band not in policy.band_points:
            raise SignalMappingError(f"band {band!r} not covered by {policy.version}")
        return MLAnomalySignal(
            points=float(policy.band_points[band]), band=band,
            score=float(prediction.behavioral_anomaly_score),
            mapping_version=policy.version,
            model_id=str(prediction.model_id), model_version_label=prediction.model_version_label,
            threshold_version=prediction.threshold_version)


signal_mapping_service = SignalMappingService()

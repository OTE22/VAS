"""
Decision-mode dispatch. RULES is the production decision system.

This release:
- rules  : the heuristic assessment, unchanged (default, production-safe).
- shadow : the LIVE result is the rules result, byte-for-byte; the anomaly
           model runs afterwards, bounded and swallow-all, persisting
           predictions + side-by-side comparisons that never touch the
           live decision.
- hybrid : GATED — resolves to rules; the exact unmet gates are recorded
           and returned on any activation attempt.
- ml     : GATED — same.

The mode-availability report is the single source of truth for the admin
UI and the mode-switch API: every unavailable mode carries the precise
reasons it cannot be activated yet.
"""

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from sqlalchemy.ext.asyncio import AsyncSession

from backend.ml.constants import MODEL_TYPE_BEHAVIOR_ANOMALY, MODES, FallbackReason
from config import settings

logger = logging.getLogger(__name__)

RELEASE_GATE_HYBRID = (
    "release gate: HYBRID requires an approved calibrated supervised model, "
    "an untouched test-set evaluation, subgroup review, approved thresholds "
    "and tested rollback — none of which exist yet")
RELEASE_GATE_ML = (
    "release gate: ML mode requires sufficient reviewed labels, supervised "
    "evaluation, calibration, approval and rollback testing — none of which "
    "exist yet")


FINAL_SCORING_ENGINE = "risk-engine-v1"

# Gate text -> stable code, so the UI can key on codes while the sentence
# stays the existing, UI-safe one (static strings + a label-count line).
GATE_CODES = (
    ("release gate: HYBRID", "RELEASE_GATE_HYBRID"),
    ("release gate: ML", "RELEASE_GATE_ML"),
    ("reviewed labels insufficient", "INSUFFICIENT_REVIEWED_LABELS"),
    ("no administrator-approved shadow model", "NO_SHADOW_MODEL"),
    ("no active threshold set", "THRESHOLD_UNRESOLVED"),
    ("no validated ML->risk signal mapping", "SIGNAL_MAPPING_UNVALIDATED"),
    ("the anomaly model", "GATED"),
)
_UNSAFE_FRAGMENTS = ("/app", "/var/", "Traceback", "Exception", "postgres", "password", "token")


def sanitize_gate_reasons(mode: str, reasons: List[str]) -> List[Dict[str, str]]:
    """{mode, code, message} per reason; a message that would leak an internal
    detail is replaced by its code (never expected — the sources are static)."""
    out = []
    for reason in reasons:
        code = "GATED"
        for prefix, candidate in GATE_CODES:
            if str(reason).startswith(prefix):
                code = candidate
                break
        message = str(reason)
        if any(fragment in message for fragment in _UNSAFE_FRAGMENTS):
            message = code
        out.append({"mode": mode, "code": code, "message": message})
    return out


@dataclass
class DecisionProvenance:
    """What ACTUALLY happened for one assessment — persisted with it, so the
    record stays true after the administrator changes the mode, the model or
    the mapping. Component-explicit on purpose: the final score is always
    computed by the rules engine; ML can only supply the anomaly INPUT."""
    requested_mode: str
    executed_mode: str                      # rules | shadow | ml
    anomaly_signal_source: str = "rules"    # rules | ml
    ml_role: str = "none"                   # none | observational | anomaly_signal
    ml_model_id: Optional[str] = None
    ml_model_version: Optional[str] = None
    signal_mapping_version: Optional[str] = None
    final_scoring_engine: str = FINAL_SCORING_ENGINE
    fallback: bool = False
    fallback_reason: Optional[str] = None
    gates: List[Dict[str, str]] = field(default_factory=list)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "requested_mode": self.requested_mode,
            "executed_mode": self.executed_mode,
            "anomaly_signal_source": self.anomaly_signal_source,
            "ml_role": self.ml_role,
            "ml_model_id": self.ml_model_id,
            "ml_model_version": self.ml_model_version,
            "signal_mapping_version": self.signal_mapping_version,
            "final_scoring_engine": self.final_scoring_engine,
            "fallback": self.fallback,
            "fallback_reason": self.fallback_reason,
            "gates": list(self.gates),
        }


@dataclass
class DecisionOutcome:
    assessment: Any                      # ThreatAssessment (rules engine output, always)
    requested_mode: str
    actual_mode_used: str
    fallback_reason: Optional[str] = None
    gate_reasons: List[str] = field(default_factory=list)
    shadow_planned: bool = False
    decision_record: Dict[str, Any] = field(default_factory=dict)
    provenance: Optional[DecisionProvenance] = None
    prediction_id: Optional[str] = None  # ML-mode prediction persisted by the router


class DecisionService:

    def current_mode(self) -> str:
        mode = str(settings.ML_DECISION_MODE or "rules").lower()
        return mode if mode in MODES else "rules"

    async def mode_availability(self, db: AsyncSession) -> Dict[str, Dict[str, Any]]:
        """All four modes with honest availability + exact unmet reasons."""
        from backend.ml.labeling_service import labeling_service
        from backend.ml.registry_service import registry_service

        shadow_model = await registry_service.get_stage_model(
            db, MODEL_TYPE_BEHAVIOR_ANOMALY, "shadow")
        stats = await labeling_service.label_stats(db)

        shadow_reasons = []
        if shadow_model is None:
            shadow_reasons.append(
                "no administrator-approved shadow model exists (train a "
                "candidate, then approve it into shadow)")

        hybrid_reasons = [RELEASE_GATE_HYBRID]
        if not stats["supervised_gate_open"]:
            counted = stats["counted_reviewed_manual"]
            label_reason = (
                f"reviewed labels insufficient: {counted['total']}/"
                f"{stats['required_total']} total, positive "
                f"{counted['positive']}/{stats['required_per_class']}, negative "
                f"{counted['negative']}/{stats['required_per_class']}")
            hybrid_reasons.append(label_reason)

        # ML mode = the anomaly model supplies the anomaly INPUT of the rules
        # engine through a VALIDATED mapping policy. Eligible only with an
        # approved shadow-stage model, its active threshold set and that policy.
        ml_reasons = list(shadow_reasons)
        if shadow_model is not None:
            from backend.ml.threshold_service import threshold_service
            if await threshold_service.get_active(db, model_id=shadow_model.id) is None:
                ml_reasons.append("no active threshold set for the shadow model")
        mapping = await self._mapping_policy(db)
        if mapping is None:
            ml_reasons.append(
                "no validated ML->risk signal mapping policy is active (profile "
                "ml_anomaly_signal_map must be active AND calibration_status=validated "
                "with evidence) — see the shadow evidence report")

        current = self.current_mode()
        return {
            "current_mode": current,
            "modes": {
                "rules": {"available": True, "reasons": [],
                          "description": "deterministic heuristic engine (production-safe default)"},
                "shadow": {"available": shadow_model is not None,
                           "reasons": shadow_reasons,
                           "description": "rules stay live; the anomaly model runs in parallel for comparison only"},
                "hybrid": {"available": False, "reasons": hybrid_reasons,
                           "description": "GATED this release"},
                "ml": {"available": not ml_reasons, "reasons": ml_reasons,
                       "description": ("the anomaly model supplies the behavioral-anomaly input of "
                                       "the rules risk engine through a validated mapping; rules "
                                       "fall back automatically per request when ML cannot")},
            },
        }

    async def _mapping_policy(self, db: AsyncSession):
        """The validated policy scoped to the CURRENT shadow model, its feature
        set and its active threshold set — None otherwise."""
        from backend.core.risk_engine import risk_engine
        from backend.ml.registry_service import registry_service
        from backend.ml.signal_mapping_service import signal_mapping_service
        from backend.ml.threshold_service import threshold_service
        config, _ = await risk_engine.get_model(db, "identity_threat")
        cap = float(config["weights"].get("anomaly_cap", 30.0))
        shadow = await registry_service.get_stage_model(db, MODEL_TYPE_BEHAVIOR_ANOMALY, "shadow")
        if shadow is None:
            return None
        from backend.ml.threshold_service import version_label
        active = await threshold_service.get_active(db, model_id=shadow.id)
        threshold_version = version_label(active) if active is not None else None
        return await signal_mapping_service.active_policy(
            db, anomaly_cap=cap, model_id=str(shadow.id),
            feature_set_version=shadow.feature_set_version, threshold_version=threshold_version)

    async def decision_engine_state(self, db: AsyncSession) -> Dict[str, Any]:
        """Current configuration, for page-level status (NOT per-result provenance)."""
        from backend.ml.registry_service import registry_service
        availability = await self.mode_availability(db)
        requested = availability["current_mode"]
        shadow_model = await registry_service.get_stage_model(db, MODEL_TYPE_BEHAVIOR_ANOMALY, "shadow")
        mapping = await self._mapping_policy(db)
        available = availability["modes"].get(requested, {}).get("available", False)
        effective = requested if available else "rules"
        gates = []
        for mode, info in availability["modes"].items():
            if not info["available"]:
                gates.extend(sanitize_gate_reasons(mode, info["reasons"]))
        return {
            "requested_mode": requested,
            "effective_mode": effective,
            "anomaly_signal_source_now": "ml" if effective == "ml" else "rules",
            "ml_role_now": ("anomaly_signal" if effective == "ml"
                            else "observational" if effective == "shadow" else "none"),
            "final_scoring_engine": FINAL_SCORING_ENGINE,
            "ml_model": ({"model_id": str(shadow_model.id), "model_type": shadow_model.model_type,
                          "version": shadow_model.version, "algorithm": shadow_model.algorithm,
                          "stage": shadow_model.stage,
                          "feature_set_version": shadow_model.feature_set_version}
                         if shadow_model else None),
            "signal_mapping": ({"version": mapping.version, "kind": mapping.kind,
                                "calibration_status": mapping.calibration_status}
                               if mapping else None),
            "gates": gates,
        }

    async def validate_mode_change(self, db: AsyncSession, target_mode: str) -> List[str]:
        """[] when the switch is permitted, else the exact unmet reasons."""
        if target_mode not in MODES:
            return [f"unknown mode {target_mode!r} (valid: {', '.join(MODES)})"]
        availability = await self.mode_availability(db)
        mode_info = availability["modes"][target_mode]
        return [] if mode_info["available"] else list(mode_info["reasons"])

    async def decide(self, db: AsyncSession, identity_id: str) -> DecisionOutcome:
        """THE decision router — the only place mode selection and fallback live.

            RULES  -> statistical anomaly signal -> risk engine
            SHADOW -> same live path; the model runs afterwards, observationally
            ML     -> validated mapping + healthy prediction -> ML anomaly signal
                      -> risk engine; ANY failure -> statistical signal (per
                      request; the configured mode is never touched)
            HYBRID -> gated (no combination policy exists) -> rules

        The rules engine computes the final score in every branch."""
        from backend.core.security_intelligence_service import security_intelligence_service

        requested = self.current_mode()

        if requested == "ml":
            return await self._decide_ml(db, identity_id)

        assessment = await security_intelligence_service.assess_threat(
            db=db, identity_id=identity_id)

        if requested == "rules":
            provenance = DecisionProvenance(requested_mode="rules", executed_mode="rules")
            return DecisionOutcome(
                assessment=assessment, requested_mode="rules", actual_mode_used="rules",
                provenance=provenance,
                decision_record={"requested_mode": "rules", "actual_mode_used": "rules"})

        if requested == "shadow":
            provenance = DecisionProvenance(
                requested_mode="shadow", executed_mode="shadow", ml_role="observational")
            return DecisionOutcome(
                assessment=assessment, requested_mode="shadow",
                actual_mode_used="shadow",   # live output is still the rules result
                shadow_planned=True, provenance=provenance,
                decision_record={
                    "requested_mode": "shadow",
                    "actual_mode_used": "shadow",
                    "note": ("live decision is the rules result; the anomaly "
                             "model runs in parallel without affecting it")})

        # hybrid: gated — rules serve, the gates are recorded.
        gate_reasons = await self.validate_mode_change(db, requested)
        logger.warning("[ML_OPS] mode %s requested but gated — serving rules "
                       "(reasons recorded)", requested)
        provenance = DecisionProvenance(
            requested_mode=requested, executed_mode="rules", fallback=True,
            fallback_reason=FallbackReason.MODE_GATED.value,
            gates=sanitize_gate_reasons(requested, gate_reasons))
        return DecisionOutcome(
            assessment=assessment, requested_mode=requested,
            actual_mode_used="rules",
            fallback_reason=FallbackReason.MODE_GATED.value,
            gate_reasons=gate_reasons, provenance=provenance,
            decision_record={
                "requested_mode": requested,
                "actual_mode_used": "rules",
                "fallback_reason": FallbackReason.MODE_GATED.value,
                "gate_reasons": gate_reasons})

    async def _decide_ml(self, db: AsyncSession, identity_id: str) -> DecisionOutcome:
        """ML mode. Readiness + inference are the existing inference service
        (bounded, never raises); the mapping policy is the existing validated
        slot. Every refusal becomes a per-request fallback to the statistical
        signal with a stable reason. Nothing here mutates ML_DECISION_MODE."""
        from backend.core.security_intelligence_service import security_intelligence_service
        from backend.ml.constants import ANOMALY_BANDS
        from backend.ml.inference_service import inference_service
        from backend.ml.signal_mapping_service import SignalMappingError, signal_mapping_service

        signal = None
        fallback_reason: Optional[str] = None
        prediction_id: Optional[str] = None
        model_id: Optional[str] = None
        model_label: Optional[str] = None

        policy = await self._mapping_policy(db)
        if policy is None:
            fallback_reason = FallbackReason.SIGNAL_MAPPING_UNVALIDATED.value
        else:
            try:
                result = await inference_service.predict_identity(db, identity_id)
            except Exception as exc:   # contract says never raises; belt and braces
                logger.warning("[ML_OPS] ML-mode inference raised identity=%s: %s", identity_id, exc)
                result = None
            if result is None:
                fallback_reason = FallbackReason.PREDICTION_FAILED.value
            else:
                model_id = str(result.model_id) if result.model_id else None
                model_label = result.model_version_label
                if not result.ok:
                    fallback_reason = result.failure_reason or FallbackReason.PREDICTION_FAILED.value
                elif (result.ml_anomaly_band not in ANOMALY_BANDS
                      or result.behavioral_anomaly_score is None
                      or not (0.0 <= float(result.behavioral_anomaly_score) <= 1.0)):
                    fallback_reason = FallbackReason.INVALID_PREDICTION.value
                else:
                    try:
                        signal = signal_mapping_service.apply(policy, result)
                    except SignalMappingError as exc:
                        logger.warning("[ML_OPS] mapping %s refused prediction: %s", policy.version, exc)
                        fallback_reason = FallbackReason.INVALID_PREDICTION.value
                try:
                    prediction_id = await inference_service.persist_prediction(
                        db, result, identity_id=identity_id, requested_mode="ml",
                        actual_mode_used="ml" if signal is not None else "rules",
                        fallback_reason=None if signal is not None else fallback_reason)
                except Exception as exc:   # persistence failure never changes the decision
                    logger.warning("[ML_OPS] ML-mode prediction persistence failed: %s", exc)

        assessment = await security_intelligence_service.assess_threat(
            db=db, identity_id=identity_id, anomaly_signal=signal)

        if signal is not None:
            provenance = DecisionProvenance(
                requested_mode="ml", executed_mode="ml", anomaly_signal_source="ml",
                ml_role="anomaly_signal", ml_model_id=model_id, ml_model_version=model_label,
                signal_mapping_version=signal.mapping_version)
            record = {"requested_mode": "ml", "actual_mode_used": "ml",
                      "note": ("the anomaly model supplied the behavioral-anomaly input; "
                               "the rules risk engine computed the score")}
            return DecisionOutcome(assessment=assessment, requested_mode="ml",
                                   actual_mode_used="ml", provenance=provenance,
                                   decision_record=record, prediction_id=prediction_id)

        logger.info("[ML_OPS] ML mode fell back to the statistical signal identity=%s reason=%s",
                    identity_id, fallback_reason)
        try:
            from backend.ml import metrics as ml_metrics
            ml_metrics.observe_fallback(fallback_reason)
        except Exception:
            pass
        provenance = DecisionProvenance(
            requested_mode="ml", executed_mode="rules", anomaly_signal_source="rules",
            ml_role="none", ml_model_id=model_id, ml_model_version=model_label,
            fallback=True, fallback_reason=fallback_reason)
        return DecisionOutcome(
            assessment=assessment, requested_mode="ml", actual_mode_used="rules",
            fallback_reason=fallback_reason, provenance=provenance,
            decision_record={"requested_mode": "ml", "actual_mode_used": "rules",
                             "fallback_reason": fallback_reason},
            prediction_id=prediction_id)


# Global instance
decision_service = DecisionService()

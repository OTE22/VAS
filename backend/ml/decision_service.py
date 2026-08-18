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


@dataclass
class DecisionOutcome:
    assessment: Any                      # ThreatAssessment (rules output, always)
    requested_mode: str
    actual_mode_used: str
    fallback_reason: Optional[str] = None
    gate_reasons: List[str] = field(default_factory=list)
    shadow_planned: bool = False
    decision_record: Dict[str, Any] = field(default_factory=dict)


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
        ml_reasons = [RELEASE_GATE_ML]
        if not stats["supervised_gate_open"]:
            counted = stats["counted_reviewed_manual"]
            label_reason = (
                f"reviewed labels insufficient: {counted['total']}/"
                f"{stats['required_total']} total, positive "
                f"{counted['positive']}/{stats['required_per_class']}, negative "
                f"{counted['negative']}/{stats['required_per_class']}")
            hybrid_reasons.append(label_reason)
            ml_reasons.append(label_reason)

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
                "ml": {"available": False, "reasons": ml_reasons,
                       "description": "GATED this release"},
            },
        }

    async def validate_mode_change(self, db: AsyncSession, target_mode: str) -> List[str]:
        """[] when the switch is permitted, else the exact unmet reasons."""
        if target_mode not in MODES:
            return [f"unknown mode {target_mode!r} (valid: {', '.join(MODES)})"]
        availability = await self.mode_availability(db)
        mode_info = availability["modes"][target_mode]
        return [] if mode_info["available"] else list(mode_info["reasons"])

    async def decide(self, db: AsyncSession, identity_id: str) -> DecisionOutcome:
        """Rules ALWAYS produce the live assessment. The mode only controls
        what happens AROUND it (shadow) or what is recorded (gated modes)."""
        from backend.core.security_intelligence_service import security_intelligence_service

        assessment = await security_intelligence_service.assess_threat(
            db=db, identity_id=identity_id)
        requested = self.current_mode()

        if requested == "rules":
            return DecisionOutcome(
                assessment=assessment, requested_mode="rules",
                actual_mode_used="rules",
                decision_record={"requested_mode": "rules",
                                 "actual_mode_used": "rules"})

        if requested == "shadow":
            return DecisionOutcome(
                assessment=assessment, requested_mode="shadow",
                actual_mode_used="shadow",   # live output is still the rules result
                shadow_planned=True,
                decision_record={
                    "requested_mode": "shadow",
                    "actual_mode_used": "shadow",
                    "note": ("live decision is the rules result; the anomaly "
                             "model runs in parallel without affecting it")})

        # hybrid / ml: gated — rules serve, the gates are recorded.
        gate_reasons = await self.validate_mode_change(db, requested)
        logger.warning("[ML_OPS] mode %s requested but gated — serving rules "
                       "(reasons recorded)", requested)
        return DecisionOutcome(
            assessment=assessment, requested_mode=requested,
            actual_mode_used="rules",
            fallback_reason=FallbackReason.MODE_GATED.value,
            gate_reasons=gate_reasons,
            decision_record={
                "requested_mode": requested,
                "actual_mode_used": "rules",
                "fallback_reason": FallbackReason.MODE_GATED.value,
                "gate_reasons": gate_reasons})


# Global instance
decision_service = DecisionService()

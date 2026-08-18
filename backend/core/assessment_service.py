"""
Threat-assessment persistence
=============================
Every generated assessment lands in ``threat_assessments`` transactionally.

Duplicate prevention is DATABASE-level: the idempotency key
``{subject_type}:{subject_id}:{model_version}:{time bucket}`` carries a
unique constraint, so two workers assessing the same subject in the same
window collapse onto one row regardless of locks (a short distributed lock
around computation additionally avoids duplicate WORK; the constraint is the
correctness guarantee). The bucket width is ASSESSMENT_DEDUP_WINDOW_MINUTES.

Logs carry ids and durations only — never embeddings, credentials or
snapshots.
"""

import logging
import time
import uuid as uuid_mod
from datetime import datetime
from typing import Any, Dict, List, Optional

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from db_models import Identity, IdentityAppearance, RiskSignalResult, ThreatAssessmentRecord
from config import settings
from backend.utils.time_utils import iso_utc

logger = logging.getLogger(__name__)



def _observe(result: str) -> None:
    try:
        from backend.core import metrics as app_metrics
        if app_metrics.metrics_assessments is not None:
            app_metrics.metrics_assessments.labels(result=result).inc()
    except Exception:
        pass


def _observe_duration(seconds: float) -> None:
    try:
        from backend.core import metrics as app_metrics
        if app_metrics.metrics_assessment_duration is not None:
            app_metrics.metrics_assessment_duration.observe(seconds)
    except Exception:
        pass


def build_idempotency_key(subject_type: str, subject_id: str,
                          model_version: str, at: datetime) -> str:
    window_minutes = max(1, int(settings.ASSESSMENT_DEDUP_WINDOW_MINUTES))
    bucket = int(at.timestamp() // (window_minutes * 60))
    return f"{subject_type}:{subject_id}:{model_version}:{bucket}"


def serialize_assessment(record: ThreatAssessmentRecord) -> Dict[str, Any]:
    def iso(dt):
        return iso_utc(dt) if dt else None
    return {
        "id": str(record.id),
        "subject_type": record.subject_type,
        "subject_id": record.subject_id,
        "person_id": str(record.person_id) if record.person_id else None,
        "pipeline_id": record.pipeline_id,
        "location_name": record.location_name,
        "event_id": record.event_id,
        "total_risk_score": record.total_risk_score,
        "severity": record.severity,
        "confidence": record.confidence,
        "signals": record.signals or [],
        "model_version": record.model_version,
        "threshold_version": record.threshold_version,
        "explanation": record.explanation,
        "limitations": record.limitations or [],
        "status": record.status,
        "source_timestamp": iso(record.source_timestamp),
        "created_at": iso(record.created_at),
        "updated_at": iso(record.updated_at),
        "acknowledged_at": iso(record.acknowledged_at),
        "acknowledged_by": record.acknowledged_by,
        "resolution_status": record.resolution_status,
        "resolution_notes": record.resolution_notes,
        # ML first release (additive): NULL/rules/shadow — the persisted
        # decision is the RULES result in every one of these modes, so the
        # honesty labels below stay exactly as they always were.
        "decision_mode": getattr(record, "decision_mode", None),
        "ml_prediction_id": (str(record.ml_prediction_id)
                             if getattr(record, "ml_prediction_id", None) else None),
        # Honesty labelling on every read, mirroring the engine contract.
        "score_type": "heuristic",
        "is_probability": False,
        "calibration_status": "uncalibrated",
    }


class AssessmentService:
    """Create, deduplicate, and manage persisted threat assessments."""

    async def persist_identity_assessment(
        self, db: AsyncSession, *, identity_id: str, assessment,
        threshold_version: Optional[str] = None,
        event_id: Optional[str] = None,
        decision_mode: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Persist a ThreatAssessment (unified engine output) idempotently.

        Returns the stored (or pre-existing) row as a dict plus a
        ``deduplicated`` flag. INSERT ... ON CONFLICT DO NOTHING on the
        idempotency key makes concurrent workers converge without errors.
        """
        started = time.monotonic()
        now = datetime.utcnow()
        engine_payload = assessment.engine or {}
        signals = engine_payload.get("signals") or [
            {"name": f.get("factor"), "score": f.get("score"),
             "weight": None, "raw_value": None,
             "explanation": f.get("description")}
            for f in (assessment.risk_factors or [])
        ]
        key = build_idempotency_key(
            "identity", identity_id, assessment.algorithm_version, now)

        # Context columns: latest appearance pins pipeline/location.
        pipeline_id = None
        location_name = None
        try:
            last_app = (await db.execute(
                select(IdentityAppearance)
                .where(IdentityAppearance.identity_id == uuid_mod.UUID(identity_id))
                .order_by(IdentityAppearance.start_time.desc()).limit(1)
            )).scalar_one_or_none()
            if last_app is not None:
                pipeline_id = last_app.pipeline_id
                from db_models import Pipeline
                prow = (await db.execute(
                    select(Pipeline.location_name)
                    .where(Pipeline.pipeline_id == pipeline_id))).scalar_one_or_none()
                location_name = prow
        except Exception:
            logger.debug("[ASSESSMENT] context lookup failed", exc_info=True)

        row_id = uuid_mod.uuid4()
        insert_stmt = pg_insert(ThreatAssessmentRecord).values(
            id=row_id,
            subject_type="identity",
            subject_id=identity_id,
            person_id=uuid_mod.UUID(identity_id),
            pipeline_id=pipeline_id,
            location_name=location_name,
            event_id=event_id,
            total_risk_score=assessment.overall_risk_score,
            severity=assessment.severity or assessment.threat_level,
            confidence=assessment.confidence,
            signals=signals,
            model_version=assessment.algorithm_version,
            threshold_version=threshold_version,
            explanation=engine_payload.get("explanation"),
            limitations=engine_payload.get("limitations") or [],
            status="open",
            source_timestamp=assessment.last_assessed or now,
            idempotency_key=key,
            created_at=now,
            updated_at=now,
            # NULL = legacy/rules; only 'rules'|'shadow' occur this release.
            # The live decision is ALWAYS the rules result either way.
            decision_mode=decision_mode,
        ).on_conflict_do_nothing(index_elements=["idempotency_key"])

        try:
            insert_result = await db.execute(insert_stmt)
            inserted = bool(insert_result.rowcount)
            if inserted:
                db.add_all([
                    RiskSignalResult(
                        assessment_id=row_id,
                        signal_name=str(s.get("name") or "unknown")[:64],
                        raw_value={"value": s.get("raw_value")},
                        score=float(s.get("score") or 0.0),
                        weight=float(s.get("weight") or 0.0),
                        explanation=s.get("explanation"),
                    )
                    for s in signals
                ])
            await db.commit()
        except Exception as e:
            await db.rollback()
            _observe("persist_error")
            logger.error("[ASSESSMENT] persist failed subject=identity:%s error=%s",
                         identity_id, e, exc_info=True)
            raise

        record = (await db.execute(
            select(ThreatAssessmentRecord)
            .where(ThreatAssessmentRecord.idempotency_key == key)
        )).scalar_one()
        _observe("created" if inserted else "deduplicated")
        _observe_duration(time.monotonic() - started)
        logger.info(
            "[ASSESSMENT] %s id=%s subject=identity:%s score=%.1f severity=%s "
            "model=%s threshold=%s duration_ms=%d",
            "created" if inserted else "deduplicated", record.id, identity_id,
            record.total_risk_score, record.severity, record.model_version,
            record.threshold_version, int((time.monotonic() - started) * 1000))
        payload = serialize_assessment(record)
        payload["deduplicated"] = not inserted
        return payload

    async def get(self, db: AsyncSession, assessment_id: str) -> Optional[Dict[str, Any]]:
        try:
            row_uuid = uuid_mod.UUID(str(assessment_id))
        except (ValueError, TypeError):
            return None
        record = (await db.execute(
            select(ThreatAssessmentRecord)
            .where(ThreatAssessmentRecord.id == row_uuid)
        )).scalar_one_or_none()
        return serialize_assessment(record) if record else None

    async def list(self, db: AsyncSession, *,
                   subject_type: Optional[str] = None,
                   subject_id: Optional[str] = None,
                   person_id: Optional[str] = None,
                   pipeline_id: Optional[str] = None,
                   location_name: Optional[str] = None,
                   severity: Optional[str] = None,
                   status: Optional[str] = None,
                   date_from: Optional[datetime] = None,
                   date_to: Optional[datetime] = None,
                   page: int = 1, page_size: int = 25) -> Dict[str, Any]:
        from sqlalchemy import func as sa_func
        query = select(ThreatAssessmentRecord)
        if subject_type:
            query = query.where(ThreatAssessmentRecord.subject_type == subject_type)
        if subject_id:
            query = query.where(ThreatAssessmentRecord.subject_id == subject_id)
        if person_id:
            try:
                query = query.where(
                    ThreatAssessmentRecord.person_id == uuid_mod.UUID(str(person_id)))
            except (ValueError, TypeError):
                return {"items": [], "total": 0, "page": page, "page_size": page_size}
        if pipeline_id:
            query = query.where(ThreatAssessmentRecord.pipeline_id == pipeline_id)
        if location_name:
            query = query.where(ThreatAssessmentRecord.location_name == location_name)
        if severity:
            query = query.where(ThreatAssessmentRecord.severity == severity)
        if status:
            query = query.where(ThreatAssessmentRecord.status == status)
        if date_from:
            query = query.where(ThreatAssessmentRecord.created_at >= date_from)
        if date_to:
            query = query.where(ThreatAssessmentRecord.created_at <= date_to)

        total = (await db.execute(
            select(sa_func.count()).select_from(query.subquery()))).scalar() or 0
        rows = (await db.execute(
            query.order_by(ThreatAssessmentRecord.created_at.desc())
            .offset((page - 1) * page_size).limit(page_size)
        )).scalars().all()
        return {
            "items": [serialize_assessment(r) for r in rows],
            "total": int(total),
            "page": page,
            "page_size": page_size,
        }

    async def transition(self, db: AsyncSession, assessment_id: str, *,
                         action: str, actor: str,
                         notes: Optional[str] = None,
                         resolution_status: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """acknowledge | resolve | reopen. Returns the updated row, or None
        when the id is unknown. Raises ValueError on an illegal transition."""
        try:
            row_uuid = uuid_mod.UUID(str(assessment_id))
        except (ValueError, TypeError):
            return None
        record = (await db.execute(
            select(ThreatAssessmentRecord)
            .where(ThreatAssessmentRecord.id == row_uuid)
        )).scalar_one_or_none()
        if record is None:
            return None

        now = datetime.utcnow()
        if action == "acknowledge":
            if record.status == "resolved":
                raise ValueError("Cannot acknowledge a resolved assessment; reopen it first.")
            record.status = "acknowledged"
            record.acknowledged_at = now
            record.acknowledged_by = actor[:255]
            _observe("acknowledged")
        elif action == "resolve":
            record.status = "resolved"
            record.resolution_status = (resolution_status or "resolved")[:32]
            if notes:
                record.resolution_notes = notes[:4000]
            if record.acknowledged_at is None:
                record.acknowledged_at = now
                record.acknowledged_by = actor[:255]
            _observe("resolved")
        elif action == "reopen":
            if record.status != "resolved":
                raise ValueError("Only resolved assessments can be reopened.")
            record.status = "open"
            record.resolution_status = "reopened"
            if notes:
                record.resolution_notes = notes[:4000]
            _observe("reopened")
        else:
            raise ValueError(f"Unknown action {action!r}")

        record.updated_at = now
        await db.commit()
        logger.info("[ASSESSMENT] %s id=%s actor=%s status=%s",
                    action, record.id, actor, record.status)
        return serialize_assessment(record)


# Global instance
assessment_service = AssessmentService()

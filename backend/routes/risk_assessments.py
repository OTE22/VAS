"""
Risk Assessment API
===================
Persisted threat assessments (unified risk engine) with full lifecycle:

- POST   /api/security/assessments                     create / recalculate (idempotent)
- GET    /api/security/assessments                     list + filters + pagination
- GET    /api/security/assessments/{id}                one assessment
- GET    /api/security/assessments/history/{type}/{id} subject history
- POST   /api/security/assessments/{id}/acknowledge    workflow
- POST   /api/security/assessments/{id}/resolve        workflow
- POST   /api/security/assessments/{id}/reopen         workflow
- GET    /api/security/risk-model                      model + threshold versions
- GET    /api/security/learned-thresholds              learned-threshold rows
- POST   /api/security/learned-thresholds/{id}/activate  explicit activation

Authorization: admin only (same guard as the rest of the security page).
Mutations require the same CSRF defense as the intelligence routes. Errors
return opaque reference ids — never internals. Scores are heuristics: every
payload carries score_type / is_probability / calibration_status so nobody
can mistake 80 for "80% probability".
"""

import logging
import time
import uuid as uuid_mod
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from db_connection import get_db
from backend.auth.auth_service import require_admin
from backend.core.assessment_service import assessment_service
from backend.core.distributed_lock import DistributedLock
from backend.core.rate_limiter import rate_limited
from backend.core.risk_engine import RISK_ENGINE_VERSION, SEVERITY_BANDS
from backend.core.threshold_store import (
    threshold_store, SIGNAL_DISTANCE, SIGNAL_TIME_WINDOW)
from backend.routes.intelligence import (
    _get_identity_or_404, _reference_id, require_intel_csrf)

logger = logging.getLogger(__name__)
router = APIRouter()

_VALID_SEVERITIES = {"low", "moderate", "high", "critical"}
_VALID_STATUSES = {"open", "acknowledged", "resolved"}


def _audit(action: str, current_user: dict, subject: Optional[str] = None,
           result: str = "success", duration_ms: Optional[int] = None, **fields):
    """One structured line per assessment access — ids and outcomes only."""
    extra = " ".join(f"{k}={v}" for k, v in fields.items() if v is not None)
    logger.info(
        "[RISK_AUDIT] action=%s user_id=%s subject=%s result=%s duration_ms=%s %s",
        action,
        (current_user or {}).get("id") or (current_user or {}).get("user_id"),
        subject, result, duration_ms, extra,
    )


def _safe_500(action: str, exc: Exception) -> HTTPException:
    ref = _reference_id()
    logger.error("[RISK_ASSESSMENTS] action=%s status=error reference_id=%s error=%s",
                 action, ref, exc, exc_info=True)
    return HTTPException(status_code=500,
                         detail=f"Internal error during {action}. Reference: {ref}")


def _actor(current_user: dict) -> str:
    return str((current_user or {}).get("username")
               or (current_user or {}).get("id") or "unknown")


class CreateAssessmentRequest(BaseModel):
    subject_type: str = Field(default="identity", pattern="^identity$",
                              description="Only 'identity' subjects are assessable today")
    subject_id: str = Field(..., min_length=8, max_length=64)
    event_id: Optional[str] = Field(default=None, max_length=64)


class ResolveRequest(BaseModel):
    resolution_status: str = Field(default="resolved", max_length=32)
    notes: Optional[str] = Field(default=None, max_length=4000)


class ReopenRequest(BaseModel):
    notes: Optional[str] = Field(default=None, max_length=4000)


async def _threshold_provenance(db: AsyncSession) -> str:
    """Which learned-threshold versions the co-appearance analysis is
    currently consuming (recorded on every persisted assessment)."""
    try:
        from config import settings
        window = await threshold_store.resolve(
            db, SIGNAL_TIME_WINDOW,
            static_default=settings.MULTI_CAMERA_TIME_WINDOW_MINUTES)
        distance = await threshold_store.resolve(
            db, SIGNAL_DISTANCE,
            static_default=settings.MULTI_CAMERA_DISTANCE_METERS)
        return (f"{SIGNAL_TIME_WINDOW}={window.provenance},"
                f"{SIGNAL_DISTANCE}={distance.provenance}")[:128]
    except Exception:
        return "unresolved"


@router.post(
    "/api/security/assessments",
    tags=["Security Intelligence"],
    summary="Create or Recalculate a Threat Assessment",
    description="Runs the unified risk engine for the subject and persists the "
                "result. Idempotent inside the dedup window: concurrent or "
                "repeated requests return the SAME assessment row.",
    status_code=201,
)
async def create_assessment(
    body: CreateAssessmentRequest,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_admin()),
    _csrf: None = Depends(require_intel_csrf),
    _rl: None = Depends(rate_limited("assessment_recalc", heavy=True)),
):
    started = time.monotonic()
    await _get_identity_or_404(db, body.subject_id)
    try:
        # Short distributed lock: avoids duplicate WORK across workers; the
        # DB unique constraint is the correctness guarantee either way.
        lock = DistributedLock(f"assessment:identity:{body.subject_id}", ttl_seconds=60)
        acquired = await lock.acquire(holder_label=_actor(current_user))
        try:
            from backend.ml.decision_service import decision_service
            outcome = await decision_service.decide(db, body.subject_id)
            assessment = outcome.assessment
            provenance = await _threshold_provenance(db)
            payload = await assessment_service.persist_identity_assessment(
                db, identity_id=body.subject_id, assessment=assessment,
                threshold_version=provenance, event_id=body.event_id,
                decision_mode=outcome.actual_mode_used)
            payload["decision"] = outcome.decision_record
        finally:
            if acquired:
                await lock.release()

        # SHADOW runs after the live result is sealed — comparison only.
        if outcome.shadow_planned:
            from backend.ml.shadow_service import shadow_service
            await shadow_service.run_shadow(
                identity_id=body.subject_id,
                rule_score=assessment.overall_risk_score,
                rule_severity=assessment.severity or assessment.threat_level,
                assessment_id=payload.get("id"),
                event_time=getattr(assessment, "last_assessed", None))

        _audit("assessment_create", current_user, f"identity:{body.subject_id}",
               duration_ms=int((time.monotonic() - started) * 1000),
               assessment_id=payload["id"], severity=payload["severity"],
               score=payload["total_risk_score"],
               deduplicated=payload.get("deduplicated"))
        status_code = 200 if payload.get("deduplicated") else 201
        return JSONResponse(status_code=status_code, content=payload)

    except HTTPException:
        _audit("assessment_create", current_user, f"identity:{body.subject_id}", result="error")
        raise
    except Exception as e:
        _audit("assessment_create", current_user, f"identity:{body.subject_id}", result="error")
        raise _safe_500("assessment creation", e)


@router.get(
    "/api/security/assessments",
    tags=["Security Intelligence"],
    summary="List Threat Assessments",
)
async def list_assessments(
    person_id: Optional[str] = Query(default=None),
    pipeline_id: Optional[str] = Query(default=None, max_length=255),
    location_name: Optional[str] = Query(default=None, max_length=255),
    severity: Optional[str] = Query(default=None),
    status: Optional[str] = Query(default=None),
    date_from: Optional[datetime] = Query(default=None),
    date_to: Optional[datetime] = Query(default=None),
    page: int = Query(default=1, ge=1, le=10000),
    page_size: int = Query(default=25, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_admin()),
):
    """Paginated risk assessments filtered by person, pipeline, location, severity, status and creation-date range. Invalid severity or status values are 422."""
    if severity and severity not in _VALID_SEVERITIES:
        raise HTTPException(status_code=422, detail=f"severity must be one of {sorted(_VALID_SEVERITIES)}")
    if status and status not in _VALID_STATUSES:
        raise HTTPException(status_code=422, detail=f"status must be one of {sorted(_VALID_STATUSES)}")
    try:
        result = await assessment_service.list(
            db, person_id=person_id, pipeline_id=pipeline_id,
            location_name=location_name, severity=severity, status=status,
            date_from=date_from, date_to=date_to, page=page, page_size=page_size)
        _audit("assessment_list", current_user,
               row_count=len(result["items"]), total=result["total"])
        return result
    except HTTPException:
        raise
    except Exception as e:
        raise _safe_500("assessment listing", e)


@router.get(
    "/api/security/assessments/history/{subject_type}/{subject_id}",
    tags=["Security Intelligence"],
    summary="Assessment History for a Subject",
)
async def assessment_history(
    subject_type: str,
    subject_id: str,
    page: int = Query(default=1, ge=1, le=10000),
    page_size: int = Query(default=25, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_admin()),
):
    """Assessment history for one identity (the only supported subject type), paginated. The identity must exist."""
    if subject_type != "identity":
        raise HTTPException(status_code=404, detail="Unknown subject type")
    await _get_identity_or_404(db, subject_id)
    try:
        result = await assessment_service.list(
            db, subject_type=subject_type, subject_id=subject_id,
            page=page, page_size=page_size)
        _audit("assessment_history", current_user, f"{subject_type}:{subject_id}",
               row_count=len(result["items"]))
        return result
    except HTTPException:
        raise
    except Exception as e:
        raise _safe_500("assessment history", e)


@router.get(
    "/api/security/assessments/{assessment_id}",
    tags=["Security Intelligence"],
    summary="Get One Threat Assessment",
)
async def get_assessment(
    assessment_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_admin()),
):
    """One assessment, including its score type, probability flag and calibration status."""
    try:
        payload = await assessment_service.get(db, assessment_id)
    except Exception as e:
        raise _safe_500("assessment retrieval", e)
    if payload is None:
        raise HTTPException(status_code=404, detail="Assessment not found")
    _audit("assessment_get", current_user, assessment_id=payload["id"])
    return payload


async def _workflow(action: str, assessment_id: str, current_user: dict,
                    db: AsyncSession, notes: Optional[str] = None,
                    resolution_status: Optional[str] = None):
    try:
        payload = await assessment_service.transition(
            db, assessment_id, action=action, actor=_actor(current_user),
            notes=notes, resolution_status=resolution_status)
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except HTTPException:
        raise
    except Exception as e:
        raise _safe_500(f"assessment {action}", e)
    if payload is None:
        raise HTTPException(status_code=404, detail="Assessment not found")
    _audit(f"assessment_{action}", current_user, assessment_id=payload["id"],
           status=payload["status"])
    return payload


@router.post("/api/security/assessments/{assessment_id}/acknowledge",
             tags=["Security Intelligence"], summary="Acknowledge an Assessment")
async def acknowledge_assessment(
    assessment_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_admin()),
    _csrf: None = Depends(require_intel_csrf),
):
    """Mark an open assessment acknowledged, recording who and when. A resolved assessment must be reopened first (409)."""
    return await _workflow("acknowledge", assessment_id, current_user, db)


@router.post("/api/security/assessments/{assessment_id}/resolve",
             tags=["Security Intelligence"], summary="Resolve an Assessment")
async def resolve_assessment(
    assessment_id: str,
    body: ResolveRequest,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_admin()),
    _csrf: None = Depends(require_intel_csrf),
):
    """Resolve an assessment with a resolution status and optional notes, back-filling acknowledgement if it was never acknowledged."""
    return await _workflow("resolve", assessment_id, current_user, db,
                           notes=body.notes, resolution_status=body.resolution_status)


@router.post("/api/security/assessments/{assessment_id}/reopen",
             tags=["Security Intelligence"], summary="Reopen a Resolved Assessment")
async def reopen_assessment(
    assessment_id: str,
    body: ReopenRequest,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_admin()),
    _csrf: None = Depends(require_intel_csrf),
):
    """Reopen a resolved assessment (anything not currently resolved is 409). Status returns to open with resolution_status 'reopened'."""
    return await _workflow("reopen", assessment_id, current_user, db, notes=body.notes)


@router.get(
    "/api/security/risk-model",
    tags=["Security Intelligence"],
    summary="Risk Model and Threshold Versions",
    description="Active scoring-model rows per profile plus the currently "
                "consumed learned-threshold versions. Scores are heuristics — "
                "calibration_status stays 'uncalibrated' until a validated "
                "calibration is recorded (never fabricated).",
)
async def get_risk_model(
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_admin()),
):
    try:
        from db_models import RiskModelVersion
        rows = (await db.execute(
            select(RiskModelVersion).order_by(
                RiskModelVersion.profile, RiskModelVersion.created_at.desc())
        )).scalars().all()
        models = [{
            "profile": r.profile,
            "version": r.version,
            "status": r.status,
            "score_type": r.score_type,
            "calibration_status": r.calibration_status,
            "weights": r.weights,
            "thresholds": r.thresholds,
            "activated_at": r.activated_at.isoformat() + "Z" if r.activated_at else None,
        } for r in rows]
        provenance = await _threshold_provenance(db)
        payload = {
            "engine_version": RISK_ENGINE_VERSION,
            "severity_bands": [
                {"floor": floor, "severity": label} for floor, label in SEVERITY_BANDS],
            "models": models,
            "threshold_provenance": provenance,
            "score_type": "heuristic",
            "is_probability": False,
        }
        resp = JSONResponse(content=payload)
        resp.headers["Cache-Control"] = "no-store"
        return resp
    except Exception as e:
        raise _safe_500("risk model report", e)


@router.get(
    "/api/security/learned-thresholds",
    tags=["Security Intelligence"],
    summary="List Learned Thresholds",
)
async def list_learned_thresholds(
    signal_name: Optional[str] = Query(default=None, max_length=64),
    status: Optional[str] = Query(default=None, max_length=16),
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_admin()),
):
    """Learned decision thresholds (up to 200), filterable by signal and status, each with value, sample count, version and activation record. Read-only."""
    try:
        items = await threshold_store.list_thresholds(
            db, signal_name=signal_name, status=status)
        return {"items": items, "total": len(items)}
    except Exception as e:
        raise _safe_500("threshold listing", e)


@router.post(
    "/api/security/learned-thresholds/{threshold_id}/activate",
    tags=["Security Intelligence"],
    summary="Activate a Learned Threshold",
    description="Explicit activation gate: candidates never take effect on "
                "their own. Activation retires the previous active row for "
                "the same scope+signal (re-activate it to roll back). "
                "Candidates under the minimum sample size are refused.",
)
async def activate_learned_threshold(
    threshold_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_admin()),
    _csrf: None = Depends(require_intel_csrf),
):
    from config import settings
    min_samples = int(settings.THRESHOLD_MIN_SAMPLES_FOR_ACTIVATION or 10)
    try:
        result = await threshold_store.activate(
            db, threshold_id, activated_by=_actor(current_user),
            min_samples=min_samples)
        await db.commit()
        _audit("threshold_activate", current_user, threshold_id=threshold_id,
               scope=f"{result.get('scope_type')}:{result.get('scope_id')}",
               signal=result.get("signal_name"), version=result.get("version"))
        return result
    except ValueError as e:
        await db.rollback()
        raise HTTPException(status_code=422, detail=str(e))
    except HTTPException:
        raise
    except Exception as e:
        await db.rollback()
        raise _safe_500("threshold activation", e)

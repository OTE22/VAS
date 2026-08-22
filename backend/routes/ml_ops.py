"""
ML Operations API — /api/ml/*

First-release surface: mode governance (RULES default; SHADOW after
explicit approval; HYBRID/ML gated with exact reasons), feature collection,
labels + review, datasets, training jobs (candidate-only output), registry
lifecycle (validated → admin-approved shadow → archive/rollback),
predictions, shadow comparisons (separate-concepts vocabulary), drift
reports (observation only), retraining policy (scaffolded, disabled).

Every endpoint: require_capability(ML_MANAGE) (admin-only by construction).
Mutations: CSRF (cookie clients need X-Requested-With) + rate limits +
ml_audit rows + [MLOPS_AUDIT] lines. Errors: opaque reference ids. No
server filesystem path is ever serialized.
"""

import logging
import time
import uuid as uuid_mod
from datetime import datetime
from typing import Any, Dict, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import select, func as sa_func
from sqlalchemy.ext.asyncio import AsyncSession

from db_connection import get_db
from backend.auth.auth_service import require_capability
from backend.auth.capabilities import Capability
from backend.core.distributed_lock import DistributedLock
from backend.core.rate_limiter import rate_limited
from backend.ml.audit import ml_audit
from backend.ml.constants import (
    MODEL_TYPE_BEHAVIOR_ANOMALY, MODEL_TYPES, all_optional_capabilities)
from backend.utils.time_utils import iso_utc

logger = logging.getLogger(__name__)
from fastapi.routing import APIRoute
from backend.ml import call_log
from backend.ml.system_state import ml_system_state


class LoggedMLRoute(APIRoute):
    """Every ML-Ops call leaves one structured record (backend/ml/call_log):
    request id, actor, method, route + path, query, sanitised body summary,
    status, error code, duration, produced ids. Refusals (HTTPException)
    are recorded with their stable code and re-raised unchanged; unexpected
    exceptions are recorded as 500 and re-raised for the global handler."""

    def get_route_handler(self):
        original = super().get_route_handler()
        template = self.path

        async def logged(request: Request):
            timer = call_log.CallTimer()
            body_summary = None
            if request.method in ("POST", "PUT", "PATCH"):
                try:
                    raw = await request.body()
                    if raw:
                        import json as _json
                        body_summary = call_log.sanitize_body(_json.loads(raw))
                except Exception:
                    body_summary = "<unparsed>"
            entry = {
                "method": request.method, "route": template,
                "path": request.url.path,
                "query": dict(request.query_params) or None,
                "body": body_summary,
                "client": request.client.host if request.client else None,
            }
            try:
                response = await original(request)
            except HTTPException as exc:
                detail = exc.detail if isinstance(exc.detail, dict) else {}
                entry.update({"status": exc.status_code, "ms": timer.ms,
                              "error_code": detail.get("error_code") or (
                                  exc.detail if isinstance(exc.detail, str) else None),
                              "actor": getattr(request.state, "ml_actor", None)})
                call_log.record(entry)
                raise
            except Exception as exc:
                entry.update({"status": 500, "ms": timer.ms,
                              "error_code": type(exc).__name__,
                              "actor": getattr(request.state, "ml_actor", None)})
                call_log.record(entry)
                raise
            produced = {}
            try:
                body = getattr(response, "body", None)
                if body and len(body) < 200_000:
                    import json as _json
                    produced = call_log.extract_ids(_json.loads(body))
            except Exception:
                produced = {}
            entry.update({"status": response.status_code, "ms": timer.ms,
                          "actor": getattr(request.state, "ml_actor", None),
                          "produced": produced or None})
            call_log.record(entry)
            return response
        return logged


router = APIRouter(route_class=LoggedMLRoute)


def _reference_id() -> str:
    return f"MLOPS-{uuid_mod.uuid4().hex[:8]}"


def _safe_500(action: str, exc: Exception) -> HTTPException:
    ref = _reference_id()
    logger.error("[ML_OPS] action=%s status=error reference_id=%s error=%s",
                 action, ref, exc, exc_info=True)
    return HTTPException(status_code=500,
                         detail=f"Internal error during {action}. Reference: {ref}")


def _error(status_code: int, error_code: str, message: str, **extra) -> HTTPException:
    return HTTPException(status_code=status_code,
                         detail={"error_code": error_code, "message": message, **extra})


def require_mlops_csrf(request: Request):
    """Cookie-authenticated mutations need X-Requested-With (CSRF defense);
    bearer-token clients are exempt (same policy as the intelligence routes)."""
    if request.headers.get("authorization"):
        return
    if request.headers.get("x-requested-with", "").lower() != "xmlhttprequest":
        raise HTTPException(status_code=403,
                            detail="CSRF check failed: X-Requested-With header required")


def _actor(user) -> str:
    if isinstance(user, dict):
        return str(user.get("username") or user.get("id") or "unknown")
    return str(getattr(user, "username", None) or getattr(user, "id", "unknown"))


def _actor_id(user) -> Optional[int]:
    if isinstance(user, dict):
        return user.get("id") or user.get("user_id")
    return getattr(user, "id", None)


_ML_MANAGE_CAPABILITY = require_capability(Capability.ML_MANAGE)


async def ML_MANAGE(request: Request, current_user=Depends(_ML_MANAGE_CAPABILITY)):
    """The registry gate, plus the actor for the call log (request.state)."""
    request.state.ml_actor = _actor(current_user)
    request.state.ml_actor_id = _actor_id(current_user)
    return current_user


class ModeChangeRequest(BaseModel):
    mode: str = Field(..., pattern="^(rules|shadow|hybrid|ml)$")
    reason: str = Field(..., min_length=3, max_length=500)


class PauseRequest(BaseModel):
    reason: str = Field(..., min_length=3, max_length=500)


class ShadowApprovalRequest(BaseModel):
    reason: str = Field(..., min_length=3, max_length=1000)
    intended_scope: str = Field(default="all_pipelines", max_length=255)


class RejectRequest(BaseModel):
    reason: str = Field(..., min_length=3, max_length=1000)


class TrainingRequest(BaseModel):
    model_type: str = Field(default=MODEL_TYPE_BEHAVIOR_ANOMALY)
    algorithm: str = Field(default="isolation_forest", max_length=64)
    # Experiment knobs. dataset_id trains from an EXISTING built dataset
    # (verified by logical checksum + Parquet file hash) instead of building
    # a new one — one immutable dataset, many experiments.
    dataset_id: Optional[str] = Field(default=None, max_length=64)
    seed: Optional[int] = Field(default=None, ge=0, le=2**31 - 1)
    hyperparameters: Optional[Dict[str, Any]] = None
    sampling_policy: Optional[str] = Field(default=None, pattern="^(refuse|newest_first|oldest_first)$")


_SELECTION_METHODS = ("natural", "stratified_by_band", "top_scores", "random", "manual")


def _selection_metadata(raw):
    """Bounded, typed selection metadata: {method, band, sampling_probability, reason, selected_at}."""
    if not raw:
        return None
    if not isinstance(raw, dict):
        raise _error(422, "INVALID_SELECTION", "selection must be an object")
    method = str(raw.get("method") or "natural")
    if method not in _SELECTION_METHODS:
        raise _error(422, "INVALID_SELECTION", f"selection.method must be one of {_SELECTION_METHODS}")
    prob = raw.get("sampling_probability")
    if prob is not None:
        try:
            prob = float(prob)
        except (TypeError, ValueError):
            raise _error(422, "INVALID_SELECTION", "sampling_probability must be a number")
        if not 0.0 < prob <= 1.0:
            raise _error(422, "INVALID_SELECTION", "sampling_probability must be in (0, 1]")
    return {"method": method, "band": (str(raw.get("band"))[:32] if raw.get("band") else None),
            "sampling_probability": prob, "reason": (str(raw.get("reason"))[:200] if raw.get("reason") else None),
            "selected_at": iso_utc(datetime.utcnow())}


class LabelCreateRequest(BaseModel):
    subject_id: str = Field(..., min_length=8, max_length=64)
    label: str = Field(..., pattern="^(positive|negative|unknown)$")
    label_kind: str = Field(default="manual", pattern="^(manual|weak)$")
    source: str = Field(default="analyst_review", max_length=64)
    event_time: datetime
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    assessment_id: Optional[str] = None
    notes: Optional[str] = Field(default=None, max_length=2000)
    # How this review was selected (stratified validation reviews): keeps a
    # deliberately over-sampled band from being read as natural prevalence.
    selection: Optional[Dict[str, Any]] = None


class LabelReviewRequest(BaseModel):
    action: str = Field(..., pattern="^(confirm|dispute|retract)$")
    notes: Optional[str] = Field(default=None, max_length=2000)


class LabelSupersedeRequest(BaseModel):
    label: str = Field(..., pattern="^(positive|negative|unknown)$")
    notes: Optional[str] = Field(default=None, max_length=2000)


class DatasetBuildRequest(BaseModel):
    name: str = Field(default="behavior_anomaly_model-train", max_length=128)
    kind: str = Field(default="unsupervised", pattern="^(unsupervised|supervised)$")
    # Typed extraction definition (defaults to the definition of `kind`),
    # explicit [start, end) range and what to do above the cap. Without a
    # policy the build REFUSES when the population exceeds the cap — nothing
    # is ever discarded silently.
    definition: Optional[str] = Field(default=None, max_length=128)
    definition_version: Optional[str] = Field(default=None, max_length=16)
    time_range_start: Optional[datetime] = None
    time_range_end: Optional[datetime] = None
    sampling_policy: Optional[str] = Field(default=None, pattern="^(refuse|newest_first|oldest_first)$")
    # Declared split strategy (overrides the definition's; recorded on the
    # dataset). 'temporal' lets entities recur across splits and records the
    # measured overlap — for long histories of regular entities where
    # group isolation leaves no val/test rows.
    split_strategy: Optional[str] = Field(default=None, pattern="^(temporal_group|temporal)$")


class PolicyUpdateRequest(BaseModel):
    enabled: bool = False
    schedule_interval_hours: int = Field(default=168, ge=1, le=8760)
    min_new_labels: int = Field(default=25, ge=1)
    min_total_labels: int = Field(default=100, ge=1)
    cooldown_hours: int = Field(default=168, ge=1)
    min_drift_reports: int = Field(default=2, ge=2,
                                   description="never retrain on one weak signal")


# ---------------------------------------------------------------------------
# Overview + configuration
# ---------------------------------------------------------------------------

@router.get("/api/ml/overview", tags=["ML Operations"], summary="ML Operations Overview")
async def ml_overview(
    db: AsyncSession = Depends(get_db),
    current_user=Depends(ML_MANAGE),
):
    """One read-only payload for the ML Ops dashboard: mode availability with unmet gates, label stats, feature/prediction counts, current shadow and validated models, and the three newest drift reports."""
    try:
        from backend.ml.decision_service import decision_service
        from backend.ml.labeling_service import labeling_service
        from backend.ml.registry_service import registry_service, serialize_model_row
        from db_models import (MLFeatureSnapshot, MLPrediction, MLDriftReport)

        availability = await decision_service.mode_availability(db)
        label_stats = await labeling_service.label_stats(db)
        shadow = await registry_service.get_stage_model(
            db, MODEL_TYPE_BEHAVIOR_ANOMALY, "shadow")
        validated = await registry_service.get_stage_model(
            db, MODEL_TYPE_BEHAVIOR_ANOMALY, "validated")
        snapshot_count = (await db.execute(
            select(sa_func.count(MLFeatureSnapshot.id)))).scalar() or 0
        prediction_count = (await db.execute(
            select(sa_func.count(MLPrediction.id)))).scalar() or 0
        fallback_count = (await db.execute(
            select(sa_func.count(MLPrediction.id))
            .where(MLPrediction.fallback_reason.isnot(None)))).scalar() or 0
        latest_drift = (await db.execute(
            select(MLDriftReport).order_by(MLDriftReport.created_at.desc()).limit(3)
        )).scalars().all()
        from backend.ml.drift_service import drift_service

        payload = {
            "mode": availability,
            "label_readiness": label_stats,
            "data_readiness": {
                "feature_snapshots": int(snapshot_count),
                "predictions": int(prediction_count),
                "fallback_predictions": int(fallback_count),
            },
            "models": {
                "shadow": serialize_model_row(shadow) if shadow else None,
                "validated_candidate": serialize_model_row(validated) if validated else None,
            },
            "latest_drift_reports": [drift_service.serialize_report(r) for r in latest_drift],
            "optional_capabilities": all_optional_capabilities(),
            # What the system IS right now + the core changes that made it so
            "system": await ml_system_state(db),
            "warnings": [
                "Anomaly does not mean threat.",
                "Heuristic scores are not probabilities.",
                "Uncalibrated model outputs are not probabilities.",
                "Shadow mode does not affect live decisions.",
                "ML and HYBRID modes are currently gated.",
                "Drift does not automatically prove model failure.",
                "Human review remains required.",
            ],
        }
        resp = JSONResponse(content=jsonable(payload))
        resp.headers["Cache-Control"] = "no-store"
        return resp
    except HTTPException:
        raise
    except Exception as e:
        raise _safe_500("ml overview", e)


def jsonable(obj):
    """datetime-safe deep conversion for JSONResponse."""
    if isinstance(obj, dict):
        return {k: jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [jsonable(v) for v in obj]
    if isinstance(obj, datetime):
        return iso_utc(obj)
    return obj


@router.put("/api/ml/config/mode", tags=["ML Operations"], summary="Change Decision Mode")
async def change_mode(
    body: ModeChangeRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(ML_MANAGE),
    _csrf: None = Depends(require_mlops_csrf),
    _rl: None = Depends(rate_limited("ml_ops", heavy=False)),
):
    """Change the ML decision mode. Refused with 409 MODE_GATED while any promotion gate is unmet; on success the mode is persisted, applied to the runtime, and audited with the caller and reason."""
    try:
        from backend.ml.decision_service import decision_service
        unmet = await decision_service.validate_mode_change(db, body.mode)
        if unmet:
            raise _error(409, "MODE_GATED",
                         f"mode '{body.mode}' cannot be activated yet",
                         unmet_gates=unmet)

        from config import settings as app_settings
        from backend.core.runtime_settings import apply_to_runtime
        from db_models import Setting
        before = str(app_settings.ML_DECISION_MODE)
        row = (await db.execute(
            select(Setting).where(Setting.key == "ML_DECISION_MODE"))).scalar_one_or_none()
        if row is not None:
            row.value = body.mode
            row.updated_at = datetime.utcnow()
        apply_to_runtime("ML_DECISION_MODE", body.mode)
        await ml_audit(db, action="mode_change", actor_username=_actor(current_user),
                       actor_user_id=_actor_id(current_user),
                       object_type="ml_config", object_id="ML_DECISION_MODE",
                       before={"mode": before}, after={"mode": body.mode},
                       reason=body.reason,
                       ip_address=(request.client.host if request.client else None))
        await db.commit()
        return {"success": True, "mode": body.mode, "previous_mode": before}
    except HTTPException:
        raise
    except Exception as e:
        raise _safe_500("mode change", e)


@router.post("/api/ml/pause", tags=["ML Operations"],
             summary="Pause ML — restore rules immediately")
async def pause_ml(
    body: PauseRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(ML_MANAGE),
    _csrf: None = Depends(require_mlops_csrf),
):
    """Emergency stop: unconditionally forces the decision mode back to 'rules', bypassing gate checks. Persisted, applied immediately, and audited with the required reason."""
    try:
        from config import settings as app_settings
        from backend.core.runtime_settings import apply_to_runtime
        from db_models import Setting
        before = str(app_settings.ML_DECISION_MODE)
        row = (await db.execute(
            select(Setting).where(Setting.key == "ML_DECISION_MODE"))).scalar_one_or_none()
        if row is not None:
            row.value = "rules"
            row.updated_at = datetime.utcnow()
        apply_to_runtime("ML_DECISION_MODE", "rules")
        await ml_audit(db, action="pause", actor_username=_actor(current_user),
                       actor_user_id=_actor_id(current_user),
                       object_type="ml_config", object_id="ML_DECISION_MODE",
                       before={"mode": before}, after={"mode": "rules"},
                       reason=body.reason,
                       ip_address=(request.client.host if request.client else None))
        await db.commit()
        return {"success": True, "mode": "rules", "previous_mode": before,
                "note": "rules engine restored as the sole decision path"}
    except HTTPException:
        raise
    except Exception as e:
        raise _safe_500("ml pause", e)


# ---------------------------------------------------------------------------
# Features + labels + datasets
# ---------------------------------------------------------------------------

@router.post("/api/ml/features/compute", tags=["ML Operations"],
             summary="Run Feature Collection", status_code=202)
async def compute_features(
    full_rebuild: bool = Query(default=False, description="Recompute every snapshot from the start of history "
                                                             "(needed once after a feature-set version bump)"),
    db: AsyncSession = Depends(get_db),
    current_user=Depends(ML_MANAGE),
    _csrf: None = Depends(require_mlops_csrf),
    _rl: None = Depends(rate_limited("ml_ops", heavy=True)),
):
    """Launch feature-snapshot collection as a background job (202 + job_id). Incremental by default (watermark); `full_rebuild=true` recomputes all history under the CURRENT feature set — required once after a feature-set version bump, since incremental runs only see new events. A second request while one runs returns 409 JOB_ALREADY_RUNNING; a cross-worker lock prevents concurrent collection."""
    try:
        from backend.ml.collector import launch_collection_job
        outcome = await launch_collection_job(created_by_user_id=_actor_id(current_user),
                                              full_rebuild=full_rebuild)
        if outcome.get("status") == "busy":
            raise _error(409, "JOB_ALREADY_RUNNING", outcome.get("reason", "busy"))
        return JSONResponse(status_code=202, content=outcome)
    except HTTPException:
        raise
    except Exception as e:
        raise _safe_500("feature collection", e)


@router.get("/api/ml/features/definitions", tags=["ML Operations"],
            summary="Feature Definitions")
async def feature_definitions(
    db: AsyncSession = Depends(get_db),
    current_user=Depends(ML_MANAGE),
):
    """List every feature definition (active first): name, version, entity type, window, leakage class and readiness requirements. Read-only."""
    try:
        from db_models import MLFeatureDefinition
        rows = (await db.execute(
            select(MLFeatureDefinition).order_by(
                MLFeatureDefinition.is_active.desc(), MLFeatureDefinition.name)
        )).scalars().all()
        return {"items": [{
            "name": r.name, "version": r.version, "entity_type": r.entity_type,
            "window": r.window, "source": r.source, "computation": r.computation,
            "leakage_class": r.leakage_class, "is_active": r.is_active,
            "description": r.description,
            "readiness_requirements": r.readiness_requirements,
        } for r in rows], "total": len(rows)}
    except Exception as e:
        raise _safe_500("feature definitions", e)


@router.get("/api/ml/labels/stats", tags=["ML Operations"], summary="Label Readiness")
async def label_stats(
    db: AsyncSession = Depends(get_db),
    current_user=Depends(ML_MANAGE),
):
    """Label counts by class and review status, the configured supervised-training minimums, and whether the supervised gate is currently open. Only manual and reviewed labels count."""
    try:
        from backend.ml.labeling_service import labeling_service
        return await labeling_service.label_stats(db)
    except Exception as e:
        raise _safe_500("label stats", e)


@router.get("/api/ml/labels", tags=["ML Operations"], summary="List Labels")
async def list_labels(
    label: Optional[str] = Query(default=None),
    label_kind: Optional[str] = Query(default=None),
    review_status: Optional[str] = Query(default=None),
    subject_id: Optional[str] = Query(default=None, max_length=64),
    page: int = Query(default=1, ge=1, le=10000),
    page_size: int = Query(default=25, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    current_user=Depends(ML_MANAGE),
):
    """Paginated label listing, newest first, filterable by class, kind, review status and subject."""
    try:
        from backend.ml.labeling_service import labeling_service
        return await labeling_service.list_labels(
            db, label=label, label_kind=label_kind, review_status=review_status,
            subject_id=subject_id, page=page, page_size=page_size)
    except Exception as e:
        raise _safe_500("label listing", e)


@router.post("/api/ml/labels", tags=["ML Operations"], summary="Create Label",
             status_code=201)
async def create_label(
    body: LabelCreateRequest,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(ML_MANAGE),
    _csrf: None = Depends(require_mlops_csrf),
    _rl: None = Depends(rate_limited("ml_ops", heavy=False)),
):
    """Create a label (idempotent — a duplicate returns 200 instead of 201; weak labels are capped below full confidence). Invalid input is 422 with INVALID_LABEL or UNREVIEWED_ALERT."""
    try:
        from backend.ml.labeling_service import labeling_service
        payload = await labeling_service.create_label(
            db, subject_id=body.subject_id, label=body.label,
            label_kind=body.label_kind, source=body.source,
            event_time=body.event_time, created_by=_actor(current_user),
            confidence=body.confidence, assessment_id=body.assessment_id,
            notes=body.notes, actor_user_id=_actor_id(current_user),
            selection=_selection_metadata(body.selection))
        status_code = 200 if payload.get("deduplicated") else 201
        return JSONResponse(status_code=status_code, content=jsonable(payload))
    except ValueError as e:
        code = "UNREVIEWED_ALERT" if str(e).startswith("UNREVIEWED_ALERT") else "INVALID_LABEL"
        raise _error(422, code, str(e))
    except HTTPException:
        raise
    except Exception as e:
        raise _safe_500("label creation", e)


@router.post("/api/ml/labels/{label_id}/review", tags=["ML Operations"],
             summary="Review Label")
async def review_label(
    label_id: str,
    body: LabelReviewRequest,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(ML_MANAGE),
    _csrf: None = Depends(require_mlops_csrf),
):
    """Confirm, dispute or retract a label. Only confirmed manual labels count toward the supervised gate. Illegal transitions return 409 INVALID_REVIEW."""
    try:
        from backend.ml.labeling_service import labeling_service
        payload = await labeling_service.review_label(
            db, label_id, action=body.action, actor=_actor(current_user),
            notes=body.notes, actor_user_id=_actor_id(current_user))
    except ValueError as e:
        raise _error(409, "INVALID_REVIEW", str(e))
    except Exception as e:
        raise _safe_500("label review", e)
    if payload is None:
        raise HTTPException(status_code=404, detail="Label not found")
    return payload


@router.post("/api/ml/labels/{label_id}/supersede", tags=["ML Operations"],
             summary="Correct a label by supersession")
async def supersede_label(
    label_id: str,
    body: LabelSupersedeRequest,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(ML_MANAGE),
    _csrf: None = Depends(require_mlops_csrf),
):
    """Correct a label WITHOUT editing history: the old label becomes `superseded`, a new active label is created with `supersedes_id` pointing at it, and every prediction outcome the old label explained is re-pointed to the new one in the same transaction. Only an active label can be superseded (409 LABEL_NOT_ACTIVE)."""
    try:
        from backend.ml.labeling_service import labeling_service
        payload = await labeling_service.supersede_label(
            db, label_id, label=body.label, actor=_actor(current_user),
            notes=body.notes, actor_user_id=_actor_id(current_user))
    except ValueError as e:
        code = "LABEL_NOT_ACTIVE" if str(e).startswith("LABEL_NOT_ACTIVE") else "INVALID_LABEL"
        raise _error(409 if code == "LABEL_NOT_ACTIVE" else 422, code, str(e))
    except Exception as e:
        raise _safe_500("label supersede", e)
    if payload is None:
        raise HTTPException(status_code=404, detail="Label not found")
    return payload


@router.get("/api/ml/datasets", tags=["ML Operations"], summary="List Datasets")
async def list_datasets(
    page: int = Query(default=1, ge=1, le=10000),
    page_size: int = Query(default=25, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    current_user=Depends(ML_MANAGE),
):
    """Paginated dataset-version listing, newest first."""
    try:
        from db_models import MLDataset
        from backend.ml.dataset_builder import serialize_dataset
        total = (await db.execute(select(sa_func.count(MLDataset.id)))).scalar() or 0
        rows = (await db.execute(
            select(MLDataset).order_by(MLDataset.created_at.desc())
            .offset((page - 1) * page_size).limit(page_size))).scalars().all()
        return {"items": [serialize_dataset(r) for r in rows],
                "total": int(total), "page": page, "page_size": page_size}
    except Exception as e:
        raise _safe_500("dataset listing", e)


@router.post("/api/ml/datasets", tags=["ML Operations"], summary="Build Dataset",
             status_code=201)
async def build_dataset_endpoint(
    body: DatasetBuildRequest,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(ML_MANAGE),
    _csrf: None = Depends(require_mlops_csrf),
    _rl: None = Depends(rate_limited("ml_ops", heavy=True)),
):
    """Build a dataset version synchronously from a typed extraction definition (default: the definition of `kind`), an optional explicit time range and a declared cap policy. Returns 201 when built; a failed quality report or a cap refusal (EXTRACTION_EXCEEDS_CAP) is returned as 422."""
    try:
        from backend.ml.dataset_builder import build_dataset
        from backend.ml.dataset_definitions import get_definition
        definition = None
        if body.definition:
            try:
                definition = get_definition(body.definition, body.definition_version)
            except KeyError as e:
                raise _error(422, "UNKNOWN_DATASET_DEFINITION", str(e))
            if definition.kind != body.kind:
                raise _error(422, "DATASET_DEFINITION_KIND_MISMATCH",
                             f"definition {definition.key} is {definition.kind}, request says {body.kind}")
        start = body.time_range_start.replace(tzinfo=None) if body.time_range_start else None
        end = body.time_range_end.replace(tzinfo=None) if body.time_range_end else None
        if start and end and start >= end:
            raise _error(422, "INVALID_TIME_RANGE", "time_range_start must be before time_range_end")
        outcome = await build_dataset(db, name=body.name, kind=body.kind,
                                      created_by=_actor_id(current_user),
                                      definition=definition, time_range_start=start,
                                      time_range_end=end, sampling_policy=body.sampling_policy,
                                      split_strategy=body.split_strategy)
        status_code = 201 if outcome["status"] == "built" else 422
        return JSONResponse(status_code=status_code, content=jsonable(outcome))
    except HTTPException:
        raise
    except Exception as e:
        raise _safe_500("dataset build", e)


@router.get("/api/ml/datasets/definitions", tags=["ML Operations"],
            summary="Dataset Definitions")
async def list_dataset_definitions(current_user=Depends(ML_MANAGE)):
    """The typed extraction definitions a dataset can be built from, with the known limitations of the feature set each one uses."""
    from backend.ml.dataset_definitions import feature_set_limitations, list_definitions
    items = []
    for d in list_definitions():
        item = d.to_manifest()
        item["feature_set_limitations"] = feature_set_limitations(d.feature_set_version)
        items.append(item)
    return {"items": items, "total": len(items)}


@router.post("/api/ml/datasets/backfill-hashes", tags=["ML Operations"],
             summary="Verify legacy datasets and record their file hashes")
async def backfill_dataset_hashes_endpoint(
    db: AsyncSession = Depends(get_db),
    current_user=Depends(ML_MANAGE),
    _csrf: None = Depends(require_mlops_csrf),
    _rl: None = Depends(rate_limited("ml_ops", heavy=True)),
):
    """For datasets built before file hashing: reload the Parquet, and only when the reloaded rows reproduce the registered checksum record the file sha256 + a manifest (legacy extraction policy). Lineage is never rewritten; unverifiable rows are listed with a reason."""
    try:
        from backend.ml.dataset_builder import backfill_dataset_file_hashes
        report = await backfill_dataset_file_hashes(db)
        await ml_audit(db, action="dataset_backfill_hashes",
                       actor_username=_actor(current_user), actor_user_id=_actor_id(current_user),
                       object_type="ml_dataset", object_id="*",
                       after={"verified": len(report["verified"]),
                              "unverifiable": len(report["unverifiable"])})
        await db.commit()
        return report
    except HTTPException:
        raise
    except Exception as e:
        raise _safe_500("dataset hash backfill", e)


class DatasetArchiveRequest(BaseModel):
    reason: str = Field(..., min_length=3, max_length=1000)


@router.post("/api/ml/datasets/{dataset_id}/archive", tags=["ML Operations"],
             summary="Archive an unreferenced dataset (explicit, never automatic)")
async def archive_dataset_endpoint(
    dataset_id: str,
    body: DatasetArchiveRequest,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(ML_MANAGE),
    _csrf: None = Depends(require_mlops_csrf),
    _rl: None = Depends(rate_limited("ml_ops", heavy=False)),
):
    """Releases the Parquet bytes of a dataset no registered model was trained from. The row (provenance) and manifest stay; a dataset referenced by a model is refused with 409 DATASET_REFERENCED_BY_MODEL."""
    try:
        from backend.ml.dataset_builder import DatasetArchiveRefusal, archive_dataset
        try:
            outcome = await archive_dataset(db, dataset_id, actor=_actor(current_user), reason=body.reason)
        except DatasetArchiveRefusal as refusal:
            status = 404 if refusal.code == "DATASET_NOT_FOUND" else 409
            raise _error(status, refusal.code, refusal.message)
        await ml_audit(db, action="dataset_archived",
                       actor_username=_actor(current_user), actor_user_id=_actor_id(current_user),
                       object_type="ml_dataset", object_id=str(dataset_id),
                       reason=body.reason, after=outcome)
        await db.commit()
        return outcome
    except HTTPException:
        raise
    except Exception as e:
        raise _safe_500("dataset archive", e)


@router.get("/api/ml/datasets/{dataset_id}", tags=["ML Operations"], summary="Dataset Detail")
async def dataset_detail(
    dataset_id: str,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(ML_MANAGE),
):
    """One dataset version with its extraction record, manifest-derived lineage, feature-set limitations and the models trained from it. No storage path is ever serialized."""
    try:
        from db_models import MLDataset, MLModel
        from backend.ml.dataset_builder import read_manifest, serialize_dataset
        from backend.ml.dataset_definitions import feature_set_limitations
        try:
            row_uuid = uuid_mod.UUID(dataset_id)
        except (ValueError, TypeError):
            raise _error(422, "INVALID_DATASET_ID", "dataset_id must be a uuid")
        row = (await db.execute(select(MLDataset).where(MLDataset.id == row_uuid))).scalar_one_or_none()
        if row is None:
            raise _error(404, "DATASET_NOT_FOUND", "no such dataset")
        out = serialize_dataset(row)
        manifest = read_manifest(row) or {}
        out["manifest"] = {k: v for k, v in manifest.items()
                           if k in ("manifest_version", "definition", "split", "columns",
                                    "column_count", "parquet_bytes", "comparability",
                                    "quality", "created_at", "build_job_id")}
        out["feature_set_limitations"] = feature_set_limitations(row.feature_set_version)
        models = (await db.execute(
            select(MLModel.id, MLModel.version, MLModel.stage, MLModel.algorithm)
            .where(MLModel.dataset_id == row.id).order_by(MLModel.version))).all()
        out["models"] = [{"id": str(m[0]), "version": m[1], "stage": m[2], "algorithm": m[3]}
                         for m in models]
        out["immutable"] = bool(models)   # referenced by a registered model
        return out
    except HTTPException:
        raise
    except Exception as e:
        raise _safe_500("dataset detail", e)


# ---------------------------------------------------------------------------
# Training jobs
# ---------------------------------------------------------------------------

@router.post("/api/ml/training-jobs", tags=["ML Operations"],
             summary="Start Training Job", status_code=202)
async def create_training_job(
    body: TrainingRequest,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(ML_MANAGE),
    _csrf: None = Depends(require_mlops_csrf),
    _rl: None = Depends(rate_limited("ml_ops", heavy=True)),
):
    """Schedule model training as a background job (202 + job_id). Only behavior_anomaly_model is implemented; a concurrent job returns 409 TRAINING_ALREADY_RUNNING with the holder's job_id. Training produces a reviewable candidate, never a live replacement."""
    from backend.ml import trainer

    if body.model_type not in MODEL_TYPES:
        raise _error(422, "UNKNOWN_MODEL_TYPE",
                     f"model_type must be one of {MODEL_TYPES}")
    if body.model_type != MODEL_TYPE_BEHAVIOR_ANOMALY:
        raise _error(422, "MODEL_TYPE_NOT_IMPLEMENTED",
                     f"{body.model_type} is an interface reserved for future "
                     "releases; only behavior_anomaly_model trains today")

    job_id = f"mltrain-{uuid_mod.uuid4().hex[:8]}"
    running = trainer.try_acquire_training(job_id)
    if running is not None:
        raise _error(409, "TRAINING_ALREADY_RUNNING",
                     "a training job is already running", job_id=running)
    dlock = DistributedLock("ml-training-job", ttl_seconds=1800)
    if not await dlock.acquire(holder_label=job_id):
        trainer.release_training(job_id)
        raise _error(409, "TRAINING_ALREADY_RUNNING",
                     "a training job is already running (another worker)",
                     job_id=dlock.holder_hint or "unknown")
    try:
        from backend.core.task_history import task_history_manager
        task_id = await task_history_manager.create_job(
            job_id=job_id, task_type="ml_training",
            task_name="ML Anomaly Model Training",
            description=f"{body.model_type} / {body.algorithm}",
            created_by_user_id=_actor_id(current_user))

        async def _run_and_release():
            try:
                await trainer.run_training_job(
                    job_id, model_type=body.model_type,
                    algorithm=body.algorithm,
                    requested_by=_actor_id(current_user),
                    dataset_id=body.dataset_id, seed=body.seed,
                    hyperparameters=body.hyperparameters,
                    sampling_policy=body.sampling_policy)
            finally:
                await dlock.release()

        background_tasks.add_task(_run_and_release)
        await ml_audit(db, action="training_requested",
                       actor_username=_actor(current_user),
                       actor_user_id=_actor_id(current_user),
                       object_type="ml_training_job", object_id=job_id,
                       after={"model_type": body.model_type,
                              "algorithm": body.algorithm,
                              "dataset_id": body.dataset_id,
                              "seed": body.seed,
                              "hyperparameters": body.hyperparameters})
        await db.commit()
        return JSONResponse(status_code=202, content={
            "accepted": True, "job_id": job_id, "task_id": task_id,
            "status": "scheduled", "task_type": "ml_training"})
    except HTTPException:
        trainer.release_training(job_id)
        await dlock.release()
        raise
    except Exception as e:
        trainer.release_training(job_id)
        await dlock.release()
        raise _safe_500("training scheduling", e)


@router.get("/api/ml/training-jobs/{job_id}", tags=["ML Operations"],
            summary="Training Job Status")
async def get_training_job(
    job_id: str,
    current_user=Depends(ML_MANAGE),
):
    """Status of one training or feature-computation job from task history. 404 for any other job type."""
    from backend.core.task_history import task_history_manager
    task = await task_history_manager.get_task_by_job_id(job_id)
    if not task or task.get("task_type") not in ("ml_training", "ml_feature_computation"):
        raise HTTPException(status_code=404, detail="Job not found")
    resp = JSONResponse(content=task)
    resp.headers["Cache-Control"] = "no-store"
    return resp


@router.post("/api/ml/training-jobs/{job_id}/cancel", tags=["ML Operations"],
             summary="Cancel Training Job")
async def cancel_training_job(
    job_id: str,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(ML_MANAGE),
    _csrf: None = Depends(require_mlops_csrf),
):
    """Request cooperative cancellation of a running training job. 404 if no cancellable job holds that id; acceptance is audited."""
    from backend.ml import trainer
    accepted = trainer.request_cancel(job_id)
    if not accepted:
        raise HTTPException(status_code=404, detail="No cancellable job with that id")
    await ml_audit(db, action="training_cancelled",
                   actor_username=_actor(current_user),
                   actor_user_id=_actor_id(current_user),
                   object_type="ml_training_job", object_id=job_id)
    await db.commit()
    return {"success": True, "job_id": job_id, "status": "cancel_requested"}


# ---------------------------------------------------------------------------
# Registry lifecycle
# ---------------------------------------------------------------------------

@router.get("/api/ml/models", tags=["ML Operations"], summary="List Models")
async def list_models(
    model_type: Optional[str] = Query(default=None),
    stage: Optional[str] = Query(default=None),
    page: int = Query(default=1, ge=1, le=10000),
    page_size: int = Query(default=25, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    current_user=Depends(ML_MANAGE),
):
    """Paginated model registry listing, newest first, filterable by model type and stage."""
    try:
        from db_models import MLModel
        from backend.ml.registry_service import serialize_model_row
        query = select(MLModel)
        if model_type:
            query = query.where(MLModel.model_type == model_type)
        if stage:
            query = query.where(MLModel.stage == stage)
        total = (await db.execute(
            select(sa_func.count()).select_from(query.subquery()))).scalar() or 0
        rows = (await db.execute(
            query.order_by(MLModel.created_at.desc())
            .offset((page - 1) * page_size).limit(page_size))).scalars().all()
        return {"items": [serialize_model_row(r) for r in rows],
                "total": int(total), "page": page, "page_size": page_size}
    except Exception as e:
        raise _safe_500("model listing", e)


@router.get("/api/ml/models/{model_id}", tags=["ML Operations"], summary="Model Detail")
async def get_model(
    model_id: str,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(ML_MANAGE),
):
    """One model with its decision-threshold SETS (one row per scope/version:
    cutpoints {elevated, unusual, highly_unusual}, lifecycle candidate → active
    → retired; the active set is what inference bands with and what every
    prediction names as threshold_id)."""
    from backend.ml.registry_service import registry_service, serialize_model_row
    from backend.ml.threshold_service import threshold_service, serialize_threshold
    row = await registry_service.get_model(db, model_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Model not found")
    payload = serialize_model_row(row)
    payload["thresholds"] = [serialize_threshold(t) for t in await threshold_service.list_for_model(db, row.id)]
    return payload


@router.post("/api/ml/models/{model_id}/shadow-approve", tags=["ML Operations"],
             summary="Approve a VALIDATED model into SHADOW")
async def shadow_approve(
    model_id: str,
    body: ShadowApprovalRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(ML_MANAGE),
    _csrf: None = Depends(require_mlops_csrf),
    _rl: None = Depends(rate_limited("ml_ops", heavy=True)),
):
    """Explicit administrator approval — the ONLY path into shadow. The full
    approval payload (approver, reason, dataset, evaluation ref, checksum,
    schema, scope, rollback target) persists on the model row."""
    try:
        from backend.ml.registry_service import (
            RegistryError, registry_service, serialize_model_row)
        row = await registry_service.get_model(db, model_id)
        if row is None:
            raise HTTPException(status_code=404, detail="Model not found")
        approval = {
            "approved_by_user_id": _actor_id(current_user),
            "approved_by": _actor(current_user),
            "reason": body.reason,
            "dataset_version": str(row.dataset_id) if row.dataset_id else "unknown",
            "evaluation_report_ref": f"ml_models:{row.id}:evaluation_report",
            "artifact_checksum": row.artifact_hash,
            "feature_set_version": row.feature_set_version,
            "intended_scope": body.intended_scope,
            "rollback_target": ("POST /api/ml/shadow/stop archives this model; "
                                "rules remain the decision system throughout"),
        }
        payload = await registry_service.transition(
            db, model_id, to_stage="shadow", actor=_actor(current_user),
            actor_user_id=_actor_id(current_user), reason=body.reason,
            shadow_approval=approval)
        return payload
    except RegistryError as e:
        status_code = 409 if e.code in (
            "INVALID_TRANSITION", "ANOMALY_SHADOW_CAP",
            "SHADOW_APPROVAL_CHECKSUM_MISMATCH",
            "THRESHOLD_CANDIDATE_MISSING", "THRESHOLD_ARTIFACT_MISMATCH",
            "THRESHOLD_VERSION_CONFLICT") else 422
        raise _error(status_code, e.code, e.message)
    except HTTPException:
        raise
    except Exception as e:
        raise _safe_500("shadow approval", e)


@router.post("/api/ml/models/{model_id}/reject", tags=["ML Operations"],
             summary="Reject a Model")
async def reject_model(
    model_id: str,
    body: RejectRequest,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(ML_MANAGE),
    _csrf: None = Depends(require_mlops_csrf),
):
    """Move a model to the rejected stage with a recorded reason. Every registry error (unknown model, illegal transition) is returned as 409 with the registry's stable code."""
    try:
        from backend.ml.registry_service import RegistryError, registry_service
        return await registry_service.transition(
            db, model_id, to_stage="rejected", actor=_actor(current_user),
            actor_user_id=_actor_id(current_user), reason=body.reason)
    except RegistryError as e:
        raise _error(409, e.code, e.message)
    except Exception as e:
        raise _safe_500("model rejection", e)


@router.post("/api/ml/shadow/stop", tags=["ML Operations"],
             summary="Stop Shadow (rollback)")
async def stop_shadow(
    body: RejectRequest,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(ML_MANAGE),
    _csrf: None = Depends(require_mlops_csrf),
):
    """The rollback drill: archives the shadow model. Live decisions were
    never affected by it and remain rules-only."""
    try:
        from backend.ml.registry_service import registry_service
        stopped = await registry_service.stop_shadow(
            db, MODEL_TYPE_BEHAVIOR_ANOMALY, actor=_actor(current_user),
            actor_user_id=_actor_id(current_user), reason=body.reason)
        if stopped is None:
            raise HTTPException(status_code=404, detail="No shadow model is running")
        return {"success": True, "archived_model": stopped,
                "note": "rules remain the decision system; shadow observation ended"}
    except HTTPException:
        raise
    except Exception as e:
        raise _safe_500("shadow stop", e)


# ---------------------------------------------------------------------------
# Predictions + shadow comparisons + drift
# ---------------------------------------------------------------------------

@router.get("/api/ml/predictions", tags=["ML Operations"], summary="List Predictions")
async def list_predictions(
    subject_id: Optional[str] = Query(default=None, max_length=64),
    fallback_only: bool = Query(default=False),
    page: int = Query(default=1, ge=1, le=10000),
    page_size: int = Query(default=25, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    current_user=Depends(ML_MANAGE),
):
    """Paginated prediction log, newest first: requested vs actual mode, fallback reason, score and band, calibration status, missing features and latency. fallback_only=true restricts to fallbacks."""
    try:
        from db_models import MLPrediction
        query = select(MLPrediction)
        if subject_id:
            query = query.where(MLPrediction.subject_id == subject_id)
        if fallback_only:
            query = query.where(MLPrediction.fallback_reason.isnot(None))
        total = (await db.execute(
            select(sa_func.count()).select_from(query.subquery()))).scalar() or 0
        rows = (await db.execute(
            query.order_by(MLPrediction.created_at.desc())
            .offset((page - 1) * page_size).limit(page_size))).scalars().all()

        def _serialize(r):
            return {
                "id": str(r.id), "subject_id": r.subject_id,
                "model_version_label": r.model_version_label,
                "model_purpose": r.model_purpose,
                "requested_mode": r.requested_mode,
                "actual_mode_used": r.actual_mode_used,
                "fallback_reason": r.fallback_reason,
                "behavioral_anomaly_score": r.behavioral_anomaly_score,
                "ml_anomaly_band": r.ml_anomaly_band,
                "score_type": r.score_type, "is_probability": r.is_probability,
                "calibration_status": r.calibration_status,
                "missing_features": r.missing_features,
                "explanation": r.explanation,
                "latency_ms": r.latency_ms,
                "as_of_timestamp": (r.as_of_timestamp.isoformat() + "Z"
                                    if r.as_of_timestamp else None),
                "created_at": r.created_at.isoformat() + "Z" if r.created_at else None,
                # lineage (exact, persisted at prediction time — never inferred)
                "model_id": str(r.model_id) if r.model_id else None,
                "threshold_id": str(r.threshold_id) if r.threshold_id else None,
                "threshold_version": r.threshold_version,
                "snapshot_id": r.snapshot_id,
                "assessment_id": str(r.assessment_id) if r.assessment_id else None,
                "event_time": r.event_time.isoformat() + "Z" if r.event_time else None,
                "outcome_label_id": str(r.outcome_label_id) if r.outcome_label_id else None,
                "outcome_label": r.outcome_label,
                "outcome_recorded_at": (r.outcome_recorded_at.isoformat() + "Z"
                                        if r.outcome_recorded_at else None),
            }
        return {"items": [_serialize(r) for r in rows], "total": int(total),
                "page": page, "page_size": page_size}
    except Exception as e:
        raise _safe_500("prediction listing", e)


@router.get("/api/ml/shadow/summary", tags=["ML Operations"], summary="Shadow Summary")
async def shadow_summary(
    days: int = Query(default=7, ge=1, le=90),
    db: AsyncSession = Depends(get_db),
    current_user=Depends(ML_MANAGE),
):
    """Descriptive shadow-vs-rules aggregates over the last N days (1-90): disagreement counts, band distribution by rule severity, latency and failures. Reports insufficient_data when the window is empty."""
    try:
        from backend.ml.shadow_service import shadow_service
        return await shadow_service.shadow_summary(db, days=days)
    except Exception as e:
        raise _safe_500("shadow summary", e)


@router.get("/api/ml/shadow/evidence", tags=["ML Operations"],
            summary="Shadow evidence for offline mapping review")
async def shadow_evidence(
    days: int = Query(default=90, ge=1, le=365),
    model_id: Optional[str] = Query(default=None, max_length=64),
    db: AsyncSession = Depends(get_db),
    current_user=Depends(ML_MANAGE),
):
    """Per model/band: predictions, reviewed manual outcomes and their split, rule-severity x band crosstab, disagreement mix, score quantiles of reviewed positives vs negatives. Descriptive evidence for a human to validate a future ML->risk signal mapping; mapping_decision is always REQUIRES_VALIDATION."""
    try:
        from backend.ml.evaluation import shadow_evidence_report
        report = await shadow_evidence_report(db, days=days, model_id=model_id)
        if report.get("status") == "failed":
            raise _error(422, report["code"], "model_id must be a uuid")
        return report
    except HTTPException:
        raise
    except Exception as e:
        raise _safe_500("shadow evidence", e)


@router.get("/api/ml/drift/reports", tags=["ML Operations"], summary="Drift Reports")
async def drift_reports(
    page: int = Query(default=1, ge=1, le=10000),
    page_size: int = Query(default=25, ge=1, le=100),
    model_id: Optional[str] = Query(default=None, description="filter to one model"),
    db: AsyncSession = Depends(get_db),
    current_user=Depends(ML_MANAGE),
):
    """Paginated drift reports, newest first, optionally for one model. Drift reports are informational only — they never trigger deployment or retraining."""
    try:
        from db_models import MLDriftReport
        from backend.ml.drift_service import drift_service
        conds = []
        if model_id:
            try:
                conds.append(MLDriftReport.model_id == uuid_mod.UUID(str(model_id)))
            except (ValueError, TypeError):
                raise HTTPException(status_code=422, detail={"code": "INVALID_MODEL_ID"})
        total = (await db.execute(
            select(sa_func.count(MLDriftReport.id)).where(*conds))).scalar() or 0
        rows = (await db.execute(
            select(MLDriftReport).where(*conds).order_by(MLDriftReport.created_at.desc())
            .offset((page - 1) * page_size).limit(page_size))).scalars().all()
        return {"items": [drift_service.serialize_report(r) for r in rows],
                "total": int(total), "page": page, "page_size": page_size,
                "note": ("drift reports are observations only and never trigger "
                         "deployment or retraining")}
    except HTTPException:
        raise
    except Exception as e:
        raise _safe_500("drift reports", e)


@router.post("/api/ml/drift/run", tags=["ML Operations"],
             summary="Run Drift Check Now")
async def run_drift(
    db: AsyncSession = Depends(get_db),
    current_user=Depends(ML_MANAGE),
    _csrf: None = Depends(require_mlops_csrf),
    _rl: None = Depends(rate_limited("ml_ops", heavy=True)),
):
    """Run data-drift and prediction-drift analysis synchronously in the request and return both reports."""
    try:
        from backend.ml.drift_service import drift_service
        return await drift_service.run_all(db, job_id=f"manual-{uuid_mod.uuid4().hex[:8]}")
    except Exception as e:
        raise _safe_500("drift run", e)


# ---------------------------------------------------------------------------
# Retraining policy (scaffolded; DISABLED by default) + audit
# ---------------------------------------------------------------------------

@router.get("/api/ml/retraining-policy/{model_type}", tags=["ML Operations"],
            summary="Retraining Policy")
async def get_policy(
    model_type: str,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(ML_MANAGE),
):
    """The retraining policy for one model type. Scheduled retraining is scaffolded and disabled in this release."""
    from db_models import MLRetrainingPolicy
    row = (await db.execute(
        select(MLRetrainingPolicy)
        .where(MLRetrainingPolicy.model_type == model_type))).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="Unknown model type")
    return {
        "model_type": row.model_type, "enabled": row.enabled,
        "schedule_interval_hours": row.schedule_interval_hours,
        "min_new_labels": row.min_new_labels,
        "min_total_labels": row.min_total_labels,
        "cooldown_hours": row.cooldown_hours,
        "min_drift_reports": row.min_drift_reports,
        "last_triggered_at": (row.last_triggered_at.isoformat() + "Z"
                              if row.last_triggered_at else None),
        "note": ("scheduled retraining is scaffolded and DISABLED this "
                 "release; any triggered output would be a candidate "
                 "requiring approval, never an automatic replacement"),
    }


@router.put("/api/ml/retraining-policy/{model_type}", tags=["ML Operations"],
            summary="Update Retraining Policy")
async def update_policy(
    model_type: str,
    body: PolicyUpdateRequest,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(ML_MANAGE),
    _csrf: None = Depends(require_mlops_csrf),
):
    """Update retraining-policy parameters. Enabling scheduled retraining is refused with 409 SCHEDULED_RETRAINING_GATED — the stored policy is always kept disabled this release. Changes are audited."""
    try:
        from db_models import MLRetrainingPolicy
        row = (await db.execute(
            select(MLRetrainingPolicy)
            .where(MLRetrainingPolicy.model_type == model_type))).scalar_one_or_none()
        if row is None:
            raise HTTPException(status_code=404, detail="Unknown model type")
        if body.enabled:
            # Scheduled retraining stays off this release — honest refusal.
            raise _error(409, "SCHEDULED_RETRAINING_GATED",
                         "scheduled retraining is disabled in this release; "
                         "training runs manually via POST /api/ml/training-jobs")
        before = {"enabled": row.enabled,
                  "schedule_interval_hours": row.schedule_interval_hours}
        row.enabled = False
        row.schedule_interval_hours = body.schedule_interval_hours
        row.min_new_labels = body.min_new_labels
        row.min_total_labels = body.min_total_labels
        row.cooldown_hours = body.cooldown_hours
        row.min_drift_reports = max(2, body.min_drift_reports)
        row.updated_at = datetime.utcnow()
        row.updated_by = _actor(current_user)
        await ml_audit(db, action="policy_update", actor_username=_actor(current_user),
                       actor_user_id=_actor_id(current_user),
                       object_type="ml_retraining_policy", object_id=model_type,
                       before=before,
                       after={"enabled": row.enabled,
                              "schedule_interval_hours": row.schedule_interval_hours})
        await db.commit()
        return {"success": True, "model_type": model_type, "enabled": row.enabled}
    except HTTPException:
        raise
    except Exception as e:
        raise _safe_500("policy update", e)


@router.get("/api/ml/calls", tags=["ML Operations"], summary="ML-Ops Call Log")
async def ml_call_log(
    limit: int = Query(default=50, ge=1, le=500),
    errors_only: bool = Query(default=False),
    path_contains: Optional[str] = Query(default=None, max_length=120),
    current_user=Depends(ML_MANAGE),
):
    """Newest-first records of recent /api/ml/* calls (request id, actor, method, route, status, error code, duration, produced ids). The same request id is in every server log line of that call (`req=<id>`) and in the X-Request-ID response header the client received."""
    items = call_log.read_recent(limit, status_min=400 if errors_only else None,
                                 path_contains=path_contains)
    return {"items": items, "total": len(items), "limit": limit,
            "note": ("one record per ML-Ops call; bodies are sanitised summaries; "
                     "correlate with server logs by request_id")}


@router.get("/api/ml/audit", tags=["ML Operations"], summary="ML Audit Log")
async def ml_audit_log(
    page: int = Query(default=1, ge=1, le=10000),
    page_size: int = Query(default=25, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    current_user=Depends(ML_MANAGE),
):
    """Paginated ML audit trail (action, object, actor, reason), newest first. The stored before/after payloads are not returned here."""
    try:
        from db_models import MLAuditLog
        total = (await db.execute(select(sa_func.count(MLAuditLog.id)))).scalar() or 0
        rows = (await db.execute(
            select(MLAuditLog).order_by(MLAuditLog.created_at.desc())
            .offset((page - 1) * page_size).limit(page_size))).scalars().all()
        return {"items": [{
            "id": r.id, "action": r.action, "object_type": r.object_type,
            "object_id": r.object_id, "actor_username": r.actor_username,
            "reason": r.reason,
            "created_at": r.created_at.isoformat() + "Z" if r.created_at else None,
        } for r in rows], "total": int(total), "page": page, "page_size": page_size}
    except Exception as e:
        raise _safe_500("ml audit listing", e)

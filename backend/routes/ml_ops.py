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
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import select, func as sa_func
from sqlalchemy.ext.asyncio import AsyncSession

from db_connection import get_db
from backend.auth.auth_service import require_capability
from backend.auth.capabilities import Capability
from backend.core.rate_limiter import rate_limited
from backend.ml.audit import ml_audit
from backend.ml.constants import (
    IMPLEMENTED_MODEL_TYPES, MODEL_TYPE_BEHAVIOR_ANOMALY, MODEL_TYPES,
    all_optional_capabilities, model_type_status)
from backend.ml.model_specs import get_model_spec
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
                # a refused mode change records WHY (gate codes only)
                if detail.get("error_code") == "MODE_GATED":
                    entry["gates"] = [str(r.get("code")) for r in (detail.get("reasons") or [])
                                      if isinstance(r, dict)][:12]
                call_log.record(entry)
                raise
            except RequestValidationError as exc:
                # The client receives 422 from the app-level handler; record
                # it as the client error it is, never as a server fault.
                entry.update({"status": 422, "ms": timer.ms,
                              "error_code": "VALIDATION_ERROR",
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


async def record_mode_rejection(db, report: Dict[str, Any], *, actor_username: str,
                                actor_user_id: Optional[int], reason: Optional[str],
                                ip_address: Optional[str]) -> None:
    """A REFUSED mode change is a governance event: audited (gate codes) and
    counted — a refusal that leaves no durable trace is invisible later."""
    try:
        await ml_audit(db, action="mode_change_rejected", actor_username=actor_username,
                       actor_user_id=actor_user_id, object_type="ml_config",
                       object_id="ML_DECISION_MODE",
                       before={"mode": report.get("current_mode")},
                       after={"requested_mode": report.get("target_mode"),
                              "gates": [r["code"] for r in report.get("reasons", [])]},
                       reason=reason, ip_address=ip_address)
        await db.commit()
    except Exception:
        logger.debug("[ML_OPS] mode rejection audit failed", exc_info=True)
    try:
        from backend.ml import metrics as ml_metrics
        ml_metrics.observe_mode_rejection(report.get("target_mode"))
    except Exception:
        pass


def _current_request_id() -> Optional[str]:
    """The request id of the call being handled (for job rows), or None."""
    try:
        from utils.logging import request_id_var
        value = request_id_var.get()
        return value if value and value != "-" else None
    except Exception:
        return None


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


from backend.ml.run_spec import RunOptions, PipelineConfiguration


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
    pipeline_id: Optional[uuid_mod.UUID] = None
    run_options: RunOptions = Field(default_factory=RunOptions)


class PipelineCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=128, pattern=r"^[\w .-]+$")
    configuration: PipelineConfiguration


class OfflinePromotionRequest(BaseModel):
    reason: str = Field(min_length=3, max_length=500)
    artifact_checksum: str = Field(pattern="^[a-f0-9]{64}$")


@router.get("/api/ml/capabilities", tags=["ML Operations"])
async def platform_capabilities(current_user=Depends(ML_MANAGE)):
    from backend.ml.capabilities import capability_registry
    from starlette.concurrency import run_in_threadpool
    from config import settings
    capabilities = await run_in_threadpool(capability_registry)
    if capabilities["mlflow"]["operational"] and settings.MLFLOW_TRACKING_URI:
        try:
            from backend.ml.mlflow_tracking import client
            c = await run_in_threadpool(client)
            await run_in_threadpool(c.search_experiments, max_results=1)
        except Exception:
            capabilities["mlflow"].update(status="Misconfigured", operational=False,
                action="Tracking service is unreachable or access was refused. Check HTTPS URL, service credentials and network access.")
    return JSONResponse({"items": capabilities, "permissions": {"manage": "admin", "promote": "admin", "settings": "existing settings RBAC"},
        "limits": {"threads": settings.ML_TRAIN_MAX_THREADS, "optuna_trials": settings.ML_OPTUNA_MAX_TRIALS,
            "optuna_timeout_seconds": settings.ML_OPTUNA_TIMEOUT_SECONDS, "shap_rows": settings.ML_SHAP_MAX_ROWS},
        "storage": {"datasets": "immutable registered snapshots", "tracking": "HTTPS service" if settings.MLFLOW_TRACKING_URI else "managed persistent SQL store", "dvc": "not used; existing snapshot storage is authoritative"}}, headers={"Cache-Control": "no-store"})


@router.get("/api/ml/pipelines", tags=["ML Operations"])
async def list_pipeline_versions(db: AsyncSession = Depends(get_db), current_user=Depends(ML_MANAGE)):
    from db_models import MLPipelineVersion
    rows = (await db.execute(select(MLPipelineVersion).order_by(MLPipelineVersion.created_at.desc()).limit(100))).scalars().all()
    return {"items": [{"id": str(r.id), "name": r.name, "version": r.version, "configuration": r.configuration} for r in rows]}


@router.post("/api/ml/pipelines", tags=["ML Operations"], status_code=201)
async def create_pipeline_version(body: PipelineCreateRequest, db: AsyncSession = Depends(get_db), current_user=Depends(ML_MANAGE),
                                  _csrf=Depends(require_mlops_csrf), _rl=Depends(rate_limited("ml_ops"))):
    from db_models import MLPipelineVersion
    from sqlalchemy import text
    # Version allocation is serialized per name; published configurations are never updated.
    await db.execute(text("SELECT pg_advisory_xact_lock(hashtext(:key))"), {"key": "ml-pipeline:" + body.name})
    version = ((await db.execute(select(sa_func.max(MLPipelineVersion.version)).where(MLPipelineVersion.name == body.name))).scalar() or 0) + 1
    row = MLPipelineVersion(id=uuid_mod.uuid4(), name=body.name, version=version, configuration=body.configuration.model_dump(), created_by=_actor_id(current_user))
    db.add(row)
    await ml_audit(db, action="pipeline_version_created", actor_username=_actor(current_user), actor_user_id=_actor_id(current_user),
                   object_type="ml_pipeline", object_id=str(row.id), after={"name": row.name, "version": version, "configuration": row.configuration})
    await db.commit()
    return {"id": str(row.id), "name": row.name, "version": version, "configuration": row.configuration}


@router.get("/api/ml/experiments", tags=["ML Operations"])
async def list_experiments(db: AsyncSession = Depends(get_db), current_user=Depends(ML_MANAGE)):
    from db_models import MLTrackingRun
    from backend.ml.mlflow_tracking import serialize
    rows = (await db.execute(select(MLTrackingRun).order_by(MLTrackingRun.updated_at.desc()).limit(100))).scalars().all()
    return {"items": [serialize(row) for row in rows]}


@router.get("/api/ml/comparisons", tags=["ML Operations"])
async def compare_experiments(model_ids: List[uuid_mod.UUID] = Query(..., min_length=2, max_length=5), db: AsyncSession = Depends(get_db), current_user=Depends(ML_MANAGE)):
    from db_models import MLModel
    rows = (await db.execute(select(MLModel).where(MLModel.id.in_(model_ids)))).scalars().all()
    if len(rows) != len(set(model_ids)):
        raise _error(404, "MODEL_NOT_FOUND", "One or more selected models no longer exist")
    contracts = {(str(r.dataset_id), r.model_type, ((r.training_config or {}).get("pipeline") or {}).get("target")) for r in rows}
    return {"comparable": len(contracts) == 1, "note": "Compare the same dataset version, task and target; lower regression error is better. This comparison never approves deployment.",
            "items": [{"id": str(r.id), "version": r.version, "algorithm": r.algorithm, "dataset_id": str(r.dataset_id), "metrics": r.evaluation_report, "stage": r.stage} for r in rows]}


@router.post("/api/ml/experiments/{job_id}/retry", tags=["ML Operations"], status_code=202)
async def retry_tracking(job_id: str, body: PauseRequest, db: AsyncSession = Depends(get_db), current_user=Depends(ML_MANAGE),
                         _csrf=Depends(require_mlops_csrf), _rl=Depends(rate_limited("ml_ops", heavy=True))):
    from db_models import MLTrackingRun
    from backend.ml.job_service import enqueue_ml_job, MLJobConflict
    if not await db.get(MLTrackingRun, job_id):
        raise _error(404, "EXPERIMENT_NOT_FOUND", "No experiment exists for this job")
    try:
        out = await enqueue_ml_job(db, kind="tracking", payload={"training_job_id": job_id}, description="Administrator requested MLflow synchronization", created_by_user_id=_actor_id(current_user))
    except MLJobConflict:
        raise _error(409, "TRACKING_SYNC_ACTIVE", "A synchronization is already queued or running. Wait and refresh.")
    await ml_audit(db, action="mlflow_retry_requested", actor_username=_actor(current_user), actor_user_id=_actor_id(current_user), object_type="ml_training_job", object_id=job_id, reason=body.reason)
    await db.commit()
    return out


@router.post("/api/ml/models/{model_id}/promote", tags=["ML Operations"])
async def promote_offline_model(model_id: uuid_mod.UUID, body: OfflinePromotionRequest, db: AsyncSession = Depends(get_db), current_user=Depends(ML_MANAGE),
                                 _csrf=Depends(require_mlops_csrf), _rl=Depends(rate_limited("ml_ops"))):
    from backend.ml.registry_service import registry_service, RegistryError, validate_artifact
    from starlette.concurrency import run_in_threadpool
    row = await registry_service.get_model(db, str(model_id))
    if row is None:
        raise _error(404, "MODEL_NOT_FOUND", "Model not found")
    if get_model_spec(row.model_type).serving_mode not in ("offline_ranking", "offline_regression"):
        raise _error(409, "PROMOTION_GATED", "Use the existing governed shadow approval for security anomaly models")
    if body.artifact_checksum != row.artifact_hash:
        raise _error(409, "ARTIFACT_CHECKSUM_MISMATCH", "Refresh the model and review the current artifact checksum")
    try:
        await run_in_threadpool(validate_artifact, row.artifact_path, expected_hash=row.artifact_hash, expected_feature_names=row.feature_names, expected_dependencies=row.dependency_versions)
        return await registry_service.transition(db, str(row.id), to_stage="approved", actor=_actor(current_user), actor_user_id=_actor_id(current_user), reason=body.reason)
    except RegistryError as exc:
        raise _error(409, exc.code, exc.message)


async def _explanation_file(db, model_id, filename):
    from pathlib import Path
    import hashlib
    from config import settings
    from db_models import MLModel
    row = await db.get(MLModel, model_id)
    if row is None:
        raise _error(404, "MODEL_NOT_FOUND", "Model not found")
    exports = ((row.evaluation_report or {}).get("shap") or {}).get("exports", {})
    if filename not in exports:
        raise _error(404, "EXPLANATION_NOT_RECORDED", "SHAP was not recorded for this run. Enable it on a compatible new training run.")
    root = (Path(settings.ML_ARTIFACT_DIR).resolve() / "explanations").resolve()
    path = (root / str(model_id) / filename).resolve()
    if not path.is_relative_to(root) or not path.is_file():
        raise _error(409, "EXPLANATION_FILE_MISSING", "Restore explanation artifacts from backup or train a new run")
    if hashlib.sha256(path.read_bytes()).hexdigest() != exports[filename]:
        raise _error(409, "EXPLANATION_CHECKSUM_MISMATCH", "Explanation artifact integrity check failed. Restore the original file.")
    return path


@router.get("/api/ml/models/{model_id}/explanations", tags=["ML Operations"])
async def model_explanations(model_id: uuid_mod.UUID, sample: int = Query(0, ge=0, le=999), db: AsyncSession = Depends(get_db), current_user=Depends(ML_MANAGE)):
    import json
    path = await _explanation_file(db, model_id, "shap.json")
    data = json.loads(path.read_text(encoding="utf-8"))
    if sample >= data["rows"]:
        raise _error(422, "EXPLANATION_SAMPLE_OUT_OF_RANGE", "Choose a recorded sample index")
    return {"rows": data["rows"], "feature_names": data["feature_names"], "global_importance": data["global_importance"], "output_space": data["output_space"],
            "sample": {"index": sample, "id": data["sample_ids"][sample], "contributions": data["values"][sample], "features": data["data"][sample],
                       "base_value": data["base_values"][sample] if isinstance(data["base_values"], list) else data["base_values"]}}


@router.get("/api/ml/models/{model_id}/explanations/download/{filename}", tags=["ML Operations"])
async def download_explanation(model_id: uuid_mod.UUID, filename: str, db: AsyncSession = Depends(get_db), current_user=Depends(ML_MANAGE)):
    from fastapi.responses import FileResponse
    if filename not in ("shap.json", "shap-global.png", "shap-individual-0.png"):
        raise _error(404, "EXPORT_NOT_FOUND", "Select a recorded SHAP export")
    path = await _explanation_file(db, model_id, filename)
    return FileResponse(path, filename=filename, media_type="application/json" if filename.endswith("json") else "image/png", headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})


class RelationalScoreRequest(BaseModel):
    model_type: str = Field(..., pattern="^(coappearance_anomaly_model|social_graph_anomaly_model)$")
    identity_id: str = Field(..., min_length=36, max_length=36)
    related_identity_id: Optional[str] = Field(default=None, min_length=36, max_length=36)
    model_id: Optional[str] = Field(default=None, min_length=36, max_length=36)


class ThreatRankRequest(BaseModel):
    identity_ids: List[str] = Field(..., min_length=1, max_length=200)
    model_id: Optional[str] = Field(default=None, min_length=36, max_length=36)


_SELECTION_METHODS = ("natural", "stratified_by_band", "top_scores", "random", "manual")
_ENTRY_POINTS = ("security_intelligence", "ml_ops", "api")


def _selection_metadata(raw, *, default_entry_point: str = "api",
                        default_revealed=None):
    """Bounded, typed selection metadata: {method, band, sampling_probability,
    reason, selected_at, entry_point, ml_observation_revealed}. The last two
    record HOW the outcome was obtained - from which page and whether the ML
    observation had been revealed to the reviewer first - so evidence reports
    can keep blind and revealed reviews apart."""
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise _error(422, "INVALID_SELECTION", "selection must be an object")
    if not raw and default_revealed is None:
        return None
    method = str(raw.get("method") or "natural")
    if method not in _SELECTION_METHODS:
        raise _error(422, "INVALID_SELECTION", f"selection.method must be one of {_SELECTION_METHODS}")
    entry_point = str(raw.get("entry_point") or default_entry_point)
    if entry_point not in _ENTRY_POINTS:
        raise _error(422, "INVALID_SELECTION", f"selection.entry_point must be one of {_ENTRY_POINTS}")
    revealed = raw.get("ml_observation_revealed", default_revealed)
    if revealed is not None and not isinstance(revealed, bool):
        raise _error(422, "INVALID_SELECTION", "selection.ml_observation_revealed must be true/false")
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
            "selected_at": iso_utc(datetime.utcnow()),
            "entry_point": entry_point, "ml_observation_revealed": revealed}


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
            # Capability contract per model type: the UI derives algorithms,
            # dataset kind, entity type and score semantics from THIS list.
            "model_types": [model_type_status(t) for t in MODEL_TYPES],
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
        from backend.ml.decision_service import decision_service, mode_gated_detail
        report = await decision_service.mode_gate_report(db, body.mode)
        if not report["allowed"]:
            await record_mode_rejection(
                db, report, actor_username=_actor(current_user), actor_user_id=_actor_id(current_user),
                reason=body.reason, ip_address=(request.client.host if request.client else None))
            raise HTTPException(status_code=409, detail=mode_gated_detail(report))

        from backend.ml.mode_service import change_decision_mode
        return await change_decision_mode(
            db, target_mode=body.mode, action="mode_change",
            actor_username=_actor(current_user), actor_user_id=_actor_id(current_user),
            reason=body.reason,
            ip_address=(request.client.host if request.client else None),
        )
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
        from backend.ml.mode_service import change_decision_mode
        result = await change_decision_mode(
            db, target_mode="rules", action="pause",
            actor_username=_actor(current_user), actor_user_id=_actor_id(current_user),
            reason=body.reason,
            ip_address=(request.client.host if request.client else None),
        )
        result["note"] = "rules engine restored as the sole decision path"
        return result
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
    """Persist a feature-snapshot command for the independent ML worker."""
    try:
        from backend.ml.job_service import enqueue_ml_job, MLJobConflict
        try:
            outcome = await enqueue_ml_job(
                db, kind="collection", payload={"full_rebuild": bool(full_rebuild)},
                description="Point-in-time feature snapshots from operational data",
                created_by_user_id=_actor_id(current_user),
                request_id=_current_request_id(),
            )
        except MLJobConflict as conflict:
            raise _error(409, "JOB_ALREADY_RUNNING",
                         "a feature collection job is already active",
                         job_id=conflict.existing.get("job_id"))
        await ml_audit(db, action="collection_requested", actor_username=_actor(current_user),
                       actor_user_id=_actor_id(current_user), object_type="ml_collection_job",
                       object_id=str(outcome.get("job_id")),
                       after={"full_rebuild": bool(full_rebuild)})
        await db.commit()
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
            selection=_selection_metadata(body.selection, default_entry_point="ml_ops",
                                          default_revealed=True))
        status_code = 200 if payload.get("deduplicated") else 201
        return JSONResponse(status_code=status_code, content=jsonable(payload))
    except ValueError as e:
        from backend.ml.labeling_service import LabelConflict
        if isinstance(e, LabelConflict):
            raise _error(409, "LABEL_CONFLICT", str(e),
                         existing_label_id=e.existing_id, existing_label=e.existing_label)
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
        message = str(e)
        code = ("WEAK_LABEL_NOT_REVIEWABLE" if message.startswith("WEAK_LABEL_NOT_REVIEWABLE")
                else "SELF_REVIEW_REFUSED" if message.startswith("SELF_REVIEW_REFUSED")
                else "INVALID_REVIEW")
        raise _error(422 if code in ("WEAK_LABEL_NOT_REVIEWABLE", "SELF_REVIEW_REFUSED") else 409, code, message)
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
             status_code=202)
async def build_dataset_endpoint(
    body: DatasetBuildRequest,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(ML_MANAGE),
    _csrf: None = Depends(require_mlops_csrf),
    _rl: None = Depends(rate_limited("ml_ops", heavy=True)),
):
    """Validate and enqueue an immutable dataset build; execution is worker-owned."""
    try:
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
        from backend.ml.job_service import enqueue_ml_job, MLJobConflict
        payload = {
            "name": body.name, "kind": body.kind,
            "definition": (definition.name if definition else None),
            "definition_version": (definition.version if definition else None),
            "time_range_start": iso_utc(start) if start else None,
            "time_range_end": iso_utc(end) if end else None,
            "sampling_policy": body.sampling_policy,
            "split_strategy": body.split_strategy,
            "created_by": _actor_id(current_user),
        }
        try:
            outcome = await enqueue_ml_job(
                db, kind="dataset", payload=payload,
                description=f"{body.name} / {body.kind}",
                created_by_user_id=_actor_id(current_user),
                request_id=_current_request_id(),
            )
        except MLJobConflict as conflict:
            raise _error(409, "JOB_ALREADY_RUNNING",
                         "a dataset build is already active",
                         job_id=conflict.existing.get("job_id"))
        await ml_audit(db, action="dataset_build_requested",
                       actor_username=_actor(current_user),
                       actor_user_id=_actor_id(current_user),
                       object_type="ml_dataset_job", object_id=outcome["job_id"],
                       after={"name": body.name, "kind": body.kind,
                              "definition": payload["definition"]})
        await db.commit()
        return JSONResponse(status_code=202, content=jsonable(outcome))
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
             summary="Verify legacy datasets and record their file hashes",
             status_code=202)
async def backfill_dataset_hashes_endpoint(
    db: AsyncSession = Depends(get_db),
    current_user=Depends(ML_MANAGE),
    _csrf: None = Depends(require_mlops_csrf),
    _rl: None = Depends(rate_limited("ml_ops", heavy=True)),
):
    """Enqueue verification of legacy dataset files and their logical checksums."""
    try:
        from backend.ml.job_service import enqueue_ml_job, MLJobConflict
        try:
            outcome = await enqueue_ml_job(
                db, kind="backfill", payload={},
                description="Verify legacy dataset files and record hashes",
                created_by_user_id=_actor_id(current_user),
                request_id=_current_request_id(),
            )
        except MLJobConflict as conflict:
            raise _error(409, "JOB_ALREADY_RUNNING",
                         "dataset hash verification is already active",
                         job_id=conflict.existing.get("job_id"))
        await ml_audit(db, action="dataset_hash_backfill_requested",
                       actor_username=_actor(current_user),
                       actor_user_id=_actor_id(current_user),
                       object_type="ml_dataset_job", object_id=outcome["job_id"])
        await db.commit()
        return JSONResponse(status_code=202, content=outcome)
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


@router.get("/api/ml/datasets/{dataset_id}/explorer", tags=["ML Operations"])
@router.get("/api/ml/datasets/{dataset_id}/validation-report", tags=["ML Operations"])
async def dataset_explorer(
    request: Request,
    dataset_id: uuid_mod.UUID,
    page: int = Query(1, ge=1, le=4000),
    page_size: int = Query(25, ge=1, le=100),
    split: Optional[str] = Query(None, pattern="^(train|val|test)$"),
    label: Optional[str] = Query(None, pattern="^(positive|negative|unknown|unlabelled)$"),
    q: str = Query("", max_length=200),
    db: AsyncSession = Depends(get_db),
    current_user=Depends(ML_MANAGE),
):
    """Bounded artifact preview; downloads contain the original validation report."""
    from db_models import MLDataset
    from backend.ml.dataset_explorer import explore_dataset
    from starlette.concurrency import run_in_threadpool
    from config import settings

    row = (await db.execute(select(MLDataset).where(MLDataset.id == dataset_id))).scalar_one_or_none()
    if row is None:
        raise _error(404, "DATASET_NOT_FOUND", "No such dataset. Refresh the dataset list.")
    if request.url.path.endswith("/validation-report"):
        return JSONResponse(jsonable({"dataset_id": str(row.id), "version": row.version,
            "checksum": row.checksum, "validation_report": row.quality_report,
            "missing_value_report": row.missing_value_report}), headers={
                "Cache-Control": "no-store",
                "Content-Disposition": f'attachment; filename="validation-{dataset_id}.json"'})
    try:
        result = await run_in_threadpool(explore_dataset, row, settings.ML_ARTIFACT_DIR,
                                        page=page, page_size=page_size, split=split, label=label, query=q)
        return JSONResponse(jsonable(result), headers={"Cache-Control": "no-store"})
    except FileNotFoundError:
        raise _error(409, "DATASET_FILE_MISSING", "Artifact unavailable. Restore it from backup or build a new dataset version; the saved validation report remains available.")
    except ValueError:
        raise _error(409, "DATASET_UNREADABLE", "Dataset cannot be inspected. Check artifact integrity or build a new version.")
    except Exception as exc:
        raise _safe_500("dataset explorer", exc)


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
    db: AsyncSession = Depends(get_db),
    current_user=Depends(ML_MANAGE),
    _csrf: None = Depends(require_mlops_csrf),
    _rl: None = Depends(rate_limited("ml_ops", heavy=True)),
):
    """Persist training for the independent worker; never execute it in the API."""
    pipeline = None
    if body.pipeline_id:
        from db_models import MLPipelineVersion
        saved = await db.get(MLPipelineVersion, body.pipeline_id)
        if saved is None:
            raise _error(404, "PIPELINE_NOT_FOUND", "Refresh pipeline configurations and select an existing version")
        config = PipelineConfiguration.model_validate(saved.configuration)
        body.model_type, body.algorithm = config.model_type, config.algorithm
        pipeline = {**config.model_dump(), "id": str(saved.id), "name": saved.name, "version": saved.version}
    if body.model_type == "tabular_regression_model" and (not pipeline or not body.dataset_id):
        raise _error(422, "REGRESSION_CONFIGURATION_REQUIRED", "Regression needs a saved pipeline with an explicit numeric target and a dataset version")
    try:
        from backend.ml.registry_service import RegistryError
        body.run_options.check_capabilities(body.algorithm)
        from backend.ml.trainer import resolve_hyperparameters
        resolve_hyperparameters(body.algorithm, body.hyperparameters)
    except RegistryError as exc:
        raise _error(422, exc.code, exc.message)
    if body.model_type not in MODEL_TYPES:
        raise _error(422, "UNKNOWN_MODEL_TYPE",
                     f"model_type must be one of {MODEL_TYPES}")
    if body.model_type not in IMPLEMENTED_MODEL_TYPES:
        raise _error(422, "MODEL_TYPE_NOT_IMPLEMENTED",
                     f"{body.model_type} is not implemented")
    spec = get_model_spec(body.model_type)
    if body.algorithm not in spec.algorithms:
        raise _error(422, "ALGORITHM_NOT_SUPPORTED_FOR_MODEL",
                     f"{body.algorithm} is not valid for {body.model_type}; "
                     f"choose one of {spec.algorithms}")

    try:
        from backend.ml.job_service import enqueue_ml_job, MLJobConflict
        payload = {
            "model_type": body.model_type, "algorithm": body.algorithm,
            "requested_by": _actor_id(current_user),
            "dataset_id": body.dataset_id, "seed": body.seed,
            "hyperparameters": body.hyperparameters,
            "sampling_policy": body.sampling_policy,
            "pipeline": pipeline, "run_options": body.run_options.model_dump(),
        }
        try:
            outcome = await enqueue_ml_job(
                db, kind="training", payload=payload,
                description=f"{body.model_type} / {body.algorithm}",
                created_by_user_id=_actor_id(current_user),
                request_id=_current_request_id(),
            )
        except MLJobConflict as conflict:
            raise _error(409, "TRAINING_ALREADY_RUNNING",
                         "a training job is already active",
                         job_id=conflict.existing.get("job_id"))
        await ml_audit(db, action="training_requested",
                       actor_username=_actor(current_user),
                       actor_user_id=_actor_id(current_user),
                       object_type="ml_training_job", object_id=outcome["job_id"],
                       after={"model_type": body.model_type,
                              "algorithm": body.algorithm,
                              "dataset_id": body.dataset_id,
                              "seed": body.seed,
                              "hyperparameters": body.hyperparameters, "pipeline": pipeline,
                              "run_options": body.run_options.model_dump()})
        await db.commit()
        return JSONResponse(status_code=202, content=outcome)
    except HTTPException:
        raise
    except Exception as e:
        raise _safe_500("training scheduling", e)


@router.post("/api/ml/score/relational", tags=["ML Operations"],
             summary="Run an observational relational shadow score")
async def score_relational_model(
    body: RelationalScoreRequest,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(ML_MANAGE),
    _csrf: None = Depends(require_mlops_csrf),
    _rl: None = Depends(rate_limited("ml_ops", heavy=True)),
):
    """Score a pair or graph node without changing any live decision."""
    from backend.ml.model_scoring_service import score_relational_subject
    from backend.ml.registry_service import RegistryError
    try:
        result = await score_relational_subject(
            db, model_type=body.model_type, identity_id=body.identity_id,
            related_identity_id=body.related_identity_id, model_id=body.model_id)
        await ml_audit(db, action="relational_shadow_scored",
                       actor_username=_actor(current_user),
                       actor_user_id=_actor_id(current_user),
                       object_type="ml_model", object_id=result["model_id"],
                       after={"model_type": body.model_type,
                              "subject_id": result["subject_id"],
                              "applied_to_live_result": False})
        await db.commit()
        return result
    except RegistryError as exc:
        raise _error(409 if "MODEL" in exc.code or "THRESHOLD" in exc.code else 422,
                     exc.code, exc.message)
    except (ValueError, TypeError) as exc:
        raise _error(422, "INVALID_SUBJECT_ID", str(exc))
    except HTTPException:
        raise
    except Exception as exc:
        raise _safe_500("relational model scoring", exc)


@router.post("/api/ml/rank/threat-review", tags=["ML Operations"],
             summary="Rank identities for analyst review")
async def rank_threat_review(
    body: ThreatRankRequest,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(ML_MANAGE),
    _csrf: None = Depends(require_mlops_csrf),
    _rl: None = Depends(rate_limited("ml_ops", heavy=True)),
):
    """Offline analyst prioritisation only; never writes a threat decision."""
    from backend.ml.model_scoring_service import rank_identities
    result = await rank_identities(db, body.identity_ids, model_id=body.model_id)
    await ml_audit(db, action="threat_review_ranked",
                   actor_username=_actor(current_user),
                   actor_user_id=_actor_id(current_user),
                   object_type="ml_model", object_id=body.model_id or "latest_validated",
                   after={"requested": len(body.identity_ids), "scored": result["scored"],
                          "failed": result["failed"], "applied_to_live_result": False})
    await db.commit()
    return result


@router.get("/api/ml/jobs", tags=["ML Operations"], summary="List ML Jobs")
async def list_ml_jobs_endpoint(
    status: Optional[str] = Query(default=None, max_length=100),
    limit: int = Query(default=50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
    current_user=Depends(ML_MANAGE),
):
    """Reconnectable queue state. Status is a comma-separated allowlist."""
    from backend.ml.job_service import list_ml_jobs, ml_worker_health
    from config import settings as app_settings
    allowed = {"scheduled", "running", "completed", "failed", "cancelled"}
    statuses = None
    if status:
        statuses = [part.strip() for part in status.split(",") if part.strip()]
        if not statuses or any(part not in allowed for part in statuses):
            raise _error(422, "INVALID_JOB_STATUS", "unknown ML job status")
    items = await list_ml_jobs(statuses=statuses, limit=limit)
    worker = await ml_worker_health(
        db, lease_seconds=int(app_settings.ML_JOB_LEASE_SECONDS)
    )
    resp = JSONResponse(content={"items": items, "total": len(items), "worker": worker})
    resp.headers["Cache-Control"] = "no-store"
    return resp


@router.get("/api/ml/training-jobs/{job_id}", tags=["ML Operations"],
            summary="Training Job Status")
@router.get("/api/ml/jobs/{job_id}", tags=["ML Operations"],
            summary="ML Job Status")
async def get_training_job(
    job_id: str,
    current_user=Depends(ML_MANAGE),
):
    """Status of one durable ML job. The training-jobs path is retained for compatibility."""
    from backend.core.task_history import task_history_manager
    from backend.ml.job_service import ML_TASK_TYPES
    task = await task_history_manager.get_task_by_job_id(job_id)
    if not task or task.get("task_type") not in ML_TASK_TYPES:
        raise HTTPException(status_code=404, detail="Job not found")
    resp = JSONResponse(content=task)
    resp.headers["Cache-Control"] = "no-store"
    return resp


@router.post("/api/ml/training-jobs/{job_id}/cancel", tags=["ML Operations"],
             summary="Cancel Training Job")
@router.post("/api/ml/jobs/{job_id}/cancel", tags=["ML Operations"],
             summary="Cancel ML Job")
async def cancel_training_job(
    job_id: str,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(ML_MANAGE),
    _csrf: None = Depends(require_mlops_csrf),
):
    """Persist cancellation so it survives API/worker process boundaries."""
    from backend.core.task_history import task_history_manager
    from backend.ml.job_service import ML_TASK_TYPES, job_kind
    task = await task_history_manager.get_task_by_job_id(job_id)
    if (not task or task.get("task_type") not in ML_TASK_TYPES
            or task.get("status") not in ("running", "scheduled")):
        raise HTTPException(status_code=404, detail="No cancellable ML job with that id")
    ok, outcome = await task_history_manager.request_cancel(int(task["id"]))
    if not ok:
        raise HTTPException(status_code=404, detail=f"No cancellable ML job with that id ({outcome})")
    kind = job_kind(task.get("task_type"))
    action, object_type = f"{kind}_cancel_requested", f"ml_{kind}_job"
    await ml_audit(db, action=action,
                   actor_username=_actor(current_user),
                   actor_user_id=_actor_id(current_user),
                   object_type=object_type, object_id=job_id)
    await db.commit()
    return {"success": True, "job_id": job_id, "status": outcome}


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
    from db_models import MLTrackingRun
    from backend.ml.mlflow_tracking import serialize as serialize_tracking
    tracking = await db.get(MLTrackingRun, row.training_job_id) if row.training_job_id else None
    payload["tracking"] = serialize_tracking(tracking) if tracking else {"status": "not_recorded"}
    return payload


@router.post("/api/ml/models/{model_id}/readiness", tags=["ML Operations"],
             summary="Recompute Model Readiness")
async def recompute_model_readiness(
    model_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(ML_MANAGE),
    _csrf: None = Depends(require_mlops_csrf),
    _rl: None = Depends(rate_limited("ml_ops", heavy=True)),
):
    """Recompute BOTH readiness gates for a registered model from its artifact,
    dataset, reviewed evidence and the current mapping status (no retraining),
    and record them on the model as computed_post_hoc. The scientific gate
    uses only configured minimums - nothing here invents a threshold or marks
    a model validated. Audited."""
    from backend.ml.readiness import compute_model_readiness
    from backend.ml.registry_service import registry_service
    row = await registry_service.get_model(db, model_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Model not found")
    try:
        before = {"engineering_gate": (row.training_config or {}).get("engineering_gate"),
                  "scientific_gate": (row.training_config or {}).get("scientific_gate")}
        out = await compute_model_readiness(db, row, persist=True)
        await ml_audit(db, action="readiness_recomputed", actor_username=_actor(current_user),
                       actor_user_id=_actor_id(current_user), object_type="ml_model",
                       object_id=str(row.id), before=before,
                       after={"engineering_gate": out["engineering_gate"]["status"],
                              "scientific_gate": out["scientific_gate"]["status"]},
                       ip_address=(request.client.host if request.client else None))
        await db.commit()
        return jsonable(out)
    except HTTPException:
        raise
    except Exception as e:
        raise _safe_500("readiness recomputation", e)


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
             summary="Run Drift Check Now", status_code=202)
async def run_drift(
    db: AsyncSession = Depends(get_db),
    current_user=Depends(ML_MANAGE),
    _csrf: None = Depends(require_mlops_csrf),
    _rl: None = Depends(rate_limited("ml_ops", heavy=True)),
):
    """Enqueue report-only drift computation for the independent worker."""
    try:
        from backend.ml.job_service import enqueue_ml_job, MLJobConflict
        try:
            outcome = await enqueue_ml_job(
                db, kind="drift", payload={"source": "manual"},
                description="Manual report-only ML drift check",
                created_by_user_id=_actor_id(current_user),
                request_id=_current_request_id(),
            )
        except MLJobConflict as conflict:
            raise _error(409, "JOB_ALREADY_RUNNING", "a drift check is already active",
                         job_id=conflict.existing.get("job_id"))
        await ml_audit(db, action="drift_run_requested", actor_username=_actor(current_user),
                       actor_user_id=_actor_id(current_user), object_type="ml_drift_run",
                       object_id=outcome["job_id"])
        await db.commit()
        return JSONResponse(status_code=202, content=outcome)
    except HTTPException:
        raise
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

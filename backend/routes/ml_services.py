"""Small service-first facade over the governed ML registry."""
import uuid
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from db_connection import get_db
from backend.core.rate_limiter import rate_limited
from backend.ml.model_specs import MODEL_SPECS
from backend.ml.registry_service import RegistryError
from backend.ml.service_deployment import deploy_service, service_status, stop_service
from backend.routes.ml_ops import (LoggedMLRoute, ML_MANAGE, _actor, _actor_id,
                                   _error, _safe_500, require_mlops_csrf)

router = APIRouter(route_class=LoggedMLRoute)


class DeploymentRequest(BaseModel):
    model_id: uuid.UUID
    artifact_hash: str = Field(..., min_length=64, max_length=64, pattern=r"^[a-fA-F0-9]{64}$")
    reason: str = Field(..., min_length=3, max_length=1000)


class StopRequest(BaseModel):
    model_id: uuid.UUID
    reason: str = Field(..., min_length=3, max_length=1000)


def _service(model_type):
    if model_type not in MODEL_SPECS:
        raise _error(404, "SERVICE_NOT_FOUND", "Unknown ML service.")


@router.get("/api/ml/services/status", tags=["ML Operations"])
async def get_services_status(db: AsyncSession = Depends(get_db), current_user=Depends(ML_MANAGE)):
    try:
        return JSONResponse(await service_status(db), headers={"Cache-Control": "no-store"})
    except Exception as exc:
        raise _safe_500("service status", exc)


@router.post("/api/ml/services/{model_type}/deploy", tags=["ML Operations"])
async def deploy(model_type: str, body: DeploymentRequest,
                 db: AsyncSession = Depends(get_db), current_user=Depends(ML_MANAGE),
                 _csrf=Depends(require_mlops_csrf), _rl=Depends(rate_limited("ml_ops", heavy=True))):
    _service(model_type)
    try:
        return await deploy_service(db, model_type, model_id=str(body.model_id),
            artifact_hash=body.artifact_hash, reason=body.reason,
            actor=_actor(current_user), actor_id=_actor_id(current_user))
    except RegistryError as exc:
        await db.rollback()
        raise _error(409, exc.code, exc.message)
    except Exception as exc:
        await db.rollback()
        raise _safe_500("service deployment", exc)


@router.post("/api/ml/services/{model_type}/rollback", tags=["ML Operations"],
             summary="Stop using a selected service model")
async def stop(model_type: str, body: StopRequest,
               db: AsyncSession = Depends(get_db), current_user=Depends(ML_MANAGE),
               _csrf=Depends(require_mlops_csrf), _rl=Depends(rate_limited("ml_ops"))):
    _service(model_type)
    try:
        return await stop_service(db, model_type, model_id=str(body.model_id),
            reason=body.reason, actor=_actor(current_user), actor_id=_actor_id(current_user))
    except RegistryError as exc:
        await db.rollback()
        raise _error(409, exc.code, exc.message)
    except Exception as exc:
        await db.rollback()
        raise _safe_500("stop service model", exc)

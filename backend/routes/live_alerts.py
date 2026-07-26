"""
Live Alerts API Routes
=======================
Manage live search alerts - get notified when a searched face appears again.

Security model (applied consistently):
  * every route enforces authorization server-side (frontend role checks are
    UI-shaping only)
  * ownership: only the alert creator or an admin can view/modify an alert,
    its triggers, or acknowledgements
  * CSRF: cookie auth is SameSite=lax; mutating routes additionally require
    the custom `X-Requested-With: XMLHttpRequest` header (cross-site pages
    cannot set custom headers without a CORS preflight this API never grants)
  * every lifecycle action writes a live_alert_audit_log row — counts/ids
    only, never tokens/embeddings/sensitive payloads
"""

import asyncio
import logging
import os
import uuid as uuid_mod
from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, HTTPException, Depends, Query, Request, Response, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from sqlalchemy.orm import selectinload

from db_connection import get_db, db_manager
from db_models import User, LiveSearchAlert, LiveAlertAuditLog, LiveAlertStatus, LiveAlertExpirationType
from backend.auth.auth_service import get_current_user, require_admin, require_unknown_faces_access
from backend.core.live_alert_service import live_alert_service

logger = logging.getLogger(__name__)
router = APIRouter()

NO_STORE = "no-store, no-cache, must-revalidate"


# =====================================================
# Security helpers
# =====================================================

def require_csrf_header(request: Request):
    """CSRF defense-in-depth for cookie-authenticated mutating requests.

    SameSite=lax on the auth cookie blocks cross-site POSTs in modern
    browsers; requiring this custom header blocks the remaining vectors
    (a cross-site page cannot attach custom headers without CORS preflight).
    Bearer-token clients (curl/tests) are exempt — the token itself cannot
    be sent cross-site.
    """
    if request.headers.get("authorization"):
        return  # explicit bearer token — not a CSRF vector
    xrw = request.headers.get("x-requested-with", "")
    if xrw.lower() != "xmlhttprequest":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="CSRF check failed: X-Requested-With header required",
        )


def _parse_alert_uuid(alert_id: str) -> uuid_mod.UUID:
    try:
        return uuid_mod.UUID(alert_id)
    except (ValueError, AttributeError):
        raise HTTPException(status_code=404, detail="Alert not found")


async def _get_owned_alert(db: AsyncSession, alert_id: str, current_user: dict) -> LiveSearchAlert:
    """404 if missing, 403 unless owner or admin. Central authorization gate."""
    _parse_alert_uuid(alert_id)
    alert = await live_alert_service.get_alert(db, alert_id)
    if not alert:
        raise HTTPException(status_code=404, detail="Alert not found")
    if alert.created_by != current_user['id'] and current_user.get('role') != 'admin':
        raise HTTPException(status_code=403, detail="Not authorized")
    return alert


async def _audit(request: Request, current_user: dict, alert_id: Optional[str],
                 action: str, details: Optional[dict] = None, result: str = "success"):
    """Best-effort audit write (own session — never blocks the main flow)."""
    try:
        async with db_manager.get_session() as audit_db:
            audit_db.add(LiveAlertAuditLog(
                user_id=current_user.get('id'),
                username=current_user.get('username'),
                alert_id=uuid_mod.UUID(alert_id) if alert_id else None,
                action=action,
                details=details,
                result=result,
                request_id=getattr(getattr(request, "state", None), "request_id", None),
                ip_address=request.headers.get("x-real-ip") or (request.client.host if request.client else None),
            ))
            await audit_db.commit()
        logger.info(
            "[ALERT] audit action=%s alert_id=%s user_id=%s result=%s request_id=%s",
            action, alert_id, current_user.get('id'), result,
            getattr(getattr(request, "state", None), "request_id", None))
    except Exception as e:
        logger.warning(f"[ALERT] audit write failed for {action}: {e}")


# =====================================================
# Request/Response Models
# =====================================================

class CreateLiveAlertRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=200, description="Alert name")
    identity_id: str = Field(..., description="UUID of the identity to track")
    min_similarity: float = Field(default=0.75, ge=0, le=1, description="Minimum similarity to trigger")
    pipeline_ids: Optional[List[str]] = Field(None, description="Specific cameras (null = all)")
    time_window_enabled: bool = Field(default=False, description="Enable time window filter")
    time_window_start: Optional[str] = Field(None, description="Start time HH:MM")
    time_window_end: Optional[str] = Field(None, description="End time HH:MM")
    active_days: Optional[List[int]] = Field(None, description="Active days (0=Sun, 6=Sat)")
    cooldown_minutes: Optional[int] = Field(None, ge=0, description="Minutes between alerts")
    notify_dashboard: bool = Field(default=True)
    notify_email: bool = Field(default=False)
    notify_sms: bool = Field(default=False)
    notify_webhook: bool = Field(default=False)
    email_recipients: Optional[List[str]] = Field(None)
    sms_recipients: Optional[List[str]] = Field(None)
    webhook_url: Optional[str] = Field(None)
    sound_alert: bool = Field(default=True)
    auto_capture_snapshot: bool = Field(default=True)
    auto_record_clip: bool = Field(default=False)
    clip_duration_seconds: int = Field(default=60, ge=10, le=300)
    expiration_type: str = Field(default="never", description="never, date, or detections")
    expiration_date: Optional[str] = Field(None, description="Expiration date (ISO format)")
    expiration_detections: Optional[int] = Field(None, ge=1, description="Expire after N detections")


class UpdateLiveAlertRequest(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=200)
    min_similarity: Optional[float] = Field(None, ge=0, le=1)
    pipeline_ids: Optional[List[str]] = None
    time_window_enabled: Optional[bool] = None
    time_window_start: Optional[str] = None
    time_window_end: Optional[str] = None
    active_days: Optional[List[int]] = None
    cooldown_minutes: Optional[int] = Field(None, ge=0)
    notify_dashboard: Optional[bool] = None
    notify_email: Optional[bool] = None
    notify_sms: Optional[bool] = None
    notify_webhook: Optional[bool] = None
    email_recipients: Optional[List[str]] = None
    sms_recipients: Optional[List[str]] = None
    webhook_url: Optional[str] = None
    sound_alert: Optional[bool] = None


class LiveAlertResponse(BaseModel):
    id: str
    name: str
    identity_id: str
    identity_name: Optional[str]
    identity_snapshot_path: Optional[str]  # Backend provides identity's snapshot path
    status: str
    min_similarity: float
    pipeline_ids: Optional[List[str]]
    time_window_enabled: bool
    time_window_start: Optional[str]
    time_window_end: Optional[str]
    active_days: Optional[List[int]]
    cooldown_minutes: int
    notify_dashboard: bool
    notify_email: bool
    notify_sms: bool
    notify_webhook: bool
    email_recipients: Optional[List[str]]
    sms_recipients: Optional[List[str]]
    webhook_url: Optional[str]
    sound_alert: bool
    expiration_type: str
    expiration_date: Optional[str]
    expiration_detections: Optional[int]
    triggers_count: int
    last_triggered_at: Optional[str]
    created_at: str
    updated_at: Optional[str]


class LiveAlertTriggerResponse(BaseModel):
    id: str
    alert_id: str
    pipeline_id: Optional[str]
    similarity_score: Optional[float]
    snapshot_path: Optional[str]
    acknowledged: bool
    acknowledged_by: Optional[str]
    acknowledged_at: Optional[str]
    created_at: str


class TriggersPageResponse(BaseModel):
    items: List[LiveAlertTriggerResponse]
    total: int
    page: int
    page_size: int
    total_pages: int
    unacknowledged_total: int


class BulkAcknowledgeRequest(BaseModel):
    trigger_ids: Optional[List[str]] = Field(
        None, max_length=500,
        description="Specific trigger ids to acknowledge; omit for ALL unacknowledged")


class ChannelTestRequest(BaseModel):
    channels: Optional[List[str]] = Field(
        None, description="Subset of dashboard|email|sms|sound (default: all)")
    confirm_real_send: bool = Field(
        default=False,
        description="Must be true before any real email/SMS would be sent")


# =====================================================
# API Endpoints
# =====================================================

@router.get(
    "/api/live-alerts",
    response_model=List[LiveAlertResponse],
    summary="List Live Alerts",
    description="Get all live alerts for the current user."
)
async def list_live_alerts(
    response: Response,
    include_inactive: bool = Query(default=False, description="Include expired/triggered alerts"),
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_unknown_faces_access())
):
    """
    Get all live alerts for current user.

    **Pipeline Access Control:**
    - Regular users: Only see alerts for identities from their assigned pipelines
    - Admins with NO pipeline restrictions: See all alerts (full admin access)
    - Admins WITH pipeline restrictions: Only see alerts for identities from their assigned pipelines
    """
    from backend.auth.auth_service import AuthService

    response.headers["Cache-Control"] = NO_STORE

    user_pipelines_list = await AuthService.get_user_pipelines(current_user['id'], db)

    user_pipelines = None  # None = no filtering (see all alerts)

    if not user_pipelines_list:
        if current_user['role'] != 'admin':
            logger.warning(f"[LIVE_ALERTS] Regular user {current_user['id']} has no pipeline access, returning empty alerts list")
            return []
        else:
            user_pipelines = None
    else:
        user_pipelines = user_pipelines_list

    alerts = await live_alert_service.get_user_alerts(
        db, current_user['id'], include_inactive, user_pipelines
    )

    logger.debug(f"[LIVE_ALERTS] Returning {len(alerts)} alerts for user {current_user['id']}")
    return [_format_alert(alert) for alert in alerts]


@router.post(
    "/api/live-alerts",
    response_model=LiveAlertResponse,
    summary="Create Live Alert",
    dependencies=[Depends(require_csrf_header)],
)
async def create_live_alert(
    request: CreateLiveAlertRequest,
    http_request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_unknown_faces_access())
):
    """Create a new live alert (identity access enforced server-side)."""
    from backend.auth.auth_service import check_identity_access

    user_result = await db.execute(select(User).where(User.id == current_user['id']))
    user_obj = user_result.scalar_one_or_none()

    if not user_obj:
        raise HTTPException(status_code=404, detail="User not found")

    has_access = await check_identity_access(request.identity_id, user_obj, db)
    if not has_access:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access denied. You don't have access to this identity."
        )

    expiration_date = None
    if request.expiration_date:
        try:
            expiration_date = datetime.fromisoformat(
                request.expiration_date.replace('Z', '+00:00')
            )
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid expiration_date format")

    try:
        alert = await live_alert_service.create_alert(
            db=db,
            name=request.name,
            identity_id=request.identity_id,
            created_by=current_user['id'],
            min_similarity=request.min_similarity,
            pipeline_ids=request.pipeline_ids,
            time_window_enabled=request.time_window_enabled,
            time_window_start=request.time_window_start,
            time_window_end=request.time_window_end,
            active_days=request.active_days,
            cooldown_minutes=request.cooldown_minutes,
            notify_dashboard=request.notify_dashboard,
            notify_email=request.notify_email,
            notify_sms=request.notify_sms,
            notify_webhook=request.notify_webhook,
            email_recipients=request.email_recipients,
            sms_recipients=request.sms_recipients,
            webhook_url=request.webhook_url,
            sound_alert=request.sound_alert,
            auto_capture_snapshot=request.auto_capture_snapshot,
            auto_record_clip=request.auto_record_clip,
            clip_duration_seconds=request.clip_duration_seconds,
            expiration_type=request.expiration_type,
            expiration_date=expiration_date,
            expiration_detections=request.expiration_detections
        )

        reload_query = select(LiveSearchAlert).options(
            selectinload(LiveSearchAlert.identity)
        ).where(LiveSearchAlert.id == alert.id)
        reload_result = await db.execute(reload_query)
        alert_with_identity = reload_result.scalar_one()

        await _audit(http_request, current_user, str(alert.id), "alert_created",
                     {"name": request.name, "identity_id": request.identity_id,
                      "min_similarity": request.min_similarity})

        return _format_alert(alert_with_identity)

    except ValueError as e:
        logger.error(f"[LIVE_ALERT] Validation error creating alert: {e}")
        raise HTTPException(status_code=400, detail=str(e))
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[LIVE_ALERT] Error creating alert: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to create alert")


@router.get(
    "/api/live-alerts/defaults/{identity_id}",
    summary="Get Default Alert Settings",
)
async def get_default_alert_settings(
    identity_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_unknown_faces_access())
):
    """Get default settings for creating a live alert (access-checked)."""
    from backend.auth.auth_service import check_identity_access

    try:
        user_result = await db.execute(select(User).where(User.id == current_user['id']))
        user_obj = user_result.scalar_one_or_none()

        if not user_obj:
            raise HTTPException(status_code=404, detail="User not found")

        has_access = await check_identity_access(identity_id, user_obj, db)
        if not has_access:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Access denied. You don't have access to this identity."
            )

        settings_data = await live_alert_service.get_default_alert_settings(
            db=db,
            identity_id=identity_id,
            created_by=current_user['id']
        )
        return settings_data
    except HTTPException:
        raise
    except ValueError as e:
        logger.warning(f"Identity not found or validation error for {identity_id}: {e}")
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        logger.error(f"Error getting default alert settings: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to get default settings")


@router.get(
    "/api/live-alerts/test-jobs/{job_id}",
    summary="Get Channel-Test Job Status",
)
async def get_channel_test_job(
    job_id: str,
    response: Response,
    current_user: dict = Depends(require_admin())
):
    """Poll an asynchronous notification-channel test job (admin only)."""
    response.headers["Cache-Control"] = NO_STORE
    from backend.core.task_history import task_history_manager
    task = await task_history_manager.get_task_by_job_id(job_id)
    if not task or task.get("task_type") != "alert_channel_test":
        raise HTTPException(status_code=404, detail="Test job not found")
    return task


@router.get(
    "/api/live-alerts/{alert_id}",
    response_model=LiveAlertResponse,
    summary="Get Live Alert",
)
async def get_live_alert(
    alert_id: str,
    response: Response,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_unknown_faces_access())
):
    """Get a specific live alert (owner or admin only)."""
    response.headers["Cache-Control"] = NO_STORE
    alert = await _get_owned_alert(db, alert_id, current_user)
    return _format_alert(alert)


@router.put(
    "/api/live-alerts/{alert_id}",
    response_model=LiveAlertResponse,
    summary="Update Live Alert",
    dependencies=[Depends(require_csrf_header)],
)
async def update_live_alert(
    alert_id: str,
    request: UpdateLiveAlertRequest,
    http_request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_unknown_faces_access())
):
    """Update a live alert (owner or admin only)."""
    existing = await _get_owned_alert(db, alert_id, current_user)

    updates = {k: v for k, v in request.model_dump().items() if v is not None}
    if not updates:
        raise HTTPException(status_code=400, detail="No updates provided")

    # Safe before-values (config fields only — no recipients' contents)
    before = {k: getattr(existing, k, None) for k in updates
              if k not in ("email_recipients", "sms_recipients")}

    alert = await live_alert_service.update_alert(db, alert_id, **updates)
    await _audit(http_request, current_user, alert_id, "alert_updated",
                 {"changed_fields": sorted(updates.keys()), "before": {k: str(v) for k, v in before.items()}})
    return _format_alert(alert)


@router.delete(
    "/api/live-alerts/{alert_id}",
    summary="Delete Live Alert",
    dependencies=[Depends(require_csrf_header)],
)
async def delete_live_alert(
    alert_id: str,
    http_request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_unknown_faces_access())
):
    """Delete a live alert (owner or admin only)."""
    existing = await _get_owned_alert(db, alert_id, current_user)
    alert_name = existing.name

    success = await live_alert_service.delete_alert(db, alert_id)
    if not success:
        raise HTTPException(status_code=404, detail="Alert not found")

    await _audit(http_request, current_user, alert_id, "alert_deleted", {"name": alert_name})
    return {"success": True}


@router.post(
    "/api/live-alerts/{alert_id}/pause",
    summary="Pause Live Alert",
    dependencies=[Depends(require_csrf_header)],
)
async def pause_live_alert(
    alert_id: str,
    http_request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_unknown_faces_access())
):
    """Pause a live alert (owner or admin only)."""
    await _get_owned_alert(db, alert_id, current_user)
    success = await live_alert_service.pause_alert(db, alert_id)
    if not success:
        raise HTTPException(status_code=404, detail="Alert not found")

    await _audit(http_request, current_user, alert_id, "alert_paused")
    return {"success": True, "status": "paused"}


@router.post(
    "/api/live-alerts/{alert_id}/resume",
    summary="Resume Live Alert",
    dependencies=[Depends(require_csrf_header)],
)
async def resume_live_alert(
    alert_id: str,
    http_request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_unknown_faces_access())
):
    """Resume a paused alert (owner or admin only)."""
    await _get_owned_alert(db, alert_id, current_user)
    success = await live_alert_service.resume_alert(db, alert_id)
    if not success:
        raise HTTPException(status_code=404, detail="Alert not found")

    await _audit(http_request, current_user, alert_id, "alert_resumed")
    return {"success": True, "status": "active"}


@router.get(
    "/api/live-alerts/{alert_id}/triggers",
    response_model=TriggersPageResponse,
    summary="Get Alert Triggers (paginated)",
)
async def get_alert_triggers(
    alert_id: str,
    response: Response,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    acknowledged: Optional[bool] = Query(default=None),
    date_from: Optional[str] = Query(default=None, description="ISO datetime lower bound"),
    date_to: Optional[str] = Query(default=None, description="ISO datetime upper bound"),
    pipeline_id: Optional[str] = Query(default=None, max_length=255),
    min_similarity: Optional[float] = Query(default=None, ge=0, le=1),
    sort_order: str = Query(default="desc", pattern="^(asc|desc)$"),
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_unknown_faces_access())
):
    """Server-side paginated + filtered trigger history (owner or admin only)."""
    response.headers["Cache-Control"] = NO_STORE
    await _get_owned_alert(db, alert_id, current_user)

    def _parse_dt(value, name):
        if not value:
            return None
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).replace(tzinfo=None)
        except ValueError:
            raise HTTPException(status_code=422, detail=f"Invalid {name}")

    page_data = await live_alert_service.get_alert_triggers_page(
        db, alert_id,
        page=page, page_size=page_size,
        acknowledged=acknowledged,
        date_from=_parse_dt(date_from, "date_from"),
        date_to=_parse_dt(date_to, "date_to"),
        pipeline_id=pipeline_id,
        min_similarity=min_similarity,
        sort_order=sort_order,
    )

    page_data["items"] = [
        {
            "id": str(t.id),
            "alert_id": str(t.alert_id),
            "pipeline_id": t.pipeline_id,
            "similarity_score": t.similarity_score,
            "snapshot_path": t.snapshot_path,
            "acknowledged": t.acknowledged,
            "acknowledged_by": t.acknowledged_by_user.username if t.acknowledged_by_user else None,
            "acknowledged_at": t.acknowledged_at.isoformat() if t.acknowledged_at else None,
            "created_at": t.created_at.isoformat()
        }
        for t in page_data["items"]
    ]
    return page_data


@router.post(
    "/api/live-alerts/{alert_id}/triggers/acknowledge-all",
    summary="Bulk Acknowledge Triggers",
    dependencies=[Depends(require_csrf_header)],
)
async def acknowledge_all_triggers(
    alert_id: str,
    http_request: Request,
    body: Optional[BulkAcknowledgeRequest] = None,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_unknown_faces_access())
):
    """Acknowledge all (or the listed) unacknowledged triggers in ONE
    statement — replaces the old one-request-per-trigger pattern."""
    await _get_owned_alert(db, alert_id, current_user)
    trigger_ids = body.trigger_ids if body else None

    result = await live_alert_service.bulk_acknowledge(
        db, alert_id, current_user['id'], trigger_ids=trigger_ids
    )
    await _audit(http_request, current_user, alert_id, "bulk_acknowledged",
                 {"acknowledged": result["acknowledged"], "failed": result["failed"],
                  "scoped_to_ids": trigger_ids is not None})
    return {"success": True, **result}


@router.post(
    "/api/live-alerts/triggers/{trigger_id}/acknowledge",
    summary="Acknowledge Trigger",
    dependencies=[Depends(require_csrf_header)],
)
async def acknowledge_trigger(
    trigger_id: str,
    http_request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_unknown_faces_access())
):
    """Acknowledge a trigger (owner of the parent alert or admin only)."""
    trigger = await live_alert_service.get_trigger_with_alert(db, trigger_id)
    if not trigger:
        raise HTTPException(status_code=404, detail="Trigger not found")
    if trigger.alert and trigger.alert.created_by != current_user['id'] \
            and current_user.get('role') != 'admin':
        raise HTTPException(status_code=403, detail="Not authorized")

    success = await live_alert_service.acknowledge_trigger(
        db, trigger_id, current_user['id']
    )
    if not success:
        raise HTTPException(status_code=404, detail="Trigger not found")

    await _audit(http_request, current_user,
                 str(trigger.alert_id) if trigger.alert_id else None,
                 "trigger_acknowledged", {"trigger_id": trigger_id})
    return {"success": True}


# =====================================================
# Alert health + channel testing
# =====================================================

def _channel_config_status() -> dict:
    """What notification infrastructure is actually configured?"""
    from config import settings as cfg
    smtp_ready = bool(getattr(cfg, 'SMTP_HOST', None))
    sms_ready = bool(getattr(cfg, 'SMS_PROVIDER_URL', None) or getattr(cfg, 'TWILIO_ACCOUNT_SID', None))
    return {"email": smtp_ready, "sms": sms_ready}


@router.get(
    "/api/live-alerts/{alert_id}/health",
    summary="Alert Health",
    description="Whether the alert is actually effective: stored vs effective status, identity/snapshot/pipeline validity, per-channel readiness."
)
async def get_alert_health(
    alert_id: str,
    response: Response,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_unknown_faces_access())
):
    response.headers["Cache-Control"] = NO_STORE
    alert = await _get_owned_alert(db, alert_id, current_user)

    now = datetime.utcnow()
    stored_status = alert.status.value

    # Effective status: stored status corrected for expiry conditions
    effective_status = stored_status
    if stored_status == "active":
        if (alert.expiration_type == LiveAlertExpirationType.DATE
                and alert.expiration_date and now >= alert.expiration_date):
            effective_status = "expired"
        elif (alert.expiration_type == LiveAlertExpirationType.DETECTIONS
                and alert.expiration_detections
                and alert.triggers_count >= alert.expiration_detections):
            effective_status = "expired"

    identity_exists = alert.identity is not None
    if not identity_exists and effective_status == "active":
        effective_status = "disabled"  # cannot trigger without its identity

    snapshot_exists = False
    if alert.identity and alert.identity.best_snapshot_path:
        try:
            p = alert.identity.best_snapshot_path
            snapshot_exists = os.path.exists(p) if os.path.isabs(p) else os.path.exists(os.path.join("/app", p))
        except Exception:
            snapshot_exists = False

    pipelines_valid = True
    invalid_pipelines: List[str] = []
    if alert.pipeline_ids:
        from db_models import Pipeline
        rows = (await db.execute(
            select(Pipeline.pipeline_id).where(Pipeline.pipeline_id.in_(alert.pipeline_ids))
        )).scalars().all()
        invalid_pipelines = sorted(set(alert.pipeline_ids) - set(rows))
        pipelines_valid = not invalid_pipelines

    from backend.core.websocket_manager import ws_manager
    ws_clients = len(getattr(ws_manager, 'active_connections', []) or [])
    channels = _channel_config_status()

    return {
        "alert_id": str(alert.id),
        "stored_status": stored_status,
        "effective_status": effective_status,
        "identity_exists": identity_exists,
        "snapshot_exists": snapshot_exists,
        "pipelines_valid": pipelines_valid,
        "invalid_pipelines": invalid_pipelines,
        "dashboard_channel_ready": alert.notify_dashboard and ws_clients > 0,
        "email_channel_ready": alert.notify_email and channels["email"],
        "sms_channel_ready": alert.notify_sms and channels["sms"],
        "websocket_ready": ws_clients > 0,
        "websocket_clients": ws_clients,
        "last_evaluated_at": now.isoformat() + "Z",
        "last_delivery_error": None,
    }


async def _run_channel_test(job_id: str, alert_id: str, alert_name: str,
                            alert_flags: dict, channels: List[str],
                            confirm_real_send: bool):
    """Async channel test — never sends real email/SMS unless explicitly
    confirmed AND the provider is configured (currently none are)."""
    from backend.core.task_history import task_history_manager
    await task_history_manager.mark_running(job_id)
    results = {}
    try:
        configured = _channel_config_status()

        if "dashboard" in channels:
            try:
                from backend.core.websocket_manager import ws_manager
                clients = len(getattr(ws_manager, 'active_connections', []) or [])
                if clients > 0:
                    await ws_manager.broadcast({
                        "type": "live_alert_test",
                        "data": {
                            "event_id": uuid_mod.uuid4().hex,
                            "alert_id": alert_id,
                            "alert_name": alert_name,
                            "is_test": True,
                            "created_at": datetime.utcnow().isoformat() + "Z",
                        }
                    })
                    results["dashboard"] = {"status": "sent", "clients": clients}
                else:
                    results["dashboard"] = {"status": "failed", "error_code": "NO_ACTIVE_LISTENERS"}
            except Exception as e:
                results["dashboard"] = {"status": "failed", "error_code": "BROADCAST_ERROR"}
                logger.error(f"[ALERT] channel test dashboard failed: {e}")

        if "email" in channels:
            if not alert_flags.get("notify_email"):
                results["email"] = {"status": "disabled"}
            elif not configured["email"]:
                results["email"] = {"status": "failed", "error_code": "SMTP_NOT_CONFIGURED"}
            elif not confirm_real_send:
                results["email"] = {"status": "skipped", "error_code": "CONFIRMATION_REQUIRED"}
            else:
                results["email"] = {"status": "failed", "error_code": "SMTP_SEND_NOT_IMPLEMENTED"}

        if "sms" in channels:
            if not alert_flags.get("notify_sms"):
                results["sms"] = {"status": "disabled"}
            elif not configured["sms"]:
                results["sms"] = {"status": "failed", "error_code": "SMS_PROVIDER_NOT_CONFIGURED"}
            elif not confirm_real_send:
                results["sms"] = {"status": "skipped", "error_code": "CONFIRMATION_REQUIRED"}
            else:
                results["sms"] = {"status": "failed", "error_code": "SMS_SEND_NOT_IMPLEMENTED"}

        if "sound" in channels:
            # Sound is produced by the browser; the backend can only deliver
            # the dashboard event that carries the sound flag.
            results["sound"] = {"status": "frontend_required",
                                "note": "played by the browser when the dashboard test event arrives"}

        for ch, res in results.items():
            logger.info("[ALERT] channel_test job_id=%s alert_id=%s channel=%s status=%s error_code=%s",
                        job_id, alert_id, ch, res.get("status"), res.get("error_code"))

        await task_history_manager.finish_job(job_id, success=True, result={"channels": results})
    except Exception as e:
        logger.error(f"[ALERT] channel test job {job_id} failed: {e}", exc_info=True)
        await task_history_manager.finish_job(
            job_id, success=False, result={"channels": results},
            error_code="TEST_JOB_FAILED", error_message=str(e))


@router.post(
    "/api/live-alerts/{alert_id}/test",
    summary="Test Notification Channels",
    dependencies=[Depends(require_csrf_header)],
)
async def test_alert_channels(
    alert_id: str,
    http_request: Request,
    body: Optional[ChannelTestRequest] = None,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_admin())
):
    """Admin only: asynchronously test the alert's notification channels.
    Returns 202 + job_id; poll GET /api/live-alerts/test-jobs/{job_id}.
    No real email/SMS is sent without confirm_real_send AND a configured
    provider — results are honest per-channel statuses."""
    alert = await _get_owned_alert(db, alert_id, current_user)

    valid_channels = ["dashboard", "email", "sms", "sound"]
    channels = body.channels if body and body.channels else valid_channels
    channels = [c for c in channels if c in valid_channels]
    if not channels:
        raise HTTPException(status_code=422, detail=f"channels must be a subset of {valid_channels}")

    from backend.core.task_history import task_history_manager
    job_id = f"alerttest-{uuid_mod.uuid4().hex[:8]}"
    task_id = await task_history_manager.create_job(
        job_id=job_id, task_type="alert_channel_test",
        task_name=f"Channel test: {alert.name[:80]}",
        description=f"Test channels {channels} (clearly labeled test — no unconfirmed real sends)",
        created_by_user_id=current_user['id'],
        request_id=getattr(getattr(http_request, "state", None), "request_id", None),
    )
    if task_id < 0:
        raise HTTPException(status_code=500, detail="Failed to create test job")

    alert_flags = {"notify_email": alert.notify_email, "notify_sms": alert.notify_sms}
    asyncio.create_task(_run_channel_test(
        job_id, str(alert.id), alert.name, alert_flags, channels,
        bool(body.confirm_real_send) if body else False))

    await _audit(http_request, current_user, alert_id, "channel_test",
                 {"channels": channels, "job_id": job_id})

    return {"accepted": True, "job_id": job_id, "task_id": task_id, "status": "scheduled"}


def _format_alert(alert) -> dict:
    """Format alert for response. Backend provides all data including snapshot path."""
    from config import settings

    # Get identity snapshot path (backend logic) - same logic as identities endpoint
    identity_snapshot_path = None
    if alert.identity:
        best_snapshot_path = alert.identity.best_snapshot_path
        storage_dir = getattr(settings, 'STORAGE_DIR', './storage')
        storage_dir_abs = os.path.abspath(storage_dir)

        if best_snapshot_path:
            # Convert absolute path to relative path for static file serving
            if os.path.isabs(best_snapshot_path):
                best_snapshot_path_abs = os.path.abspath(best_snapshot_path)
                if best_snapshot_path_abs.startswith(storage_dir_abs):
                    relative_path = os.path.relpath(best_snapshot_path_abs, storage_dir_abs)
                    best_snapshot_path = 'storage/' + relative_path.replace('\\', '/')
                elif not os.path.exists(best_snapshot_path):
                    best_snapshot_path = None
            else:
                if best_snapshot_path.startswith('known_faces/'):
                    best_snapshot_path = 'storage/' + best_snapshot_path
                elif not best_snapshot_path.startswith('storage/'):
                    best_snapshot_path = 'storage/' + best_snapshot_path.lstrip('/')

            if best_snapshot_path:
                if best_snapshot_path.startswith('storage/'):
                    identity_snapshot_path = f"/{best_snapshot_path}"
                elif not best_snapshot_path.startswith('/'):
                    identity_snapshot_path = f"/storage/{best_snapshot_path}"
                else:
                    identity_snapshot_path = best_snapshot_path

    return {
        "id": str(alert.id),
        "name": alert.name,
        "identity_id": str(alert.identity_id),
        "identity_name": alert.identity.display_name if alert.identity else None,
        "identity_snapshot_path": identity_snapshot_path,  # None => frontend uses its safe fallback
        "status": alert.status.value,
        "min_similarity": alert.min_similarity,
        "pipeline_ids": alert.pipeline_ids,
        "time_window_enabled": alert.time_window_enabled,
        "time_window_start": alert.time_window_start,
        "time_window_end": alert.time_window_end,
        "active_days": alert.active_days,
        "cooldown_minutes": alert.cooldown_minutes,
        "notify_dashboard": alert.notify_dashboard,
        "notify_email": alert.notify_email,
        "notify_sms": alert.notify_sms,
        "notify_webhook": alert.notify_webhook,
        "email_recipients": alert.email_recipients,
        "sms_recipients": alert.sms_recipients,
        "webhook_url": alert.webhook_url,
        "sound_alert": alert.sound_alert,
        "expiration_type": alert.expiration_type.value,
        "expiration_date": alert.expiration_date.isoformat() if alert.expiration_date else None,
        "expiration_detections": alert.expiration_detections,
        "triggers_count": alert.triggers_count,
        "last_triggered_at": alert.last_triggered_at.isoformat() if alert.last_triggered_at else None,
        "created_at": alert.created_at.isoformat(),
        "updated_at": alert.updated_at.isoformat() if alert.updated_at else None
    }

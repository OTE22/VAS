"""
Watchlist API Routes (hardened)
================================
Manage watchlists (VIP, Threat, POI, etc.) and their entries.

Contract:
  * every mutation is CSRF-protected (cookie clients must send
    X-Requested-With; bearer clients are exempt) and audited via
    [WATCHLIST_AUDIT] structured log lines
  * malformed/unknown ids -> 404 (no SQL errors, no str(e) leakage)
  * name uniqueness is case-insensitive among live watchlists -> 409
  * updates carry an integer version -> 409 VERSION_CONFLICT on concurrent
    edits instead of silent lost updates
  * DELETE is a SOFT delete (matching stops, history preserved);
    hard delete requires explicit ?hard_delete=true&confirm=true
  * list endpoint has a paginated envelope mode with REAL per-watchlist
    statistics (entries, alerts today, total alerts, last alert) computed
    in batched queries — never one query per watchlist
  * watchlist changes publish idempotent WebSocket events (event_id)
"""

import logging
import re
import time
import uuid
from datetime import datetime, timedelta
from typing import List, Optional

from fastapi import APIRouter, HTTPException, Depends, Query, Request, status as http_status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.ext.asyncio import AsyncSession

from db_connection import get_db
from backend.auth.auth_service import get_current_user, require_admin
from backend.core.watchlist_service import (
    watchlist_service, WatchlistStats,
    WatchlistNameConflict, WatchlistVersionConflict,
)
from db_models import WatchlistAlertLevel, WatchlistEntryPriority

logger = logging.getLogger(__name__)
router = APIRouter()


# =====================================================
# Validation allowlists + helpers
# =====================================================

WATCHLIST_ICON_ALLOWLIST = frozenset({
    "list", "shield-alt", "user-shield", "exclamation-triangle",
    "eye", "users", "star", "ban", "user-secret", "crosshairs",
})
COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
ALERT_LEVELS = frozenset(level.value for level in WatchlistAlertLevel)
ENTRY_PRIORITIES = frozenset(p.value for p in WatchlistEntryPriority)
REPORTING_TIMEZONE = "UTC"  # explicit reporting timezone for "today" stats


def _reference_id() -> str:
    return f"WL-{uuid.uuid4().hex[:8]}"


def _iso_z(dt) -> Optional[str]:
    if dt is None:
        return None
    s = dt.isoformat()
    return s + "Z" if dt.tzinfo is None else s.replace("+00:00", "Z")


def _safe_500(action: str, exc: Exception) -> HTTPException:
    ref = _reference_id()
    logger.error("[WATCHLIST] action=%s status=error reference_id=%s error=%s",
                 action, ref, exc, exc_info=True)
    return HTTPException(status_code=500,
                         detail=f"Internal error during {action}. Reference: {ref}")


def require_watchlist_csrf(request: Request):
    """CSRF defense for cookie-authenticated mutating requests."""
    if request.headers.get("authorization"):
        return
    if request.headers.get("x-requested-with", "").lower() != "xmlhttprequest":
        raise HTTPException(
            status_code=http_status.HTTP_403_FORBIDDEN,
            detail="CSRF check failed: X-Requested-With header required",
        )


def _audit(action: str, current_user: dict, watchlist_id=None,
           result: str = "success", **fields):
    """Structured audit line — ids/counts only, never biometric payloads."""
    extra = " ".join(f"{k}={v}" for k, v in fields.items() if v is not None)
    logger.info("[WATCHLIST_AUDIT] action=%s user_id=%s watchlist_id=%s result=%s %s",
                action, (current_user or {}).get("id"), watchlist_id, result, extra)


async def _broadcast_change(action: str, watchlist_id: str, extra: Optional[dict] = None):
    """Idempotent WebSocket event so live consumers refresh matching config."""
    try:
        from backend.core import ws_manager
        payload = {
            "type": "watchlist_changed",
            "event_id": uuid.uuid4().hex,
            "action": action,
            "watchlist_id": str(watchlist_id),
            "timestamp": _iso_z(datetime.utcnow()),
        }
        if extra:
            payload.update(extra)
        await ws_manager.broadcast(payload)
    except Exception as e:
        logger.warning(f"[WATCHLIST] websocket broadcast failed (non-fatal): {e}")


async def _get_watchlist_or_404(db, watchlist_id: str, include_deleted: bool = False):
    watchlist = await watchlist_service.get_watchlist(db, watchlist_id, include_deleted)
    if not watchlist:
        raise HTTPException(status_code=404, detail="Watchlist not found")
    return watchlist


def _day_period():
    """Explicit reporting window for 'alerts today' (UTC calendar day)."""
    start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    return start, start + timedelta(days=1)


def _serialize(wl, stats: Optional[dict] = None) -> dict:
    data = {
        "id": str(wl.id),
        "name": wl.name,
        "description": wl.description,
        "color": wl.color,
        "icon": wl.icon,
        "alert_level": wl.alert_level.value,
        "notify_dashboard": wl.notify_dashboard,
        "notify_email": wl.notify_email,
        "notify_sms": wl.notify_sms,
        "notify_webhook": wl.notify_webhook,
        "email_recipients": wl.email_recipients,
        "sms_recipients": wl.sms_recipients,
        "webhook_url": wl.webhook_url,
        "is_active": wl.is_active,
        "version": wl.version or 1,
        "created_at": _iso_z(wl.created_at),
        "updated_at": _iso_z(wl.updated_at),
        "deleted_at": _iso_z(wl.deleted_at),
        "deletion_reason": wl.deletion_reason,
    }
    if stats is not None:
        data["entries_count"] = stats.get("entries_count", 0)
        data["alerts_today"] = stats.get("alerts_today", 0)
        data["total_alerts"] = stats.get("total_alerts", 0)
        data["last_alert_at"] = _iso_z(stats.get("last_alert_at"))
    return data


# =====================================================
# Request/Response Models
# =====================================================

def _validate_name(value: str) -> str:
    name = " ".join(str(value or "").split())  # normalize whitespace
    if len(name) < 2 or len(name) > 100:
        raise ValueError("name must be 2-100 characters")
    return name


class CreateWatchlistRequest(BaseModel):
    name: str = Field(..., description="Unique (case-insensitive) name")
    description: Optional[str] = Field(None, max_length=1000)
    color: str = Field(default="#6366f1")
    icon: str = Field(default="list")
    alert_level: str = Field(default="info")
    notify_dashboard: bool = Field(default=True)
    notify_email: bool = Field(default=False)
    notify_sms: bool = Field(default=False)
    notify_webhook: bool = Field(default=False)
    email_recipients: Optional[List[str]] = None
    sms_recipients: Optional[List[str]] = None
    webhook_url: Optional[str] = Field(None, max_length=1000)

    @field_validator("name")
    @classmethod
    def check_name(cls, v):
        return _validate_name(v)

    @field_validator("color")
    @classmethod
    def check_color(cls, v):
        if not COLOR_RE.match(str(v or "")):
            raise ValueError("color must be a six-digit hex value like #6366f1")
        return str(v).lower()

    @field_validator("icon")
    @classmethod
    def check_icon(cls, v):
        if v not in WATCHLIST_ICON_ALLOWLIST:
            raise ValueError(f"icon must be one of: {sorted(WATCHLIST_ICON_ALLOWLIST)}")
        return v

    @field_validator("alert_level")
    @classmethod
    def check_alert_level(cls, v):
        if v not in ALERT_LEVELS:
            raise ValueError(f"alert_level must be one of: {sorted(ALERT_LEVELS)}")
        return v


class UpdateWatchlistRequest(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = Field(None, max_length=1000)
    color: Optional[str] = None
    icon: Optional[str] = None
    alert_level: Optional[str] = None
    notify_dashboard: Optional[bool] = None
    notify_email: Optional[bool] = None
    notify_sms: Optional[bool] = None
    notify_webhook: Optional[bool] = None
    email_recipients: Optional[List[str]] = None
    sms_recipients: Optional[List[str]] = None
    webhook_url: Optional[str] = Field(None, max_length=1000)
    is_active: Optional[bool] = None
    version: Optional[int] = Field(None, description="Version read by the client (optimistic concurrency)")

    @field_validator("name")
    @classmethod
    def check_name(cls, v):
        return None if v is None else _validate_name(v)

    @field_validator("color")
    @classmethod
    def check_color(cls, v):
        if v is None:
            return v
        if not COLOR_RE.match(str(v)):
            raise ValueError("color must be a six-digit hex value like #6366f1")
        return str(v).lower()

    @field_validator("icon")
    @classmethod
    def check_icon(cls, v):
        if v is not None and v not in WATCHLIST_ICON_ALLOWLIST:
            raise ValueError(f"icon must be one of: {sorted(WATCHLIST_ICON_ALLOWLIST)}")
        return v

    @field_validator("alert_level")
    @classmethod
    def check_alert_level(cls, v):
        if v is not None and v not in ALERT_LEVELS:
            raise ValueError(f"alert_level must be one of: {sorted(ALERT_LEVELS)}")
        return v


class StatusChangeRequest(BaseModel):
    is_active: bool
    reason: Optional[str] = Field(None, max_length=500)
    version: Optional[int] = None


class DeleteWatchlistRequest(BaseModel):
    reason: Optional[str] = Field(None, max_length=500)


class AddToWatchlistRequest(BaseModel):
    identity_id: str = Field(...)
    priority: str = Field(default="normal")
    notes: Optional[str] = Field(None, max_length=1000)
    action_instructions: Optional[str] = Field(None, max_length=1000)
    expires_at: Optional[str] = None

    @field_validator("priority")
    @classmethod
    def check_priority(cls, v):
        if v not in ENTRY_PRIORITIES:
            raise ValueError(f"priority must be one of: {sorted(ENTRY_PRIORITIES)}")
        return v


class AcknowledgeAlertRequest(BaseModel):
    notes: Optional[str] = Field(None, max_length=500)


# =====================================================
# Watchlist CRUD
# =====================================================

@router.get(
    "/api/watchlists",
    summary="List / Search Watchlists",
    description="Legacy array (no page param) or paginated envelope with real per-watchlist statistics."
)
async def list_watchlists(
    include_inactive: bool = Query(default=False),
    page: Optional[int] = Query(default=None, ge=1, description="Presence switches to the paginated envelope"),
    page_size: int = Query(default=20, ge=1, le=100),
    search: Optional[str] = Query(default=None, max_length=200),
    alert_level: Optional[str] = Query(default=None),
    is_active: Optional[bool] = Query(default=None),
    include_deleted: bool = Query(default=False),
    sort_by: str = Query(default="name"),
    sort_order: str = Query(default="asc"),
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_admin())
):
    """List watchlists with batched statistics (no N+1)."""
    try:
        if alert_level is not None and alert_level not in ALERT_LEVELS:
            raise HTTPException(status_code=422, detail="Unsupported alert_level filter")

        day_start, day_end = _day_period()

        if page is not None:
            items, total = await watchlist_service.list_watchlists_page(
                db, page=page, page_size=page_size, search=search,
                alert_level=alert_level, is_active=is_active,
                include_deleted=include_deleted,
                sort_by=sort_by, sort_order=sort_order,
            )
            stats = await watchlist_service.batch_watchlist_stats(
                db, [wl.id for wl in items], day_start)
            return {
                "items": [_serialize(wl, stats.get(wl.id)) for wl in items],
                "total": total,
                "page": page,
                "page_size": page_size,
                "total_pages": max(1, (total + page_size - 1) // page_size),
                "stats_period": {
                    "period_start": _iso_z(day_start),
                    "period_end": _iso_z(day_end),
                    "timezone": REPORTING_TIMEZONE,
                },
            }

        # Legacy array shape (existing consumers) — batched counts, no N+1
        watchlists = await watchlist_service.get_all_watchlists(db, include_inactive)
        stats = await watchlist_service.batch_watchlist_stats(
            db, [wl.id for wl in watchlists], day_start)
        return [_serialize(wl, stats.get(wl.id)) for wl in watchlists]

    except HTTPException:
        raise
    except Exception as e:
        raise _safe_500("watchlist listing", e)


@router.post(
    "/api/watchlists",
    summary="Create Watchlist",
)
async def create_watchlist(
    request: CreateWatchlistRequest,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_admin()),
    _csrf: None = Depends(require_watchlist_csrf)
):
    """Create a new watchlist (409 on duplicate live name)."""
    try:
        watchlist = await watchlist_service.create_watchlist(
            db=db,
            name=request.name,
            description=request.description,
            color=request.color,
            icon=request.icon,
            alert_level=request.alert_level,
            notify_dashboard=request.notify_dashboard,
            notify_email=request.notify_email,
            notify_sms=request.notify_sms,
            notify_webhook=request.notify_webhook,
            email_recipients=request.email_recipients,
            sms_recipients=request.sms_recipients,
            webhook_url=request.webhook_url,
            created_by=current_user['id']
        )
        _audit("create", current_user, watchlist.id, name_length=len(request.name),
               alert_level=request.alert_level)
        await _broadcast_change("watchlist_created", watchlist.id)
        return _serialize(watchlist, {"entries_count": 0, "alerts_today": 0,
                                      "total_alerts": 0, "last_alert_at": None})

    except WatchlistNameConflict:
        raise HTTPException(
            status_code=409,
            detail={"error_code": "NAME_CONFLICT",
                    "message": "A watchlist with this name already exists."})
    except HTTPException:
        raise
    except Exception as e:
        _audit("create", current_user, result="error")
        raise _safe_500("watchlist creation", e)


@router.get(
    "/api/watchlists/{watchlist_id}",
    summary="Get Watchlist",
)
async def get_watchlist(
    watchlist_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_admin())
):
    """Get a specific watchlist with real statistics."""
    watchlist = await _get_watchlist_or_404(db, watchlist_id, include_deleted=True)
    try:
        day_start, day_end = _day_period()
        stats = await watchlist_service.batch_watchlist_stats(db, [watchlist.id], day_start)
        _audit("view_detail", current_user, watchlist.id)
        payload = _serialize(watchlist, stats.get(watchlist.id))
        payload["stats_period"] = {
            "period_start": _iso_z(day_start),
            "period_end": _iso_z(day_end),
            "timezone": REPORTING_TIMEZONE,
        }
        return payload
    except Exception as e:
        raise _safe_500("watchlist detail", e)


@router.put(
    "/api/watchlists/{watchlist_id}",
    summary="Update Watchlist",
)
async def update_watchlist(
    watchlist_id: str,
    request: UpdateWatchlistRequest,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_admin()),
    _csrf: None = Depends(require_watchlist_csrf)
):
    """Update a watchlist with optimistic concurrency (409 on stale version)."""
    payload = request.model_dump(exclude_none=True)
    expected_version = payload.pop("version", None)
    if not payload:
        raise HTTPException(status_code=400, detail="No updates provided")

    await _get_watchlist_or_404(db, watchlist_id)
    try:
        watchlist = await watchlist_service.update_watchlist(
            db, watchlist_id, expected_version=expected_version, **payload)
        if not watchlist:
            raise HTTPException(status_code=404, detail="Watchlist not found")
        _audit("update", current_user, watchlist.id,
               fields=",".join(sorted(payload.keys())), new_version=watchlist.version)
        await _broadcast_change("watchlist_updated", watchlist.id)
        return _serialize(watchlist)

    except WatchlistVersionConflict as vc:
        _audit("update", current_user, watchlist_id, result="version_conflict",
               current_version=vc.current_version)
        raise HTTPException(
            status_code=409,
            detail={"error_code": "VERSION_CONFLICT",
                    "message": "This watchlist was modified by another administrator. "
                               "Reload the latest version before saving.",
                    "current_version": vc.current_version})
    except WatchlistNameConflict:
        raise HTTPException(
            status_code=409,
            detail={"error_code": "NAME_CONFLICT",
                    "message": "A watchlist with this name already exists."})
    except HTTPException:
        raise
    except Exception as e:
        _audit("update", current_user, watchlist_id, result="error")
        raise _safe_500("watchlist update", e)


@router.patch(
    "/api/watchlists/{watchlist_id}/status",
    summary="Activate / Deactivate Watchlist",
    description="Inactive watchlists stop matching detections; history is preserved."
)
async def change_watchlist_status(
    watchlist_id: str,
    request: StatusChangeRequest,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_admin()),
    _csrf: None = Depends(require_watchlist_csrf)
):
    """Explicit activation state change with reason + audit."""
    await _get_watchlist_or_404(db, watchlist_id)
    try:
        watchlist = await watchlist_service.update_watchlist(
            db, watchlist_id, expected_version=request.version,
            is_active=request.is_active)
        _audit("status_change", current_user, watchlist_id,
               is_active=request.is_active, reason=(request.reason or "")[:100] or None,
               new_version=watchlist.version)
        await _broadcast_change("watchlist_status_changed", watchlist_id,
                                {"is_active": request.is_active})
        return _serialize(watchlist)
    except WatchlistVersionConflict as vc:
        raise HTTPException(
            status_code=409,
            detail={"error_code": "VERSION_CONFLICT",
                    "message": "This watchlist was modified by another administrator. "
                               "Reload the latest version before saving.",
                    "current_version": vc.current_version})
    except HTTPException:
        raise
    except Exception as e:
        raise _safe_500("watchlist status change", e)


@router.get(
    "/api/watchlists/{watchlist_id}/deletion-impact",
    summary="Deletion Impact Summary",
    description="What the administrator is about to remove — shown BEFORE deletion."
)
async def get_deletion_impact(
    watchlist_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_admin())
):
    impact = await watchlist_service.deletion_impact(db, watchlist_id)
    if impact is None:
        raise HTTPException(status_code=404, detail="Watchlist not found")
    return impact


@router.delete(
    "/api/watchlists/{watchlist_id}",
    summary="Soft-Delete Watchlist",
    description="Soft delete by default: matching stops, entries/alerts/audit are preserved. "
                "Permanent delete requires hard_delete=true AND confirm=true."
)
async def delete_watchlist(
    watchlist_id: str,
    request: Request,
    hard_delete: bool = Query(default=False),
    confirm: bool = Query(default=False),
    reason: Optional[str] = Query(default=None, max_length=500),
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_admin()),
    _csrf: None = Depends(require_watchlist_csrf)
):
    """Soft delete (default) or explicitly confirmed hard delete."""
    watchlist = await _get_watchlist_or_404(db, watchlist_id, include_deleted=True)
    try:
        impact = await watchlist_service.deletion_impact(db, watchlist_id)

        if hard_delete:
            if not confirm:
                raise HTTPException(
                    status_code=400,
                    detail={"error_code": "CONFIRMATION_REQUIRED",
                            "message": "Permanent deletion requires confirm=true.",
                            "impact": impact})
            await watchlist_service.delete_watchlist(db, watchlist_id, hard_delete=True)
            _audit("hard_delete", current_user, watchlist_id,
                   entries=impact["entries"], alerts=impact["alerts"],
                   reason=(reason or "")[:100] or None)
            await _broadcast_change("watchlist_deleted", watchlist_id, {"hard": True})
            return {"success": True, "action": "hard_deleted", "impact": impact}

        deleted = await watchlist_service.soft_delete_watchlist(
            db, watchlist_id, deleted_by_user_id=current_user.get("id"), reason=reason)
        _audit("soft_delete", current_user, watchlist_id,
               entries=impact["entries"], alerts=impact["alerts"],
               reason=(reason or "")[:100] or None)
        await _broadcast_change("watchlist_deleted", watchlist_id, {"hard": False})
        return {"success": True, "action": "soft_deleted", "impact": impact,
                "deleted_at": _iso_z(deleted.deleted_at)}

    except HTTPException:
        raise
    except Exception as e:
        _audit("delete", current_user, watchlist_id, result="error")
        raise _safe_500("watchlist deletion", e)


@router.post(
    "/api/watchlists/{watchlist_id}/restore",
    summary="Restore Soft-Deleted Watchlist",
)
async def restore_watchlist(
    watchlist_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_admin()),
    _csrf: None = Depends(require_watchlist_csrf)
):
    """Restore a soft-deleted watchlist."""
    try:
        watchlist = await watchlist_service.restore_watchlist(db, watchlist_id)
        if not watchlist:
            raise HTTPException(status_code=404, detail="Deleted watchlist not found")
        _audit("restore", current_user, watchlist_id, new_version=watchlist.version)
        await _broadcast_change("watchlist_restored", watchlist_id)
        return _serialize(watchlist)
    except WatchlistNameConflict:
        raise HTTPException(
            status_code=409,
            detail={"error_code": "NAME_CONFLICT",
                    "message": "A live watchlist with this name now exists — rename it first."})
    except HTTPException:
        raise
    except Exception as e:
        raise _safe_500("watchlist restore", e)


@router.get(
    "/api/watchlists/{watchlist_id}/stats",
    summary="Get Watchlist Statistics",
)
async def get_watchlist_stats(
    watchlist_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_admin())
):
    """Statistics with an explicit reporting period."""
    await _get_watchlist_or_404(db, watchlist_id, include_deleted=True)
    try:
        stats = await watchlist_service.get_watchlist_stats(db, watchlist_id)
        day_start, day_end = _day_period()
        return {
            "total_entries": stats.total_entries,
            "active_entries": stats.active_entries,
            "expired_entries": stats.expired_entries,
            "alerts_today": stats.alerts_today,
            "alerts_total": stats.alerts_total,
            "unacknowledged_alerts": stats.unacknowledged_alerts,
            "period_start": _iso_z(day_start),
            "period_end": _iso_z(day_end),
            "timezone": REPORTING_TIMEZONE,
        }
    except HTTPException:
        raise
    except Exception as e:
        raise _safe_500("watchlist statistics", e)


# =====================================================
# Watchlist Entries
# =====================================================

@router.get(
    "/api/watchlists/{watchlist_id}/entries",
    summary="List Watchlist Entries",
)
async def list_watchlist_entries(
    watchlist_id: str,
    include_inactive: bool = Query(default=False),
    include_expired: bool = Query(default=False),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_admin())
):
    """Paginated entries for a watchlist."""
    await _get_watchlist_or_404(db, watchlist_id, include_deleted=True)
    try:
        entries = await watchlist_service.get_entries(
            db, watchlist_id, include_inactive, include_expired)
        total = len(entries)
        page_items = entries[(page - 1) * page_size: page * page_size]

        return {
            "items": [
                {
                    "id": str(entry.id),
                    "watchlist_id": str(entry.watchlist_id),
                    "identity_id": str(entry.identity_id),
                    "identity_name": entry.identity.display_name if entry.identity else None,
                    "identity_type": entry.identity.type.value if entry.identity else None,
                    "priority": entry.priority.value,
                    "notes": entry.notes,
                    "action_instructions": entry.action_instructions,
                    "added_at": _iso_z(entry.added_at),
                    "expires_at": _iso_z(entry.expires_at),
                    "is_active": entry.is_active
                }
                for entry in page_items
            ],
            "total": total,
            "page": page,
            "page_size": page_size,
            "total_pages": max(1, (total + page_size - 1) // page_size),
        }
    except HTTPException:
        raise
    except Exception as e:
        raise _safe_500("entry listing", e)


@router.post(
    "/api/watchlists/{watchlist_id}/entries",
    summary="Add to Watchlist",
    description="Idempotent: adding an identity that is already on the list updates the entry."
)
async def add_to_watchlist(
    watchlist_id: str,
    request: AddToWatchlistRequest,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_admin()),
    _csrf: None = Depends(require_watchlist_csrf)
):
    """Add an identity to a watchlist (duplicate-safe)."""
    await _get_watchlist_or_404(db, watchlist_id)

    # Verify the identity exists (404, never a 500 from a bad UUID)
    from db_models import Identity
    from sqlalchemy import select
    try:
        identity_uuid = uuid.UUID(request.identity_id)
    except (ValueError, TypeError):
        raise HTTPException(status_code=404, detail="Identity not found")
    identity = (await db.execute(
        select(Identity).where(Identity.id == identity_uuid))).scalar_one_or_none()
    if not identity:
        raise HTTPException(status_code=404, detail="Identity not found")

    expires_at = None
    if request.expires_at:
        try:
            expires_at = datetime.fromisoformat(request.expires_at.replace('Z', '+00:00'))
            expires_at = expires_at.replace(tzinfo=None)
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid expires_at format")

    try:
        entry = await watchlist_service.add_to_watchlist(
            db=db,
            watchlist_id=watchlist_id,
            identity_id=request.identity_id,
            priority=request.priority,
            notes=request.notes,
            action_instructions=request.action_instructions,
            expires_at=expires_at,
            added_by=current_user['id']
        )
        _audit("entry_add", current_user, watchlist_id,
               identity_id=request.identity_id, priority=request.priority)
        await _broadcast_change("watchlist_entry_added", watchlist_id,
                                {"identity_id": request.identity_id})
        return {
            "id": str(entry.id),
            "watchlist_id": str(entry.watchlist_id),
            "identity_id": str(entry.identity_id),
            "identity_name": identity.display_name,
            "identity_type": identity.type.value if identity.type else None,
            "priority": entry.priority.value,
            "notes": entry.notes,
            "action_instructions": entry.action_instructions,
            "added_at": _iso_z(entry.added_at),
            "expires_at": _iso_z(entry.expires_at),
            "is_active": entry.is_active
        }

    except HTTPException:
        raise
    except Exception as e:
        _audit("entry_add", current_user, watchlist_id, result="error")
        raise _safe_500("entry creation", e)


@router.delete(
    "/api/watchlists/{watchlist_id}/entries/{identity_id}",
    summary="Remove from Watchlist",
)
async def remove_from_watchlist(
    watchlist_id: str,
    identity_id: str,
    hard_delete: bool = Query(default=False),
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_admin()),
    _csrf: None = Depends(require_watchlist_csrf)
):
    """Remove an identity from a watchlist."""
    try:
        success = await watchlist_service.remove_from_watchlist(
            db, watchlist_id, identity_id, hard_delete)
        if not success:
            raise HTTPException(status_code=404, detail="Entry not found")
        _audit("entry_remove", current_user, watchlist_id, identity_id=identity_id)
        await _broadcast_change("watchlist_entry_removed", watchlist_id,
                                {"identity_id": identity_id})
        return {"success": True}
    except HTTPException:
        raise
    except Exception as e:
        raise _safe_500("entry removal", e)


@router.get(
    "/api/identities/{identity_id}/watchlists",
    summary="Get Identity Watchlists",
)
async def get_identity_watchlists(
    identity_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_admin())
):
    """Get all watchlists an identity is on."""
    try:
        watchlists = await watchlist_service.get_identity_watchlists(db, identity_id)
        return {"watchlists": watchlists}
    except ValueError:
        raise HTTPException(status_code=404, detail="Identity not found")
    except Exception as e:
        raise _safe_500("identity watchlist lookup", e)


@router.get(
    "/api/watchlists/add-identity/{identity_id}/defaults",
    summary="Get Defaults for Adding Identity to Watchlist",
)
async def get_add_to_watchlist_defaults(
    identity_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_admin())
):
    """Available watchlists + defaults for the add-identity dialog."""
    from db_models import Identity
    from sqlalchemy import select

    try:
        identity_uuid = uuid.UUID(identity_id)
    except (ValueError, TypeError):
        raise HTTPException(status_code=404, detail="Identity not found")
    identity = (await db.execute(
        select(Identity).where(Identity.id == identity_uuid))).scalar_one_or_none()
    if not identity:
        raise HTTPException(status_code=404, detail="Identity not found")

    try:
        watchlists = await watchlist_service.get_all_watchlists(db, include_inactive=False)
        existing_watchlists = await watchlist_service.get_identity_watchlists(db, identity_id)
        existing_watchlist_ids = {str(wl['watchlist_id']) for wl in existing_watchlists}

        return {
            "identity_id": identity_id,
            "identity_name": identity.display_name or "Unknown",
            "identity_type": identity.type.value if identity.type else None,
            "available_watchlists": [
                {
                    "id": str(wl.id),
                    "name": wl.name,
                    "description": wl.description,
                    "alert_level": wl.alert_level.value,
                    "color": wl.color,
                    "icon": wl.icon,
                    "is_already_added": str(wl.id) in existing_watchlist_ids
                }
                for wl in watchlists
            ],
            "default_priority": "normal",
            "can_add": True
        }
    except Exception as e:
        raise _safe_500("watchlist defaults", e)


# =====================================================
# Alerts
# =====================================================

@router.get(
    "/api/watchlist-alerts",
    summary="List Watchlist Alerts",
)
async def list_alerts(
    watchlist_id: str = Query(default=None),
    acknowledged: bool = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_admin())
):
    """Get watchlist alerts."""
    try:
        alerts = await watchlist_service.get_alerts(
            db, watchlist_id, acknowledged, limit, offset)

        return [
            {
                "id": str(alert.id),
                "watchlist_name": alert.entry.watchlist.name if alert.entry and alert.entry.watchlist else "Unknown",
                "identity_id": str(alert.entry.identity_id) if alert.entry else None,
                "identity_name": alert.entry.identity.display_name if alert.entry and alert.entry.identity else None,
                "triggered_by": alert.triggered_by,
                "similarity_score": alert.similarity_score,
                "pipeline_id": alert.pipeline_id,
                "acknowledged": alert.acknowledged,
                "acknowledged_by": alert.acknowledged_by_user.username if alert.acknowledged_by_user else None,
                "acknowledged_at": _iso_z(alert.acknowledged_at),
                "notes": alert.notes,
                "created_at": _iso_z(alert.created_at)
            }
            for alert in alerts
        ]
    except ValueError:
        raise HTTPException(status_code=404, detail="Watchlist not found")
    except Exception as e:
        raise _safe_500("alert listing", e)


@router.post(
    "/api/watchlist-alerts/{alert_id}/acknowledge",
    summary="Acknowledge Alert",
)
async def acknowledge_alert(
    alert_id: str,
    request: AcknowledgeAlertRequest = None,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_admin()),
    _csrf: None = Depends(require_watchlist_csrf)
):
    """Acknowledge a watchlist alert."""
    try:
        notes = request.notes if request else None
        success = await watchlist_service.acknowledge_alert(
            db, alert_id, current_user['id'], notes)
        if not success:
            raise HTTPException(status_code=404, detail="Alert not found")
        _audit("alert_acknowledge", current_user, alert_id=alert_id)
        return {"success": True}
    except ValueError:
        raise HTTPException(status_code=404, detail="Alert not found")
    except HTTPException:
        raise
    except Exception as e:
        raise _safe_500("alert acknowledgement", e)

"""
User Management Routes
=====================
Admin-only user management endpoints.
"""

import os
import sys
import logging
from datetime import datetime
from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession
from pydantic import BaseModel

# Add parent directory to path
parent_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if parent_dir not in sys.path:
    sys.path.insert(0, parent_dir)

from db_connection import get_db
from db_models import User, UserPipelineAccess
from backend.auth.auth_service import get_current_user, require_role
from backend.auth.capabilities import (
    Role,
    assignable_role_values,
    canonical_role,
    is_known_role,
)
from backend.services.user_service import UserService

logger = logging.getLogger(__name__)


async def _guard_last_administrator(*, db: AsyncSession, target_user_id: int,
                                    new_role=None, new_is_active=None) -> None:
    """Refuse a change that would remove the final active administrator.

    Applies to demotion (role change away from admin) and to deactivation,
    since an inactive admin resolves to zero capabilities and is therefore just
    as unable to administer.

    Counts only ACTIVE admins: an already-disabled admin account is not a
    usable escape hatch.
    """
    from sqlalchemy import func, select as _select

    demoting = (new_role is not None
                and canonical_role(new_role) is not Role.ADMIN)
    deactivating = new_is_active is False
    if not (demoting or deactivating):
        return

    target = (await db.execute(
        _select(User).where(User.id == target_user_id)
    )).scalar_one_or_none()
    if target is None or canonical_role(target.role) is not Role.ADMIN:
        return  # not an admin; nothing to protect

    remaining = (await db.execute(
        _select(func.count()).select_from(User).where(
            User.role == Role.ADMIN.value,
            User.is_active.is_(True),
            User.id != target_user_id,
        )
    )).scalar_one()

    if remaining == 0:
        action = "demote" if demoting else "deactivate"
        logger.warning(
            "[UPDATE_USER_ROUTE] ❌ Blocked attempt to %s the last administrator (user_id=%s)",
            action, target_user_id,
        )
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "LAST_ADMINISTRATOR",
                "message": (
                    f"Cannot {action} the only remaining administrator. "
                    "Promote another account to administrator first."
                ),
            },
        )


def require_user_admin_csrf(request: Request):
    """CSRF defense-in-depth for cookie-authenticated user administration.

    These endpoints create, modify and delete accounts and their permissions,
    yet were the only mutating admin surface without a CSRF dependency — the
    watchlist, intelligence and sql-agent routes all had one. A logged-in
    admin visiting an attacker's page could therefore have their browser
    silently change any user's role.

    Same policy as require_sql_agent_csrf: SameSite cookie + custom header.
    Bearer clients are exempt because a bearer token cannot be attached
    cross-site by the browser.
    """
    if request.headers.get("authorization"):
        return
    if request.headers.get("x-requested-with", "").lower() != "xmlhttprequest":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="CSRF check failed: X-Requested-With header required",
        )


router = APIRouter()


class CreateUserRequest(BaseModel):
    username: str
    email: str
    password: str
    full_name: Optional[str] = None
    role: str = "user"
    can_use_chatbot: bool = False
    pipeline_ids: Optional[List[str]] = None


class UpdateUserRequest(BaseModel):
    email: Optional[str] = None
    password: Optional[str] = None  # Optional password for updating
    full_name: Optional[str] = None
    role: Optional[str] = None
    can_use_chatbot: Optional[bool] = None
    is_active: Optional[bool] = None
    pipeline_ids: Optional[List[str]] = None


class ResetPasswordRequest(BaseModel):
    new_password: str


class UserResponse(BaseModel):
    id: int
    username: str
    email: str
    full_name: Optional[str]
    role: str
    can_use_chatbot: bool
    is_active: bool
    blocked_reason: Optional[str] = None
    blocked_at: Optional[str] = None
    created_at: str
    last_login: Optional[str]
    pipeline_ids: List[str]


@router.post("/api/users", response_model=UserResponse)
async def create_user(
    user_data: CreateUserRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_role(["admin"])),
    _csrf: None = Depends(require_user_admin_csrf)
):
    """Create a new user (admin only)"""
    logger.info(f"[CREATE_USER_ROUTE] 👤 User creation request")
    logger.debug(f"[CREATE_USER_ROUTE]   Requested by admin: {current_user.username} (ID: {current_user.id})")
    logger.debug(f"[CREATE_USER_ROUTE]   User data: username={user_data.username}, email={user_data.email}, role={user_data.role}")
    logger.debug(f"[CREATE_USER_ROUTE]   Password provided: {bool(user_data.password)}, Length: {len(user_data.password) if user_data.password else 0}")
    
    # Prevent creating admin users through the API. Compared canonically so
    # "Administrator" or " ADMIN " cannot slip past a lowercase equality check.
    if canonical_role(user_data.role) is Role.ADMIN:
        logger.warning(f"[CREATE_USER_ROUTE] ❌ SECURITY: Attempt to create admin user blocked by {current_user.username}")
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin users cannot be created through this interface. Admin users must be created through system initialization or direct database access."
        )

    # is_known_role first: canonical_role falls back to the least privileged
    # role, so an unrecognised value would otherwise pass as "observer" and
    # create the account with silently wrong permissions.
    if (not is_known_role(user_data.role)
            or canonical_role(user_data.role).value not in assignable_role_values()):
        logger.warning(f"[CREATE_USER_ROUTE] ❌ Invalid role '{user_data.role}'. Allowed: {assignable_role_values()}")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid role '{user_data.role}'. Allowed roles are: {', '.join(assignable_role_values())}"
        )
    
    try:
        user = await UserService.create_user(
            username=user_data.username,
            email=user_data.email,
            password=user_data.password,
            full_name=user_data.full_name,
            role=user_data.role,
            can_use_chatbot=user_data.can_use_chatbot,
            pipeline_ids=user_data.pipeline_ids,
            db=db
        )
        logger.info(f"[CREATE_USER_ROUTE] ✅✅✅ User created successfully: {user.username} (ID: {user.id})")
        
        # Get pipeline IDs
        from sqlalchemy import select
        result = await db.execute(
            select(UserPipelineAccess.pipeline_id)
            .where(UserPipelineAccess.user_id == user.id)
        )
        pipeline_ids = [row[0] for row in result.all()]
        
        return UserResponse(
            id=user.id,
            username=user.username,
            email=user.email,
            full_name=user.full_name,
            role=user.role,
            can_use_chatbot=user.can_use_chatbot,
            is_active=user.is_active,
            blocked_reason=user.blocked_reason,
            blocked_at=user.blocked_at.isoformat() if user.blocked_at else None,
            created_at=user.created_at.isoformat(),
            last_login=user.last_login.isoformat() if user.last_login else None,
            pipeline_ids=pipeline_ids
        )
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))


@router.get("/api/users", response_model=List[UserResponse])
async def list_users(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_role(["admin"]))
):
    """List all users (admin only)"""
    users = await UserService.get_all_users(db)
    
    result = []
    for user in users:
        # Get pipeline IDs for each user
        from sqlalchemy import select
        pipeline_result = await db.execute(
            select(UserPipelineAccess.pipeline_id)
            .where(UserPipelineAccess.user_id == user.id)
        )
        pipeline_ids = [row[0] for row in pipeline_result.all()]
        
        result.append(UserResponse(
            id=user.id,
            username=user.username,
            email=user.email,
            full_name=user.full_name,
            role=user.role,
            can_use_chatbot=user.can_use_chatbot,
            is_active=user.is_active,
            blocked_reason=user.blocked_reason,
            blocked_at=user.blocked_at.isoformat() if user.blocked_at else None,
            created_at=user.created_at.isoformat(),
            last_login=user.last_login.isoformat() if user.last_login else None,
            pipeline_ids=pipeline_ids
        ))
    
    return result


@router.get("/api/users/{user_id}", response_model=UserResponse)
async def get_user(
    user_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_role(["admin"]))
):
    """Get user by ID (admin only)"""
    user = await UserService.get_user_by_id(user_id, db)
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    
    # Get pipeline IDs
    from sqlalchemy import select
    result = await db.execute(
        select(UserPipelineAccess.pipeline_id)
        .where(UserPipelineAccess.user_id == user.id)
    )
    pipeline_ids = [row[0] for row in result.all()]
    
    return UserResponse(
        id=user.id,
        username=user.username,
        email=user.email,
        full_name=user.full_name,
        role=user.role,
        can_use_chatbot=user.can_use_chatbot,
        is_active=user.is_active,
        blocked_reason=user.blocked_reason,
        blocked_at=user.blocked_at.isoformat() if user.blocked_at else None,
        created_at=user.created_at.isoformat(),
        last_login=user.last_login.isoformat() if user.last_login else None,
        pipeline_ids=pipeline_ids
    )


@router.put("/api/users/{user_id}", response_model=UserResponse)
async def update_user(
    user_id: int,
    user_data: UpdateUserRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_role(["admin"])),
    _csrf: None = Depends(require_user_admin_csrf),
):
    """Update user (admin only)"""
    logger.info(f"[UPDATE_USER_ROUTE] ✏️  Update user request for user ID: {user_id}")
    logger.debug(f"[UPDATE_USER_ROUTE]   Requested by admin: {current_user.username} (ID: {current_user.id})")
    logger.debug(f"[UPDATE_USER_ROUTE]   Update data: email={user_data.email is not None}, password={'***' if user_data.password else None}, full_name={user_data.full_name is not None}, role={user_data.role}, is_active={user_data.is_active}")

    # Prevent changing user role to admin through the API.
    # Compared canonically so "Administrator" or " ADMIN " cannot slip past a
    # plain lowercase equality check.
    if user_data.role and canonical_role(user_data.role) is Role.ADMIN:
        logger.warning(f"[UPDATE_USER_ROUTE] ❌ SECURITY: Attempt to change user role to admin blocked by {current_user.username}")
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Cannot change user role to 'admin' through this interface. Admin role must be assigned through system initialization or direct database access."
        )

    # Validate the submitted role against the canonical assignable set.
    # An unrecognised value is rejected rather than silently stored — an
    # unknown role resolves to the LEAST privileged capabilities, so accepting
    # a typo would quietly strip a user's access.
    if user_data.role:
        # is_known_role first: canonical_role falls back to the least
        # privileged role, so an unrecognised value would otherwise pass this
        # check as "observer" and silently demote the user.
        if (not is_known_role(user_data.role)
                or canonical_role(user_data.role).value not in assignable_role_values()):
            logger.warning(
                "[UPDATE_USER_ROUTE] ❌ Invalid role %r. Allowed: %s",
                user_data.role, assignable_role_values(),
            )
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid role '{user_data.role}'. Allowed roles are: {', '.join(assignable_role_values())}"
            )

    # Never leave the system without an administrator.
    #
    # This is not hypothetical: it happened during development. The role
    # dropdown has no "admin" option, so opening an administrator renders the
    # select blank, and choosing any value demotes them. With one admin
    # account that locks everyone out of user management permanently — the
    # only recovery is direct database access.
    await _guard_last_administrator(
        db=db, target_user_id=user_id,
        new_role=user_data.role, new_is_active=user_data.is_active,
    )

    try:
        user = await UserService.update_user(
            user_id=user_id,
            email=user_data.email,
            password=user_data.password,  # Add password parameter
            full_name=user_data.full_name,
            role=user_data.role,
            can_use_chatbot=user_data.can_use_chatbot,
            is_active=user_data.is_active,
            pipeline_ids=user_data.pipeline_ids,
            db=db,
            actor=current_user,
            context={
                "request_id": request.headers.get("X-Request-ID"),
                # nginx does not trust client-supplied X-Forwarded-For, so the
                # socket address is the real one.
                "ip_address": request.client.host if request.client else None,
                "user_agent": request.headers.get("user-agent"),
            },
        )
        logger.info(f"[UPDATE_USER_ROUTE] ✅✅✅ User updated successfully: {user.username} (ID: {user.id})")
        
        # Get pipeline IDs
        from sqlalchemy import select
        result = await db.execute(
            select(UserPipelineAccess.pipeline_id)
            .where(UserPipelineAccess.user_id == user.id)
        )
        pipeline_ids = [row[0] for row in result.all()]
        
        return UserResponse(
            id=user.id,
            username=user.username,
            email=user.email,
            full_name=user.full_name,
            role=user.role,
            can_use_chatbot=user.can_use_chatbot,
            is_active=user.is_active,
            blocked_reason=user.blocked_reason,
            blocked_at=user.blocked_at.isoformat() if user.blocked_at else None,
            created_at=user.created_at.isoformat(),
            last_login=user.last_login.isoformat() if user.last_login else None,
            pipeline_ids=pipeline_ids
        )
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))


@router.post("/api/users/{user_id}/reset-password")
async def reset_password(
    user_id: int,
    password_data: ResetPasswordRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_role(["admin"])),
    _csrf: None = Depends(require_user_admin_csrf)
):
    """Reset user password (admin only)"""
    logger.info(f"[RESET_PASSWORD_ROUTE] 🔐 Password reset request for user ID: {user_id}")
    logger.debug(f"[RESET_PASSWORD_ROUTE]   Requested by admin: {current_user.username} (ID: {current_user.id})")
    logger.debug(f"[RESET_PASSWORD_ROUTE]   New password length: {len(password_data.new_password) if password_data.new_password else 0}")
    
    try:
        user = await UserService.reset_password(user_id, password_data.new_password, db)
        logger.info(f"[RESET_PASSWORD_ROUTE] ✅✅✅ Password reset successful for user: {user.username} (ID: {user.id})")
        return {"message": f"Password reset successfully for user {user.username}"}
    except ValueError as e:
        logger.error(f"[RESET_PASSWORD_ROUTE] ❌ ValueError: {str(e)}")
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    except Exception as e:
        logger.error(f"[RESET_PASSWORD_ROUTE] ❌❌❌ Unexpected error: {type(e).__name__}: {str(e)}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"An error occurred while resetting the password: {str(e)}"
        )


@router.delete("/api/users/{user_id}")
async def delete_user(
    user_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_role(["admin"])),
    _csrf: None = Depends(require_user_admin_csrf)
):
    """Delete user (admin only)"""
    if user_id == current_user.id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Cannot delete yourself")
    
    try:
        success = await UserService.delete_user(user_id, db)
        if not success:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
        
        return {"message": "User deleted successfully"}
    except ValueError as e:
        logger.error(f"Error deleting user {user_id}: {str(e)}", exc_info=True)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))
    except Exception as e:
        logger.error(f"Unexpected error deleting user {user_id}: {str(e)}", exc_info=True)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Failed to delete user: {str(e)}")


@router.get("/api/users/me/pipelines")
async def get_my_pipelines(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Get current user's accessible pipelines"""
    from backend.auth.auth_service import AuthService
    
    # Admin has access to all pipelines
    if current_user.role == "admin":
        from sqlalchemy import select
        from db_models import Pipeline
        result = await db.execute(select(Pipeline.pipeline_id))
        return [row[0] for row in result.all()]
    
    # Regular users get their assigned pipelines
    pipelines = await AuthService.get_user_pipelines(current_user.id, db)
    return pipelines


@router.post("/api/users/{user_id}/unblock")
async def unblock_user(
    user_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_role(["admin"])),
    _csrf: None = Depends(require_user_admin_csrf)
):
    """Unblock a user (admin only) - restores system access"""
    try:
        user = await UserService.unblock_user(user_id, db)
        
        # Get pipeline IDs
        from sqlalchemy import select
        result = await db.execute(
            select(UserPipelineAccess.pipeline_id)
            .where(UserPipelineAccess.user_id == user.id)
        )
        pipeline_ids = [row[0] for row in result.all()]
        
        return UserResponse(
            id=user.id,
            username=user.username,
            email=user.email,
            full_name=user.full_name,
            role=user.role,
            can_use_chatbot=user.can_use_chatbot,
            is_active=user.is_active,
            blocked_reason=user.blocked_reason,
            blocked_at=user.blocked_at.isoformat() if user.blocked_at else None,
            created_at=user.created_at.isoformat(),
            last_login=user.last_login.isoformat() if user.last_login else None,
            pipeline_ids=pipeline_ids
        )
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))


@router.get("/api/pipelines")
async def get_all_pipelines(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    Get pipelines accessible to the current user.
    - Admin users: Get all pipelines
    - Regular users: Get only pipelines they have access to
    """
    from sqlalchemy import select
    from db_models import Pipeline, UserPipelineAccess
    
    # Admin users get all pipelines
    if current_user.role == "admin":
        result = await db.execute(select(Pipeline).order_by(Pipeline.pipeline_id))
        pipelines = result.scalars().all()
    else:
        # Regular users: Get only pipelines they have access to
        pipeline_access_result = await db.execute(
            select(UserPipelineAccess.pipeline_id)
            .where(UserPipelineAccess.user_id == current_user.id)
        )
        accessible_pipeline_ids = [row[0] for row in pipeline_access_result.all()]
        
        if not accessible_pipeline_ids:
            # User has no pipeline access
            return []
        
        result = await db.execute(
            select(Pipeline)
            .where(Pipeline.pipeline_id.in_(accessible_pipeline_ids))
            .order_by(Pipeline.pipeline_id)
        )
        pipelines = result.scalars().all()
    
    return [
        {
            "pipeline_id": p.pipeline_id,
            "pipeline_name": p.location_name or p.pipeline_id,  # Use location_name if available, fallback to pipeline_id
            "is_active": bool(p.is_active),
            "total_detections": p.total_detections or 0,
            "created_at": p.created_at.isoformat() if p.created_at else None,
            "updated_at": p.updated_at.isoformat() if p.updated_at else None,
            "latitude": float(p.latitude) if p.latitude is not None else None,
            "longitude": float(p.longitude) if p.longitude is not None else None,
            "location_name": p.location_name
        }
        for p in pipelines
    ]


@router.put("/api/pipelines/{pipeline_id}/coordinates")
async def update_pipeline_coordinates(
    pipeline_id: str,
    coordinates: dict,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_role(["admin"]))
):
    """Update pipeline coordinates (latitude, longitude, location_name)"""
    from sqlalchemy import select, update
    from db_models import Pipeline
    
    # Validate coordinates
    latitude = coordinates.get("latitude")
    longitude = coordinates.get("longitude")
    location_name = coordinates.get("location_name")
    
    if latitude is not None:
        try:
            latitude = float(latitude)
            if not (-90 <= latitude <= 90):
                raise HTTPException(status_code=400, detail="Latitude must be between -90 and 90")
        except (ValueError, TypeError):
            raise HTTPException(status_code=400, detail="Invalid latitude value")
    
    if longitude is not None:
        try:
            longitude = float(longitude)
            if not (-180 <= longitude <= 180):
                raise HTTPException(status_code=400, detail="Longitude must be between -180 and 180")
        except (ValueError, TypeError):
            raise HTTPException(status_code=400, detail="Invalid longitude value")
    
    # Get pipeline
    result = await db.execute(select(Pipeline).where(Pipeline.pipeline_id == pipeline_id))
    pipeline = result.scalar_one_or_none()
    
    if not pipeline:
        raise HTTPException(status_code=404, detail=f"Pipeline '{pipeline_id}' not found")
    
    # Update coordinates
    if latitude is not None:
        pipeline.latitude = latitude
    if longitude is not None:
        pipeline.longitude = longitude
    if location_name is not None:
        pipeline.location_name = location_name
    
    pipeline.updated_at = datetime.utcnow()
    
    await db.commit()
    await db.refresh(pipeline)
    
    logger.info(f"[PIPELINE_COORDS] Updated coordinates for pipeline {pipeline_id}: lat={latitude}, lng={longitude}, name={location_name}")
    
    return {
        "pipeline_id": pipeline.pipeline_id,
        "latitude": float(pipeline.latitude) if pipeline.latitude is not None else None,
        "longitude": float(pipeline.longitude) if pipeline.longitude is not None else None,
        "location_name": pipeline.location_name,
        "updated_at": pipeline.updated_at.isoformat() if pipeline.updated_at else None
    }


@router.put("/api/pipelines/{pipeline_id}/rename")
async def rename_pipeline(
    pipeline_id: str,
    body: dict,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_role(["admin"]))
):
    """Rename a pipeline (e.g. a tool-generated UUID -> a friendly camera name).

    Cascades the id everywhere it is referenced and records an ALIAS so future
    webhooks still using the old id land in the renamed pipeline automatically.
    """
    import re as _re
    from sqlalchemy import select, text as sa_text
    from db_models import Pipeline

    new_name = (body.get("new_name") or "").strip()
    # Sanitize like the webhook does (spaces -> underscore), then validate
    new_name = _re.sub(r'[^a-zA-Z0-9_-]', '_', new_name).strip('_')
    if not _re.match(r'^[a-zA-Z0-9_-]{3,100}$', new_name):
        raise HTTPException(status_code=400, detail="Invalid new name. Use 3-100 chars: letters, digits, dashes, underscores.")
    if new_name == pipeline_id:
        raise HTTPException(status_code=400, detail="New name is identical to the current name.")

    # Old pipeline must exist; new name must not
    result = await db.execute(select(Pipeline).where(Pipeline.pipeline_id == pipeline_id))
    old_pipeline = result.scalar_one_or_none()
    if not old_pipeline:
        raise HTTPException(status_code=404, detail=f"Pipeline '{pipeline_id}' not found")

    result = await db.execute(select(Pipeline).where(Pipeline.pipeline_id == new_name))
    if result.scalar_one_or_none():
        raise HTTPException(status_code=409, detail=f"A pipeline named '{new_name}' already exists")

    try:
        # 1. Create the new pipelines row carrying over the old row's data
        await db.execute(sa_text("""
            INSERT INTO pipelines (pipeline_id, created_at, updated_at, total_detections, is_active,
                                   latitude, longitude, location_name)
            SELECT :new_name, created_at, NOW(), total_detections, is_active,
                   latitude, longitude, location_name
            FROM pipelines WHERE pipeline_id = :old_name
            ON CONFLICT (pipeline_id) DO NOTHING
        """), {"new_name": new_name, "old_name": pipeline_id})

        # 2. Repoint every referencing table (FK-bound first, then free-string columns)
        for table in ("detections", "user_pipeline_access", "identity_embeddings",
                      "identity_appearances", "watchlist_alerts", "live_alert_triggers"):
            await db.execute(sa_text(
                f"UPDATE {table} SET pipeline_id = :new_name WHERE pipeline_id = :old_name"
            ), {"new_name": new_name, "old_name": pipeline_id})

        # 3. Remove the old pipelines row
        await db.execute(sa_text("DELETE FROM pipelines WHERE pipeline_id = :old_name"),
                         {"old_name": pipeline_id})

        # 4. Alias old -> new, and re-point any aliases that targeted the old name
        await db.execute(sa_text("""
            INSERT INTO pipeline_aliases (old_pipeline_id, new_pipeline_id, created_at)
            VALUES (:old_name, :new_name, NOW())
            ON CONFLICT (old_pipeline_id) DO UPDATE SET new_pipeline_id = EXCLUDED.new_pipeline_id
        """), {"new_name": new_name, "old_name": pipeline_id})
        await db.execute(sa_text(
            "UPDATE pipeline_aliases SET new_pipeline_id = :new_name WHERE new_pipeline_id = :old_name"
        ), {"new_name": new_name, "old_name": pipeline_id})

        await db.commit()
    except HTTPException:
        raise
    except Exception as e:
        await db.rollback()
        logger.error(f"[PIPELINE_RENAME] Failed to rename '{pipeline_id}' -> '{new_name}': {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Rename failed: {e}")

    # Invalidate caches so the change is visible immediately
    try:
        from backend.routes.webhook import invalidate_alias_cache
        invalidate_alias_cache()
    except Exception:
        pass
    try:
        from backend.core.redis_cache import redis_cache_service
        await redis_cache_service.invalidate_dashboard_cache(user_id=None)
        await redis_cache_service.invalidate_unknown_cache(user_id=None)
    except Exception as cache_err:
        logger.debug(f"[PIPELINE_RENAME] Cache invalidation skipped: {cache_err}")

    logger.info(f"[PIPELINE_RENAME] ✅ '{pipeline_id}' renamed to '{new_name}' (alias created)")
    return {"success": True, "old_name": pipeline_id, "new_name": new_name}


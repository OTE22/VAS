"""Administrator directory of known identities; photos remain owned by enrollment."""
from datetime import datetime
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import String, cast, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.auth.auth_service import require_role
from backend.core.enrollment_service import EnrollmentError, validate_person_name
from backend.routes.upload import require_upload_csrf
from backend.utils.time_utils import iso_utc
from backend.utils.path_utils import path_to_url
from config import settings
from db_connection import get_db
from db_models import Identity, IdentityAuditLog, IdentityImage, IdentityStatus, IdentityType, User

router = APIRouter(tags=["Known Faces"], dependencies=[Depends(require_role(["admin"]))])


@router.get("/api/admin/known-faces")
async def list_known_faces(
    q: str = Query("", max_length=200),
    status: Literal["current", "all", "active", "promoted", "inactive", "merged"] = "current",
    sort: Literal["name", "newest", "last_seen"] = "newest",
    page: int = Query(1, ge=1),
    page_size: int = Query(24, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
):
    filters = [Identity.type == IdentityType.KNOWN]
    if status == "current":
        filters.extend([Identity.status.in_([IdentityStatus.ACTIVE, IdentityStatus.PROMOTED]),
                        Identity.merged_into_id.is_(None)])
    elif status != "all":
        filters.append(Identity.status == IdentityStatus(status))
    if q.strip():
        escaped = q.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        filters.append(or_(Identity.display_name.ilike(f"%{escaped}%", escape="\\"),
                           cast(Identity.id, String).ilike(f"{escaped}%", escape="\\")))
    total = (await db.execute(select(func.count()).select_from(Identity).where(*filters))).scalar() or 0
    order = {"name": Identity.display_name.asc().nulls_last(),
             "newest": Identity.created_at.desc(),
             "last_seen": Identity.last_seen_at.desc()}[sort]
    identities = (await db.execute(select(Identity).where(*filters)
        .order_by(order, Identity.id).offset((page - 1) * page_size).limit(page_size))).scalars().all()
    ids = [i.id for i in identities]
    # Two bounded batch queries, independent of the number of cards on the page.
    counts, primary = {}, {}
    if ids:
        counts = dict((await db.execute(select(IdentityImage.identity_id, func.count())
            .where(IdentityImage.identity_id.in_(ids))
            .group_by(IdentityImage.identity_id))).all())
        primary = dict((await db.execute(select(IdentityImage.identity_id, IdentityImage.storage_path)
            .where(IdentityImage.identity_id.in_(ids), IdentityImage.is_primary.is_(True)))).all())
    return {"items": [{
        "id": str(i.id), "display_name": i.display_name or "Unnamed person",
        "status": i.status.value, "photo_count": counts.get(i.id, 0),
        "photo_url": path_to_url(primary.get(i.id) or i.best_snapshot_path, settings.STORAGE_DIR)
                     if primary.get(i.id) or i.best_snapshot_path else None,
        "created_at": iso_utc(i.created_at),
        "last_seen_at": iso_utc(i.last_seen_at) if i.appearances_count else None,
        "appearances_count": i.appearances_count,
        "can_edit": i.status != IdentityStatus.MERGED and i.merged_into_id is None,
        "can_add_photo": i.status in (IdentityStatus.ACTIVE, IdentityStatus.PROMOTED) and i.merged_into_id is None,
    } for i in identities], "total": total, "page": page, "page_size": page_size,
        "total_pages": max(1, (total + page_size - 1) // page_size)}


class RenameKnownFace(BaseModel):
    display_name: str = Field(min_length=1, max_length=255)


class KnownFaceActivation(BaseModel):
    active: bool


class DeleteKnownFace(BaseModel):
    confirmation_name: str = Field(min_length=1, max_length=255)
    preview_token: str = Field(min_length=64, max_length=64)


@router.post("/api/admin/known-faces/{identity_id}/activation")
async def activate_known_face(identity_id: UUID, body: KnownFaceActivation,
        current_user: User = Depends(require_role(["admin"])),
        _csrf: None = Depends(require_upload_csrf), db: AsyncSession = Depends(get_db)):
    from backend.core.known_face_lifecycle import set_active
    return await set_active(db, identity_id, body.active, current_user)


@router.get("/api/admin/known-faces/{identity_id}/deletion-preview")
async def preview_known_face_deletion(identity_id: UUID, db: AsyncSession = Depends(get_db)):
    from backend.core.known_face_lifecycle import deletion_plan
    try:
        _, impact = await deletion_plan(db, identity_id)
        return impact
    except ValueError as exc:
        raise HTTPException(409, "A stored file path needs repair before this person can be deleted.") from exc


@router.delete("/api/admin/known-faces/{identity_id}")
async def delete_known_face(identity_id: UUID, body: DeleteKnownFace,
        current_user: User = Depends(require_role(["admin"])),
        _csrf: None = Depends(require_upload_csrf), db: AsyncSession = Depends(get_db)):
    from backend.core.known_face_lifecycle import permanently_delete
    try:
        return await permanently_delete(db, identity_id, body.confirmation_name,
                                        body.preview_token, current_user)
    except ValueError as exc:
        raise HTTPException(409, "A stored file path needs repair before deletion. Nothing was reported deleted.") from exc


@router.patch("/api/admin/known-faces/{identity_id}")
async def rename_known_face(identity_id: UUID, body: RenameKnownFace, request: Request,
                            current_user: User = Depends(require_role(["admin"])),
                            _csrf: None = Depends(require_upload_csrf),
                            db: AsyncSession = Depends(get_db)):
    try:
        name = validate_person_name(body.display_name)
    except EnrollmentError as exc:
        raise HTTPException(status_code=400, detail=exc.message) from exc
    identity = (await db.execute(select(Identity).where(
        Identity.id == identity_id, Identity.type == IdentityType.KNOWN).with_for_update())).scalar_one_or_none()
    if identity is None:
        raise HTTPException(status_code=404, detail="Known person not found.")
    if identity.status == IdentityStatus.MERGED or identity.merged_into_id is not None:
        raise HTTPException(status_code=409, detail="This record has been merged. Open the surviving profile.")
    old_name = identity.display_name
    if old_name != name:
        identity.display_name = name
        # Mutation and audit share one transaction. Renaming never moves files
        # or changes recognition vectors / camera last-seen timestamps.
        db.add(IdentityAuditLog(user_id=current_user.id, historical_user_id=current_user.id,
            username=current_user.username, identity_id=identity.id, action_type="rename",
            before_state={"display_name": old_name}, after_state={"display_name": name},
            action_details={"identity_id": str(identity.id), "source": "known-faces"},
            ip_address=request.client.host if request.client else None,
            user_agent=(request.headers.get("user-agent") or "")[:500], success=True,
            created_at=datetime.utcnow()))
        await db.commit()
    return {"success": True, "id": str(identity_id), "display_name": name}

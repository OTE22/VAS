"""
Upload Routes
=============
Person enrollment endpoints.

    POST /api/identities/{identity_id}/images       add a photo to a person
    PUT  /api/identities/{id}/images/{image_id}/primary
    GET  /api/identities/{identity_id}/images       list a person's photos
    POST /api/upload-person                         DEPRECATED legacy wrapper

Every one of them delegates to backend.core.enrollment_service, which owns the
whole workflow (validation -> real landmarks -> embedding -> transactional
persistence -> atomic file placement -> commit -> index sync). These routes only
translate HTTP to that service and back, so there is exactly one enrollment
implementation to reason about.
"""

import logging
import os
import sys

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

# Add parent directory to path
parent_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if parent_dir not in sys.path:
    sys.path.insert(0, parent_dir)

from backend.auth.auth_service import require_role
from backend.core.enrollment_service import (EnrollmentError, EnrollmentResult,
                                             build_candidate_rows,
                                             classify_match,
                                             create_pending_enrollment,
                                             enroll_image, find_checksum_owner,
                                             find_similar_identities,
                                             prepare_upload,
                                             resolve_identity_by_name,
                                             serialize_image, set_primary_image,
                                             sweep_expired_pending,
                                             validate_person_name)
from config import settings
from db_connection import get_db
from db_models import IdentityImage, User

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Upload & Enrollment"])


def require_upload_csrf(request: Request):
    """Cookie-authenticated mutations need X-Requested-With; bearer clients are
    exempt (same policy as the other admin surfaces)."""
    if request.headers.get("authorization"):
        return
    if request.headers.get("x-requested-with", "").lower() != "xmlhttprequest":
        raise HTTPException(status_code=403,
                            detail="CSRF check failed: X-Requested-With header required")


def _error_response(exc: EnrollmentError) -> JSONResponse:
    """The one place a refusal becomes HTTP.

    Carries a machine-readable code and a safe message. Raw exception text,
    stack traces, absolute paths and vectors never reach the client — they are
    logged server-side only.
    """
    content = {"success": False, "error": exc.code, "message": exc.message,
               "details": exc.details}
    content.update(exc.extra)
    return JSONResponse(status_code=exc.status_code, content=content)


def _success_payload(result: EnrollmentResult, *, message: str) -> dict:
    payload = {
        "success": True,
        "identity_id": result.identity_id,
        "image_id": result.image_id,
        "identity_created": result.identity_created,
        "image_created": result.image_created,
        "embedding_created": result.embedding_created,
        "is_primary": result.is_primary,
        "message": message,
    }
    if result.duplicate:
        payload["duplicate"] = True
    if result.warnings:
        payload["warning"] = result.warnings[0]
        payload["warnings"] = result.warnings
    return payload


async def _read_upload(photo: UploadFile) -> bytes:
    if not photo or not photo.filename:
        raise EnrollmentError("no_file", "Please select a photo to upload.")
    return await photo.read()


# ---------------------------------------------------------------------------
# The enrollment decision — is this somebody we already have?
# ---------------------------------------------------------------------------

# HTTP 202, not 4xx. A review prompt is the workflow working, not a failure:
# routed through _error_response it would be counted as a client error by every
# log scan, dashboard and alert rule watching this endpoint, and an operator
# would see a steady stream of "errors" from a feature behaving exactly as
# designed. 202 Accepted says what happened — the upload was accepted and is
# awaiting a decision — while `success: false` keeps any client that has not
# learned this flow from reporting an enrollment that did not happen.
HTTP_DECISION_REQUIRED = 202


async def _decision_payload(db, *, prepared, contents, display_name,
                            is_face_image, photo, actor_id) -> dict:
    """Look for the person this upload might already be; park it if we find one.

    Returns None when nothing matched — the caller then enrolls immediately,
    exactly as it did before this gate existed.
    """
    # Exact bytes first. "You already have this precise file" is a fact; a
    # similarity score is an estimate, and the fact should not be discovered
    # second or reported as though it were a guess.
    checksum_owner = await find_checksum_owner(db, prepared.checksum)

    ranked = await find_similar_identities(db, prepared.embedding_normalized)
    top_similarity = ranked[0][1] if ranked else None
    band = classify_match(top_similarity)

    if checksum_owner is not None and band == "none":
        # The identical file is already on file but the face did not clear the
        # candidate floor — possible when that identity's own embeddings have
        # since changed. Still worth a decision: the operator is about to store
        # a second copy of a photo we hold.
        band = "uncertain"
        if not any(str(i) == str(checksum_owner) for i, _ in ranked):
            ranked = list(ranked) + [(str(checksum_owner), float(top_similarity or 0.0))]

    if band == "none":
        return None

    candidates = await build_candidate_rows(db, ranked)
    if not candidates:
        # Every candidate vanished between the search and the hydrate. Nothing
        # to review, so do not park an upload the operator cannot resolve.
        return None

    raw_token, row = await create_pending_enrollment(
        db,
        prepared=prepared,
        image_bytes=contents,
        display_name=display_name,
        is_face_image=is_face_image,
        original_filename=photo.filename,
        content_type=photo.content_type,
        user_id=actor_id,
        decision=band,
        candidates=candidates,
        top_similarity=top_similarity,
    )

    from backend.utils.time_utils import iso_utc

    return {
        "success": False,
        "decision_required": True,
        "recommended_action": "add_to_existing" if band == "strong" else "review",
        "match_confidence": band,
        "candidate_identities": candidates,
        "upload_token": raw_token,
        "expires_at": iso_utc(row.expires_at),
        "duplicate_of_identity_id": (str(checksum_owner)
                                     if checksum_owner is not None else None),
        "display_name": display_name,
        "error": None,
        "message": (
            f"This photo looks like {candidates[0]['display_name']}, who is "
            "already on file. Add it to that person, or create a new person if "
            "they are someone different."
            if band == "strong" else
            "This photo may match someone already on file. Please choose who "
            "this is, or create a new person."),
    }


# ---------------------------------------------------------------------------
# Add an image to an EXISTING person — the explicit, unambiguous path
# ---------------------------------------------------------------------------

@router.post("/api/identities/{identity_id}/images", tags=["Identity Management"],
             summary="Add another photo to an existing person")
async def add_identity_image(
    identity_id: str,
    request: Request,
    photo: UploadFile = File(...),
    is_face_image: bool = Form(False),
    current_user: User = Depends(require_role(["admin"])),
    _csrf: None = Depends(require_upload_csrf),
    db: AsyncSession = Depends(get_db),
):
    """Adds one more photo (and one more embedding) to a person that exists.

    Never creates an identity: the caller has already chosen who this is.
    """
    # Read the actor BEFORE calling the service. A failed enrollment rolls the
    # session back, which expires every ORM object attached to it — touching
    # current_user.username afterwards would trigger a lazy refresh inside the
    # async context and raise greenlet_spawn, turning a clean 409 into a 500.
    actor_id, actor_name = current_user.id, current_user.username
    try:
        contents = await _read_upload(photo)
        result = await enroll_image(
            db,
            image_bytes=contents,
            original_filename=photo.filename,
            content_type=photo.content_type,
            is_face_image=is_face_image,
            identity_id=identity_id,
            actor_user_id=actor_id,
            allow_create=False,
        )
    except EnrollmentError as exc:
        logger.warning("[UPLOAD] add-image refused (%s) for identity=%s by %s",
                       exc.code, identity_id, actor_name)
        return _error_response(exc)

    message = ("This image is already registered for the selected person."
               if result.duplicate else "Image added successfully.")
    return JSONResponse(status_code=200 if result.duplicate else 201,
                        content=_success_payload(result, message=message))


@router.get("/api/identities/{identity_id}/images", tags=["Identity Management"],
            summary="List a person's photos")
async def list_identity_images(
    identity_id: str,
    current_user: User = Depends(require_role(["admin"])),
    db: AsyncSession = Depends(get_db),
):
    """All enrollment images of an identity, primary first. A well-formed but unknown UUID returns total: 0 rather than 404; a malformed one is 400 invalid_identity_id."""
    try:
        import uuid as uuid_module
        parsed = uuid_module.UUID(str(identity_id))
    except (ValueError, TypeError):
        return _error_response(EnrollmentError(
            "invalid_identity_id", "Invalid person identifier.", status_code=400))

    rows = (await db.execute(
        select(IdentityImage)
        .where(IdentityImage.identity_id == parsed)
        .order_by(IdentityImage.is_primary.desc(), IdentityImage.created_at.asc())
    )).scalars().all()
    return {"identity_id": str(parsed), "total": len(rows),
            "images": [serialize_image(row) for row in rows]}


@router.put("/api/identities/{identity_id}/images/{image_id}/primary",
            tags=["Identity Management"], summary="Choose the primary photo")
async def make_image_primary(
    identity_id: str,
    image_id: int,
    request: Request,
    current_user: User = Depends(require_role(["admin"])),
    _csrf: None = Depends(require_upload_csrf),
    db: AsyncSession = Depends(get_db),
):
    """An administrator decides which photo represents this person.

    Uploading a newer photo never does this on its own.
    """
    # Captured before the call: a rollback inside set_primary_image expires
    # current_user, and reading it afterwards would raise greenlet_spawn.
    actor_id, actor_name = current_user.id, current_user.username
    try:
        outcome = await set_primary_image(db, identity_id, image_id,
                                          actor_user_id=actor_id)
    except EnrollmentError as exc:
        return _error_response(exc)

    logger.info("[UPLOAD] %s set image %s primary for identity %s",
                actor_name, image_id, identity_id)
    return {"success": True, **outcome, "message": "Primary photo updated."}


# ---------------------------------------------------------------------------
# Legacy endpoint — kept working, routed through the same service
# ---------------------------------------------------------------------------

@router.post("/api/upload-person", tags=["Identity Management"], deprecated=True,
             summary="DEPRECATED — use POST /api/identities/{identity_id}/images")
async def upload_person(
    request: Request,
    # Form("") rather than Form(...): a missing or blank name is a name
    # problem, and validate_person_name below already answers it as
    # 400 {"error": "invalid_name", "message": "Person name is required."}.
    # Letting FastAPI reject it first produced a 422 whose {"detail": [...]}
    # envelope carries neither `error` nor `message`, so the upload modal —
    # which renders those two fields — could only show the status number.
    person_name: str = Form(""),
    photo: UploadFile = File(...),
    is_face_image: bool = Form(False),
    current_user: User = Depends(require_role(["admin"])),
    _csrf: None = Depends(require_upload_csrf),
    db: AsyncSession = Depends(get_db),
):
    """Enroll a person by NAME, creating them when they do not exist yet.

    DEPRECATED: name lookup is inherently ambiguous. Prefer
    POST /api/identities/{identity_id}/images, which names the person by id.
    When several active people share a normalized name this returns 409 with
    the candidates rather than guessing.

    This route was previously CSRF-exempt so as not to break the shipped upload
    modal, which posted cookies with no X-Requested-With header. That left the
    ONE endpoint able to mint an identity as the one endpoint a cross-site form
    could drive: an authenticated POST with no custom header is exactly what an
    attacker's page can send. The modal now sends the header
    (frontend/js/upload-modal.js), so the exemption is gone. Bearer-token
    clients are unaffected — require_upload_csrf exempts them.
    """
    # See add_identity_image: capture the actor before the service call, because
    # a rollback inside it expires current_user and a later attribute read would
    # 500 instead of returning the real refusal.
    actor_id, actor_name = current_user.id, current_user.username
    logger.info("[UPLOAD] request from '%s' (id=%s) for person '%s'",
                actor_name, actor_id, person_name)
    try:
        contents = await _read_upload(photo)

        # Ordering below is load-bearing.
        #
        # prepare_upload FIRST, so every cheap refusal (empty, oversized, wrong
        # type, undecodable, no face, several faces) happens before anything is
        # written anywhere — a rejected photo must leave storage/pending and
        # storage/faces/.incoming exactly as it found them.
        prepared = prepare_upload(contents,
                                  original_filename=photo.filename,
                                  content_type=photo.content_type,
                                  is_face_image=is_face_image)

        # Name resolution SECOND, and the gate only when it finds nobody. An
        # existing name is already an unambiguous answer to "who is this?", so
        # asking again would be noise — and an ambiguous one still raises 409
        # ambiguous_identity here, before any similarity is computed.
        display_name = validate_person_name(person_name)
        existing = await resolve_identity_by_name(db, display_name)
        if existing is None:
            await sweep_expired_pending(db)
            decision = await _decision_payload(
                db, prepared=prepared, contents=contents,
                display_name=display_name, is_face_image=is_face_image,
                photo=photo, actor_id=actor_id)
            if decision is not None:
                logger.info("[UPLOAD] decision required for '%s' by %s "
                            "(%d candidate(s), band=%s)", display_name, actor_name,
                            len(decision["candidate_identities"]),
                            decision["match_confidence"])
                return JSONResponse(status_code=HTTP_DECISION_REQUIRED,
                                    content=decision)

        result = await enroll_image(
            db,
            image_bytes=contents,
            original_filename=photo.filename,
            content_type=photo.content_type,
            is_face_image=is_face_image,
            person_name=person_name,
            actor_user_id=actor_id,
            allow_create=True,
            prepared=prepared,
        )
    except EnrollmentError as exc:
        logger.warning("[UPLOAD] refused (%s) for '%s' by %s",
                       exc.code, person_name, actor_name)
        return _error_response(exc)

    if result.identity_created:
        from backend.utils.identity_audit import log_person_created
        await log_person_created(db, request, actor_id, actor_name,
                                 identity_id=result.identity_id,
                                 display_name=result.display_name,
                                 image_id=result.image_id,
                                 source="upload-person")

    # Legacy response fields the shipped frontend reads (message/filename/
    # total_faces/total_identities/backend) are preserved alongside the new ones.
    total_identities, total_faces = await _totals(db)
    if result.duplicate:
        message = "This image is already registered for the selected person."
    elif result.identity_created:
        message = f"Successfully added {result.display_name} to tracking database."
    else:
        message = f"Image added to {result.display_name}."

    payload = _success_payload(result, message=message)
    payload.update({
        "filename": result.filename,
        "total_identities": total_identities,
        "total_faces": total_faces,
        "backend": settings.VECTOR_BACKEND,
    })
    return JSONResponse(status_code=200, content=payload)


async def _totals(db: AsyncSession):
    """Counts for the legacy response. Never fails the request."""
    from sqlalchemy import func as sa_func

    from db_models import Identity, IdentityStatus, IdentityType

    try:
        total_identities = (await db.execute(
            select(sa_func.count(Identity.id)).where(
                Identity.type == IdentityType.KNOWN,
                Identity.status == IdentityStatus.ACTIVE))).scalar() or 0
    except Exception:
        total_identities = 0

    # Count the authoritative store. This used to read the legacy name-keyed
    # FAISS index, which pgvector deployments no longer write — it would now
    # always report 0 and misinform the upload dialog.
    total_faces = 0
    try:
        from db_models import IdentityEmbedding
        total_faces = (await db.execute(
            select(sa_func.count(IdentityEmbedding.id)))).scalar() or 0
    except Exception:
        total_faces = 0
    return int(total_identities), int(total_faces)

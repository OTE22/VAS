"""
Enrollment Review Routes
========================
The second phase of a name-based upload.

    POST /api/enrollment/confirm     add_to_existing | create_new | cancel
    POST /api/enrollment/cancel      discard a parked upload

POST /api/upload-person parks an upload here when it looks like somebody the
system already has, instead of silently minting a second identity UUID for the
same face. These routes resolve that decision.

WHY THIS IS ITS OWN MODULE. backend/routes/upload.py owns exactly two calls to
`enroll_image`, and tests/test_identity_multi_image_enrollment.py asserts that
count is exactly two — the assertion that has kept a third enrollment
implementation from growing there. The confirmation path genuinely needs a
third call site, so it lives here and carries its own equivalent scan (see
tests/test_enrollment_decision_gate.py) rather than relaxing the invariant. The
HTTP helpers are imported from upload.py so the two modules cannot answer the
same refusal in two different shapes.

WHAT CONFIRMATION RE-CHECKS. A claim ticket is not a promise about the future.
Between review and confirmation an identity can be deleted, deactivated,
merged into another, or become invisible to the administrator who is
answering — and the recognition weights can be swapped underneath both. Every
one of those is re-verified here against live state, never against the frozen
copy in the ticket, and the frozen copy is used for exactly one thing: proving
the chosen identity was actually offered.
"""

import logging
import os
import sys
from typing import Optional

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

# Add parent directory to path
parent_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if parent_dir not in sys.path:
    sys.path.insert(0, parent_dir)

from backend.auth.auth_service import require_role
from backend.core.enrollment_service import (EnrollmentError, claim_pending_enrollment,
                                             enroll_image, get_active_identity,
                                             peek_pending_enrollment,
                                             pending_absolute_path,
                                             prepare_upload, sha256_of,
                                             sweep_expired_pending,
                                             validate_person_name,
                                             _safe_unlink_pending)
from backend.routes.upload import (_error_response, _success_payload, _totals,
                                   require_upload_csrf)
from config import settings
from db_connection import get_db
from db_models import IdentityStatus, User

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Enrollment Review"])

# Statuses an administrator may be shown as a candidate, and may therefore
# choose. Sourced from the vector index's own contract so the set that can be
# OFFERED and the set that can be ACCEPTED cannot drift apart — offering a
# PROMOTED identity and then refusing it is a dead end with no way out.
_SELECTABLE_STATUSES = None


def _selectable_statuses():
    global _SELECTABLE_STATUSES
    if _SELECTABLE_STATUSES is None:
        from backend.core.vector_index.base import SEARCHABLE_STATUSES
        _SELECTABLE_STATUSES = tuple(IdentityStatus[name]
                                     for name in SEARCHABLE_STATUSES)
    return _SELECTABLE_STATUSES


# Refusals the administrator can act on from the dialog they are already
# looking at. These leave the parked upload alone; anything else means the
# ticket is gone and its file has no owner.
_RECOVERABLE_CODES = frozenset({
    "invalid_action",              # nothing was chosen
    "invalid_request",             # add_to_existing without an identity
    "invalid_name",                # create_new with an unusable name
    "identity_not_offered",        # a person who was not among the candidates
    "identity_not_found",          # the chosen person vanished; pick another
    "identity_not_active",         # ...or was deactivated or merged
    "identity_forbidden",          # authorization was withdrawn
    "create_new_needs_confirmation",   # answer again to proceed
    "pending_upload_not_found",    # already consumed — its file is not ours
})


def _cancelled_payload() -> dict:
    return {
        "success": True,
        "cancelled": True,
        "identity_id": None,
        "identity_created": False,
        "image_created": False,
        "embedding_created": False,
        "message": "Upload cancelled. Nothing was saved.",
    }


class ConfirmRequest(BaseModel):
    """The administrator's answer.

    `display_name` is only read for create_new, and only to let the operator
    correct a typo before the identity is minted; the parked name is used when
    it is omitted.
    """
    action: str = Field(..., description="add_to_existing | create_new | cancel")
    upload_token: str = Field(..., description="The token returned by the review response")
    identity_id: Optional[str] = Field(None, description="Required for add_to_existing")
    display_name: Optional[str] = Field(None, description="Optional override for create_new")
    # A strong match means the server is confident this is somebody we already
    # have. Creating a separate identity anyway is legitimate — identical twins
    # and siblings are real — but it is the action that reintroduces exactly the
    # duplicate this whole flow exists to prevent, so it takes a second, explicit
    # answer rather than a single click.
    confirm_create_new: bool = Field(
        False, description="Required to create a new person when the match was strong")


class CancelRequest(BaseModel):
    upload_token: str


def _read_pending_bytes(row) -> bytes:
    """The parked file, re-verified against the checksum recorded at review.

    A mismatch means the bytes on disk are not the bytes that were reviewed, so
    the candidates in the ticket describe a different photo. Refuse rather than
    enroll something nobody looked at.
    """
    try:
        with open(pending_absolute_path(row.storage_path), "rb") as handle:
            contents = handle.read()
    except OSError as exc:
        logger.error("[ENROLL_REVIEW] parked upload unreadable (id=%s): %s",
                     row.id, exc)
        raise EnrollmentError(
            "pending_upload_missing",
            "The uploaded photo is no longer available. Please upload it again.",
            status_code=410)

    if sha256_of(contents) != row.file_checksum:
        logger.error("[ENROLL_REVIEW] checksum mismatch for pending id=%s", row.id)
        raise EnrollmentError(
            "pending_upload_corrupt",
            "The uploaded photo changed after it was reviewed and was not "
            "enrolled. Please upload it again.",
            status_code=409)
    return contents


def _verify_models_unchanged(row) -> None:
    """Refuse a decision taken under different recognition weights.

    The embedding is recomputed from the same bytes at this point. If the model
    changed since the review, that vector is not the vector the candidates were
    ranked against — the similarity the administrator saw no longer describes
    the comparison being made.
    """
    from backend.core.enrollment_service import (detection_model_version,
                                                 embedding_model_version)

    for label, recorded, current in (
        ("recognition", row.embedding_model_version, embedding_model_version()),
        ("detection", row.detection_model_version, detection_model_version()),
    ):
        if recorded != current:
            logger.warning("[ENROLL_REVIEW] %s model changed %r -> %r since review",
                           label, recorded, current)
            raise EnrollmentError(
                "model_version_changed",
                "The face recognition models were updated while this photo was "
                "awaiting a decision, so the match it was compared against is no "
                "longer valid. Please upload the photo again.",
                status_code=409)


async def _verify_choice_is_live(db: AsyncSession, row, identity_id: str,
                                 current_user: User):
    """Re-check the chosen identity against LIVE state, not the frozen ticket.

    Order matters. Membership in the offered list is checked first because it is
    the cheapest and the most conclusive: an id that was never shown is a
    client-side substitution, and answering it with "not found" would be a
    softer statement than the truth.
    """
    offered = {str(candidate.get("identity_id"))
               for candidate in (row.candidates or [])
               if isinstance(candidate, dict)}
    if str(identity_id) not in offered:
        logger.warning("[ENROLL_REVIEW] identity %s was not among the %d offered "
                       "for pending id=%s", identity_id, len(offered), row.id)
        raise EnrollmentError(
            "identity_not_offered",
            "That person was not among the suggested matches for this photo. "
            "Please upload the photo again to get a fresh comparison.",
            status_code=409)

    # exists + status is ACTIVE or PROMOTED + merged_into_id is null.
    identity = await get_active_identity(db, identity_id,
                                         allowed_statuses=_selectable_statuses())

    # Still authorized. Re-read from the LIVE user row through the one resolver
    # every gate uses, rather than trusting the request that opened the review:
    # an account deactivated or demoted while the dialog was open still carries
    # a valid token until it expires, and this is the write that token would
    # otherwise perform.
    from backend.auth.capabilities import (Capability,
                                           resolve_effective_authorization)

    live_user = await db.get(User, int(current_user.id))
    authorization = (resolve_effective_authorization(live_user)
                     if live_user is not None else None)
    if authorization is None or not authorization.has(Capability.IDENTITY_MANAGE):
        logger.warning("[ENROLL_REVIEW] user %s may no longer manage identities",
                       current_user.id)
        raise EnrollmentError(
            "identity_forbidden",
            "You are no longer permitted to modify that person's record.",
            status_code=403)
    return identity


@router.post("/api/enrollment/confirm", tags=["Identity Management"],
             summary="Resolve a parked upload: add to an existing person, "
                     "create a new one, or cancel")
async def confirm_enrollment(
    request: Request,
    body: ConfirmRequest,
    current_user: User = Depends(require_role(["admin"])),
    _csrf: None = Depends(require_upload_csrf),
    db: AsyncSession = Depends(get_db),
):
    """Apply an administrator's decision to an upload awaiting review.

    VALIDATE, THEN CONSUME. Every check runs against a ticket that is only
    READ, so a refusal the administrator can act on — the wrong person picked,
    a strong match still needing a second confirmation — leaves the dialog
    answerable instead of costing a re-upload. The ticket is consumed by a
    single delete-returning immediately before enrollment, which no concurrent
    request can also match, so exactly-once still holds.

    A failure inside the enrollment itself does burn the ticket. That is
    deliberate: a ticket surviving a partial failure could approve two
    enrollments from one review, and `enroll_image` rolls back cleanly, so the
    administrator can simply upload again.
    """
    # Captured before any service call: a rollback expires every ORM object on
    # this session, and reading current_user afterwards would raise
    # greenlet_spawn and turn a clean refusal into a 500 (see upload.py).
    actor_id, actor_name = current_user.id, current_user.username
    action = (body.action or "").strip().lower()
    storage_path = None

    await sweep_expired_pending(db)

    try:
        if action not in ("add_to_existing", "create_new", "cancel"):
            raise EnrollmentError(
                "invalid_action",
                "Choose whether to add this photo to an existing person, create "
                "a new person, or cancel.")

        # ---- cancel: consume immediately, there is nothing to validate ----
        if action == "cancel":
            row = await claim_pending_enrollment(db, body.upload_token, actor_id)
            _safe_unlink_pending(row.storage_path)
            logger.info("[ENROLL_REVIEW] %s cancelled pending id=%s", actor_name, row.id)
            return JSONResponse(status_code=200, content=_cancelled_payload())

        # ---- read-only validation ------------------------------------------
        row = await peek_pending_enrollment(db, body.upload_token, actor_id)
        storage_path = row.storage_path

        _verify_models_unchanged(row)
        contents = _read_pending_bytes(row)

        if action == "add_to_existing":
            if not body.identity_id:
                raise EnrollmentError("invalid_request",
                                      "Choose which person to add this photo to.")
            identity = await _verify_choice_is_live(db, row, body.identity_id,
                                                    current_user)
            target_kwargs = {"identity_id": str(identity.id), "allow_create": False,
                             "allowed_statuses": _selectable_statuses()}
            display_for_log = identity.display_name
        else:
            # create_new. A strong match needs a second, explicit answer.
            if row.decision == "strong" and not body.confirm_create_new:
                raise EnrollmentError(
                    "create_new_needs_confirmation",
                    "This photo closely matches a person already on file. "
                    "Creating a separate record will store the same face twice "
                    "and recognition may then report either name. Confirm again "
                    "if they are genuinely different people.",
                    status_code=409,
                    extra={"requires_confirmation": True,
                           "confirm_field": "confirm_create_new",
                           "top_similarity": row.top_similarity,
                           "candidate_identities": row.candidates or []})
            name = validate_person_name(body.display_name or row.display_name)
            target_kwargs = {"person_name": name, "allow_create": True}
            display_for_log = name

        # Copy what enrollment needs into plain values BEFORE consuming the
        # ticket. The claim deletes the row and commits; reading attributes off
        # a deleted instance afterwards is a lazy refresh, which inside an async
        # request raises greenlet_spawn rather than returning anything useful.
        upload_filename = row.original_filename
        upload_content_type = row.content_type
        upload_is_face_image = bool(row.is_face_image)

        # ---- consume, then enroll -------------------------------------------
        # Everything above is a read. This is the point of no return, and it is
        # the last thing that can be lost to a concurrent confirmation.
        await claim_pending_enrollment(db, body.upload_token, actor_id)

        # Same bytes, same is_face_image, so detection takes the same branch it
        # took at review; re-running it here keeps enrollment's ordering,
        # rollback and atomic file placement exactly as they are.
        prepared = prepare_upload(contents,
                                  original_filename=upload_filename,
                                  content_type=upload_content_type,
                                  is_face_image=upload_is_face_image)
        result = await enroll_image(
            db,
            image_bytes=contents,
            original_filename=upload_filename,
            content_type=upload_content_type,
            is_face_image=upload_is_face_image,
            actor_user_id=actor_id,
            prepared=prepared,
            **target_kwargs,
        )
    except EnrollmentError as exc:
        logger.warning("[ENROLL_REVIEW] %s refused (%s) for %s",
                       action or "confirm", exc.code, actor_name)
        # A ticket that survived validation keeps its file; one that was
        # consumed and then failed to enroll has no owner left, so the file
        # goes with it.
        if exc.code not in _RECOVERABLE_CODES:
            _safe_unlink_pending(storage_path)
        return _error_response(exc)

    # Enrolled. The parked copy has served its purpose.
    _safe_unlink_pending(storage_path)

    if result.identity_created:
        from backend.utils.identity_audit import log_person_created
        await log_person_created(db, request, actor_id, actor_name,
                                 identity_id=result.identity_id,
                                 display_name=result.display_name,
                                 image_id=result.image_id,
                                 source="enrollment-review")

    if result.duplicate:
        message = "This image is already registered for the selected person."
    elif result.identity_created:
        message = f"Successfully added {result.display_name} to tracking database."
    else:
        message = f"Image added to {result.display_name}."

    total_identities, total_faces = await _totals(db)
    payload = _success_payload(result, message=message)
    payload.update({
        "filename": result.filename,
        "total_identities": total_identities,
        "total_faces": total_faces,
        "backend": settings.VECTOR_BACKEND,
        "decision_applied": action,
    })
    logger.info("[ENROLL_REVIEW] %s applied %s -> identity=%s (created=%s) for '%s'",
                actor_name, action, result.identity_id, result.identity_created,
                display_for_log)
    return JSONResponse(status_code=200, content=payload)


@router.post("/api/enrollment/cancel", tags=["Identity Management"],
             summary="Discard an upload awaiting a decision")
async def cancel_enrollment(
    request: Request,
    body: CancelRequest,
    current_user: User = Depends(require_role(["admin"])),
    _csrf: None = Depends(require_upload_csrf),
    db: AsyncSession = Depends(get_db),
):
    """Drop a parked upload and its file. Nothing was ever created for it.

    Same effect as confirm with action=cancel; this exists so a client closing
    the dialog can say what it means without constructing a decision payload.
    """
    actor_id, actor_name = current_user.id, current_user.username
    await sweep_expired_pending(db)
    try:
        row = await claim_pending_enrollment(db, body.upload_token, actor_id)
    except EnrollmentError as exc:
        return _error_response(exc)

    _safe_unlink_pending(row.storage_path)
    logger.info("[ENROLL_REVIEW] %s cancelled pending id=%s", actor_name, row.id)
    return _cancelled_payload()

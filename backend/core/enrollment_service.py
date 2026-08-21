"""
Person enrollment — the single implementation behind every upload route.

    One Identity -> many IdentityImage -> one IdentityEmbedding per usable image

Ordering matters and is the point of this module. Everything that can fail
cheaply (decode, face detection, landmarks, embedding) runs BEFORE any durable
side effect, the photo lands in a temporary file until the database row it
belongs to is about to commit, and the final move happens last:

    validate request
    -> temp file + SHA-256
    -> decode + dimension checks
    -> REAL face + landmark detection      (never fabricated)
    -> embedding + embedding validation
    -> resolve/create identity (transactional)
    -> duplicate-checksum check
    -> IdentityImage row
    -> embedding row linked to identity + image   (deferred: no commit, no index)
    -> atomic move temp -> FACES_DIR/<identity_uuid>/
    -> commit                                     <- the single commit point
    -> vector-index sync    (best effort; a failure is a warning, not an error)

Everything above the commit is ONE transaction. That is why `save_embedding` is
called with `defer_commit=True`: its FAISS branch otherwise commits the vector
itself, which would split enrollment in two and leave the rollback below with
nothing to undo — a committed image row pointing at a file that never landed.
The deferred call also withholds the index write, because indexing a row that a
rollback then erases strands a phantom entry under a reused key.

Failure at any step rolls the transaction back and removes both the temp file
and anything already moved, so the invariants hold: no image row without a
file, no embedding without an image, no new identity without a valid first
embedding, and never success=True for an enrollment that is not usable.

Storage identity is the immutable UUID, never the display name: renaming a
person must not move their folder, and two people may share a name.

TWO-PHASE ENROLLMENT (name-based uploads only)
----------------------------------------------
Name lookup is TEXTUAL. On its own that meant a novel spelling always minted a
new UUID: a second photo of an already-enrolled person, uploaded as "Jon Smith"
instead of "John Smith", became a SECOND identity holding a second embedding of
the same face, and recognition then answered with whichever vector scored
higher. Nothing warned anybody — the response was 200 with a success toast.

So a name-based upload is now checked against the people we already have before
any identity is created:

    prepare_upload            decode + REAL face + embedding, no side effects
    -> exact checksum scan    deterministic: is this exact FILE already ours?
    -> vector search          ACTIVE + PROMOTED, collapsed per identity
    -> classify_match         strong / uncertain / none
    -> none      : enroll immediately, exactly as before
    -> otherwise : park the upload, return candidates, wait for a decision

While an upload is parked NOTHING durable exists for it — no identity, no
image row, no embedding row, no gallery folder, no vector-index entry. The
photo sits in PENDING_UPLOAD_DIR under a random name and the claim ticket is a
`pending_enrollments` row; `enroll_image` runs only after an administrator
confirms, and runs unchanged.

This does NOT block anything. Every outcome remains reachable: add the photo to
the matched person, create a new person anyway (a real lookalike or twin), or
cancel. Identities are never merged automatically — `merge_identities` is not
called from this module at all. Different photos of one person under one UUID
is the SUPPORTED case, and stays supported: the exact-file checksum rule below
is scoped to byte-identical uploads and nothing else.
"""

import hashlib
import logging
import os
import re
import secrets
import shutil  # noqa: F401  (used by adopt_existing_file)
import tempfile
import uuid as uuid_module
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np
from sqlalchemy import delete, func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from config import settings
from db_models import (Identity, IdentityEmbedding, IdentityImage,
                       IdentityStatus, IdentityType, PendingEnrollment)

logger = logging.getLogger(__name__)

# ArcFace output width. A vector of any other length is a bug, not a variant.
EXPECTED_EMBEDDING_DIM = 512

# Guard rails on the decoded image (not just the encoded bytes): a small file
# can still decode into a decompression bomb.
MIN_IMAGE_SIDE = 32
MAX_IMAGE_SIDE = 10000
MAX_DECODED_PIXELS = 60_000_000

MAX_IMAGES_PER_IDENTITY = 1000

_EXT_BY_CONTENT_TYPE = {
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/bmp": ".bmp",
}
_ALLOWED_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}


class EnrollmentError(Exception):
    """A refusal with a machine-readable code and a safe, user-facing message.

    `message` is written for an operator and never carries exception text,
    filesystem paths or database detail — those go to the log only.
    """

    def __init__(self, code: str, message: str, status_code: int = 400,
                 details: Optional[str] = None, extra: Optional[Dict[str, Any]] = None):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.status_code = status_code
        self.details = details
        self.extra = extra or {}


@dataclass
class EnrollmentResult:
    """What the caller reports. Contains no absolute path and no vector."""
    identity_id: str
    image_id: Optional[int]
    identity_created: bool
    image_created: bool
    embedding_created: bool
    is_primary: bool
    duplicate: bool = False
    filename: Optional[str] = None          # basename only, never a path
    display_name: Optional[str] = None
    source_type: Optional[str] = None
    storage_path: Optional[str] = None      # relative, e.g. storage/faces/<uuid>/...
    warnings: list = field(default_factory=list)


# ---------------------------------------------------------------------------
# Name handling
# ---------------------------------------------------------------------------

def normalize_display_name(name: Optional[str]) -> str:
    """Trim, collapse internal whitespace. Case is preserved for display."""
    if not name:
        return ""
    return re.sub(r"\s+", " ", name).strip()


def name_lookup_key(name: Optional[str]) -> str:
    """The case-insensitive key used to decide whether two names are the same.

    'Ali Abbass', 'ali abbass' and 'ALI   ABBASS' all map to 'ali abbass'.
    """
    return normalize_display_name(name).casefold()


def validate_person_name(name: Optional[str]) -> str:
    """Return the normalized display name or raise EnrollmentError."""
    normalized = normalize_display_name(name)
    if not normalized:
        raise EnrollmentError("invalid_name", "Person name is required.")
    if len(normalized) < 2:
        raise EnrollmentError(
            "invalid_name",
            "Invalid person name. Name must be at least 2 characters.")
    if len(normalized) > 255:
        raise EnrollmentError("invalid_name", "Person name is too long.")
    return normalized


# ---------------------------------------------------------------------------
# Storage helpers — UUID folders, server-generated filenames, no traversal
# ---------------------------------------------------------------------------

def identity_folder(identity_id) -> str:
    """Absolute directory for an identity's photos: FACES_DIR/<uuid>/.

    The UUID is re-parsed rather than interpolated, so nothing caller-supplied
    can ever become a path component.
    """
    safe_uuid = str(uuid_module.UUID(str(identity_id)))
    return os.path.join(settings.FACES_DIR, safe_uuid)


def _extension_for(filename: Optional[str], content_type: Optional[str]) -> str:
    """Server-chosen extension. The uploaded name is a hint, never a path."""
    if filename:
        ext = os.path.splitext(filename)[1].lower()
        if ext in _ALLOWED_EXTS:
            return ".jpg" if ext == ".jpeg" else ext
    if content_type:
        mapped = _EXT_BY_CONTENT_TYPE.get(content_type.split(";")[0].strip().lower())
        if mapped:
            return mapped
    return ".jpg"


def reserve_image_filename(folder: str, extension: str) -> str:
    """Atomically claim image_001.jpg, image_002.jpg, ... and return the name.

    The claim IS the file: O_CREAT|O_EXCL either creates the directory entry or
    fails, so two concurrent uploads into one folder can never be handed the
    same name.

    Listing the folder and picking the first free name — what this did before —
    is a check-then-act with a window in between: two uploads both saw
    image_001.jpg, both chose image_002.jpg, and the second os.replace()
    silently overwrote the first one's bytes, leaving two identity_images rows
    pointing at a single file with one photo lost.

    The caller owns the reservation and must either write over the placeholder
    or unlink it; every failure path below already unlinks final_path.
    """
    for index in range(1, MAX_IMAGES_PER_IDENTITY + 1):
        candidate = f"image_{index:03d}{extension}"
        try:
            handle = os.open(os.path.join(folder, candidate),
                             os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            continue
        os.close(handle)
        return candidate
    raise EnrollmentError(
        "too_many_images",
        f"This person already has the maximum of {MAX_IMAGES_PER_IDENTITY} images.")


def relative_storage_path(absolute_path: str) -> str:
    """'storage/faces/<uuid>/image_001.jpg' — what we persist and serialize.

    Refuses to emit anything that escapes STORAGE_DIR.
    """
    storage_dir = os.path.abspath(settings.STORAGE_DIR)
    target = os.path.abspath(absolute_path)
    if os.path.commonpath([storage_dir, target]) != storage_dir:
        raise EnrollmentError("invalid_storage_path",
                              "Refusing to store a file outside the storage directory.",
                              status_code=500)
    return "storage/" + os.path.relpath(target, storage_dir).replace("\\", "/")


def sha256_of(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------------------
# Image + embedding validation
# ---------------------------------------------------------------------------

def decode_and_validate_image(image_bytes: bytes):
    """Decode to a BGR array and sanity-check its dimensions."""
    import cv2

    try:
        image = cv2.imdecode(np.frombuffer(image_bytes, np.uint8), cv2.IMREAD_COLOR)
    except Exception:
        image = None
    if image is None:
        raise EnrollmentError(
            "invalid_image",
            "Invalid image file. Please upload a valid image (JPG, PNG, WEBP).")

    height, width = image.shape[:2]
    if width < MIN_IMAGE_SIDE or height < MIN_IMAGE_SIDE:
        raise EnrollmentError(
            "image_too_small",
            f"Image is too small. Minimum is {MIN_IMAGE_SIDE}x{MIN_IMAGE_SIDE} pixels.")
    if width > MAX_IMAGE_SIDE or height > MAX_IMAGE_SIDE or (width * height) > MAX_DECODED_PIXELS:
        raise EnrollmentError("image_too_large",
                              "Image resolution is too large to process.")
    return image


def _too_many_faces(count: int) -> "EnrollmentError":
    return EnrollmentError(
        "multiple_faces",
        f"This photo contains {count} faces. Enrollment needs exactly one.",
        details=("An enrollment photo must show a single person, otherwise the "
                 "stored face signature could belong to the wrong one. Please "
                 "crop the photo to the intended person and upload again."))


def detect_face_landmarks(image, is_face_image: bool) -> Tuple[Any, Any]:
    """Run the REAL detector and return (bbox, landmarks) for the ONE face.

    The detection itself now lives in `backend.core.face_extraction`, shared with
    both search endpoints, the quality check and the gallery loader — enrollment
    was the only path that got this right, and keeping the logic here is what
    let the others drift into inventing keypoints and skipping the padded retry.

    Detection is uncapped so a second person cannot hide behind a "return the
    best one" cap: an enrollment photo with two faces is refused, never silently
    attributed to whichever face scored highest.

    `is_face_image` means "already cropped to a face", so detection is retried
    on a padded canvas when the tight crop defeats the detector's minimum
    context. It NEVER means "invent five keypoints" — fabricated eye/nose/mouth
    coordinates silently produced garbage embeddings that still enrolled and
    then polluted every future match.

    The gate stays tied to `is_face_image` rather than always retrying, because
    enrollment's two refusals mean different things to the operator: `no_face`
    tells them to tick "This is a face image", `no_landmarks` tells them that
    box was already ticked and the retry still found nothing.
    """
    from backend.core.face_extraction import (FaceExtractionError,
                                              extract_single_face)

    try:
        face = extract_single_face(image, on_multiple="reject",
                                   allow_padded_retry=is_face_image)
    except FaceExtractionError as exc:
        if exc.code == "multiple_faces":
            raise _too_many_faces(exc.faces_found) from exc
        if is_face_image:
            raise EnrollmentError(
                "no_landmarks",
                "Facial landmarks could not be detected.",
                details=("The image was treated as an already-cropped face, but no "
                         "real facial landmarks could be located. Please upload a "
                         "clearer, front-facing photo.")) from exc
        raise EnrollmentError(
            "no_face",
            "No face detected in the image. Please upload an image with a clear, "
            "visible face, or check 'This is a face image' if you're uploading an "
            "already cropped face image.",
            details=("The image does not contain a detectable face. Please try a "
                     "different image with better lighting and a clear view of the "
                     "person's face.")) from exc

    if face.padded_retry:
        logger.info("[ENROLL] landmarks found on padded retry (cropped-face mode)")
    return face.bbox, face.landmarks


def generate_embedding(image, landmarks) -> np.ndarray:
    """Produce the face embedding and reject anything unusable."""
    from backend.core import model_manager

    try:
        embedding = model_manager.recognizer.get_embedding(image, landmarks)
    except Exception as exc:
        logger.error("[ENROLL] embedding generation raised: %s", exc, exc_info=True)
        raise EnrollmentError(
            "embedding_failed",
            "Failed to generate face embedding. The image quality may be too low.",
            details="Unable to extract face features from the image.")

    return validate_embedding_vector(embedding)


def validate_embedding_vector(embedding) -> np.ndarray:
    """dimension + finiteness + non-zero norm, or refuse."""
    if embedding is None:
        raise EnrollmentError(
            "embedding_failed",
            "Failed to generate face embedding. The image quality may be too low.")

    vector = np.asarray(embedding, dtype=np.float32).reshape(-1)
    if vector.size != EXPECTED_EMBEDDING_DIM:
        logger.error("[ENROLL] embedding has %d dims, expected %d",
                     vector.size, EXPECTED_EMBEDDING_DIM)
        raise EnrollmentError(
            "invalid_embedding",
            "Invalid face embedding generated. The image could not be processed.",
            details="The generated face signature had an unexpected size.")
    if not np.all(np.isfinite(vector)):
        raise EnrollmentError(
            "invalid_embedding",
            "Invalid face embedding generated. The image quality may be insufficient.",
            details="The generated face signature contained invalid values.")
    norm = float(np.linalg.norm(vector))
    if norm <= 0 or not np.isfinite(norm):
        raise EnrollmentError(
            "invalid_embedding",
            "Invalid face embedding generated. The image quality may be insufficient.",
            details="The generated face signature had zero magnitude.")
    return vector


def validate_saved_embedding(record, identity_id, expected_image_id: Optional[int]) -> None:
    """Verify what actually landed in the database before declaring success."""
    if record is None:
        raise EnrollmentError(
            "embedding_save_failed",
            "Failed to save embedding to database. The image quality may be "
            "below the required threshold.",
            status_code=500,
            details="The embedding was generated but could not be saved.")

    if str(getattr(record, "identity_id", None)) != str(identity_id):
        logger.error("[ENROLL] saved embedding belongs to %s, expected %s",
                     getattr(record, "identity_id", None), identity_id)
        raise EnrollmentError(
            "embedding_save_failed",
            "Failed to save embedding to database. Please try again.",
            status_code=500,
            details="The stored face signature was attributed to the wrong person.")

    # Validated on BOTH backends. This used to run only under pgvector, from the
    # era when the FAISS path wrote a faiss_id and no vector at all. Under the
    # current contract PostgreSQL is authoritative on every backend and the FAISS
    # branch writes `embedding` too — so skipping the check there would leave the
    # one backend that most needs it (its index is rebuilt FROM this column)
    # unverified.
    stored = getattr(record, "embedding", None)
    if stored is None:
        raise EnrollmentError(
            "embedding_save_failed",
            "Failed to save embedding to database. Please try again.",
            status_code=500,
            details="The stored face signature was empty.")
    vector = np.asarray(stored, dtype=np.float32).reshape(-1)
    if vector.size != EXPECTED_EMBEDDING_DIM:
        raise EnrollmentError(
            "embedding_save_failed",
            "Failed to save embedding to database. Please try again.",
            status_code=500,
            details="The stored face signature had an unexpected size.")
    norm = float(np.linalg.norm(vector))
    if not np.isfinite(norm) or norm <= 0:
        raise EnrollmentError(
            "embedding_save_failed",
            "Failed to save embedding to database. Please try again.",
            status_code=500,
            details="The stored face signature had zero magnitude.")

    if expected_image_id is not None:
        linked = getattr(record, "image_id", None)
        if linked is not None and int(linked) != int(expected_image_id):
            raise EnrollmentError(
                "embedding_save_failed",
                "Failed to save embedding to database. Please try again.",
                status_code=500,
                details="The stored face signature referenced the wrong image.")


# ---------------------------------------------------------------------------
# Identity resolution
# ---------------------------------------------------------------------------

async def resolve_identity_by_name(db: AsyncSession, display_name: str):
    """Find the single active KNOWN identity matching this name, or refuse.

    Case- and whitespace-insensitive, so 'Ali Abbass' never enrolls twice.
    Ambiguity is reported, never guessed: when several active identities share
    a normalized name the caller must choose one explicitly.
    """
    key = name_lookup_key(display_name)
    normalized_column = func.lower(
        func.regexp_replace(func.btrim(Identity.display_name), r"\s+", " ", "g"))

    result = await db.execute(
        select(Identity).where(
            normalized_column == key,
            Identity.type == IdentityType.KNOWN,
            Identity.status == IdentityStatus.ACTIVE,
        ).order_by(Identity.created_at.asc())
    )
    matches = result.scalars().all()

    if len(matches) > 1:
        raise EnrollmentError(
            "ambiguous_identity",
            "Several people share this name. Select which person this photo "
            "belongs to and upload again.",
            status_code=409,
            extra={"candidates": [
                {"identity_id": str(m.id), "display_name": m.display_name,
                 "created_at": m.created_at.isoformat() if m.created_at else None}
                for m in matches
            ]})
    return matches[0] if matches else None


async def get_active_identity(
    db: AsyncSession,
    identity_id: str,
    *,
    allowed_statuses: Sequence[IdentityStatus] = (IdentityStatus.ACTIVE,),
) -> Identity:
    """Load an explicitly addressed identity, or refuse with 404.

    `allowed_statuses` defaults to ACTIVE only, which is what the direct
    add-image route has always required. The review-confirmation path widens it
    to ACTIVE + PROMOTED, because those two together are the set the vector
    index searches (vector_index/base.SEARCHABLE_STATUSES) and therefore the set
    an administrator can be OFFERED as a candidate. Refusing PROMOTED there
    would present a choice and then reject it — a dead end with no way out
    except renaming the person.

    MERGED and INACTIVE stay refused everywhere: a MERGED record's rows belong
    to its successor, so writing to it would strand the new photo.
    """
    try:
        parsed = uuid_module.UUID(str(identity_id))
    except (ValueError, TypeError, AttributeError):
        raise EnrollmentError("invalid_identity_id", "Invalid person identifier.",
                              status_code=400)

    identity = (await db.execute(
        select(Identity).where(Identity.id == parsed))).scalar_one_or_none()
    if identity is None:
        raise EnrollmentError("identity_not_found", "Person not found.", status_code=404)
    if identity.status not in tuple(allowed_statuses):
        raise EnrollmentError("identity_not_active",
                              "This person's record is no longer active.",
                              status_code=409)
    # A merged record keeps its status for audit but its rows now belong to the
    # successor; adding a photo here would attach it to a person nobody reads.
    if getattr(identity, "merged_into_id", None) is not None:
        raise EnrollmentError("identity_not_active",
                              "This person's record has been merged into another.",
                              status_code=409)
    return identity


# ---------------------------------------------------------------------------
# The enrollment flow
# ---------------------------------------------------------------------------

def _identity_service():
    """The live identity_service singleton, or None when unavailable."""
    try:
        import backend.core
        service = getattr(backend.core, "identity_service", None)
        if service is not None:
            return service
    except Exception as exc:
        logger.debug("[ENROLL] backend.core.identity_service unavailable: %s", exc)
    try:
        from backend.core.identity_service import identity_service
        return identity_service
    except Exception as exc:
        logger.debug("[ENROLL] direct identity_service import failed: %s", exc)
    return None


def _write_temp_file(image_bytes: bytes, extension: str,
                     directory: Optional[str] = None) -> str:
    """Land the upload in a temp file beside FACES_DIR (same filesystem, so the
    final placement can be an atomic rename rather than a copy).

    `directory` overrides the destination for the review flow, which parks
    uploads in PENDING_UPLOAD_DIR — a sibling of faces/, on the same filesystem
    but outside the gallery, since a photo awaiting a decision has no identity
    folder to belong to yet.
    """
    temp_root = directory or settings.UPLOAD_TEMP_DIR
    os.makedirs(temp_root, exist_ok=True)
    handle, temp_path = tempfile.mkstemp(prefix="upload_", suffix=extension, dir=temp_root)
    try:
        with os.fdopen(handle, "wb") as fh:
            fh.write(image_bytes)
    except Exception:
        _safe_unlink(temp_path)
        raise
    return temp_path


def _safe_unlink(path: Optional[str]) -> None:
    if not path:
        return
    try:
        if os.path.exists(path):
            os.remove(path)
    except OSError as exc:
        logger.warning("[ENROLL] could not remove %s: %s", os.path.basename(path), exc)


@dataclass
class PreparedUpload:
    """Everything derivable from the uploaded bytes, with nothing written yet.

    This is the state both phases share. Producing it costs a decode, a
    detection and an embedding and touches no database and no disk, which is
    what lets the review phase rank candidates and then discard the whole thing
    if the administrator cancels.
    """
    checksum: str
    image: Any                       # BGR ndarray; never serialized
    width: int
    height: int
    landmarks: Any
    embedding_normalized: np.ndarray
    extension: str
    # Measured here because this is where the image, the face box and the
    # landmarks all exist at once. RECORDED, never used to refuse: see the
    # note where it is written in enroll_image.
    quality: Optional[float] = None
    quality_scorer_version: Optional[str] = None


def prepare_upload(
    image_bytes: bytes,
    *,
    original_filename: Optional[str] = None,
    content_type: Optional[str] = None,
    is_face_image: bool = False,
) -> PreparedUpload:
    """Validate, decode, detect and embed — every refusal that costs nothing.

    Extracted from `enroll_image` so the review phase runs EXACTLY the same
    checks in the same order, rather than a second implementation that could
    accept a photo enrollment would later reject (or the reverse, which is
    worse: a decision prompt for an image that can never be enrolled).

    No database access, no filesystem access, no side effects of any kind.
    """
    if not image_bytes:
        raise EnrollmentError("empty_file",
                              "The uploaded file is empty. Please select a valid image file.")

    from backend.config import MAX_FILE_SIZE
    if len(image_bytes) > MAX_FILE_SIZE:
        raise EnrollmentError(
            "file_too_large",
            f"File too large. Maximum size is {MAX_FILE_SIZE / 1024 / 1024:.1f}MB.")

    if content_type and not content_type.lower().startswith("image/"):
        raise EnrollmentError(
            "invalid_file_type",
            "Invalid file type. Please upload an image (JPG, PNG, WEBP).")

    checksum = sha256_of(image_bytes)
    image = decode_and_validate_image(image_bytes)
    height, width = image.shape[:2]

    # ---- real detection, real landmarks, real embedding -------------------
    bbox, landmarks = detect_face_landmarks(image, is_face_image)
    embedding = generate_embedding(image, landmarks)

    # Score the face while the image, its box and its landmarks are all in
    # hand. Enrollment used to pass quality_score=None all the way down, so
    # identity_images.quality_score, .quality_scorer_version and
    # identity_embeddings.quality were NULL for every uploaded photo forever —
    # while the API still published a quality_score field to clients. The same
    # scorer the search endpoints use is used here, so the numbers are
    # comparable across camera captures and uploads.
    quality = None
    quality_scorer_version = None
    try:
        from backend.core.face_quality import quality_score_for_detection
        quality, _details, quality_scorer_version = quality_score_for_detection(
            image, bbox, landmarks)
    except Exception as exc:                                   # noqa: BLE001
        # Diagnostics must never be able to refuse an otherwise valid photo.
        logger.warning("[ENROLL] quality scoring failed, continuing: %s", exc)

    # generate_embedding already refuses a zero/non-finite vector, but the
    # division is restated defensively because the result is what gets STORED:
    # a zero-magnitude vector in pgvector returns NaN from every cosine
    # comparison, and PostgreSQL orders NaN above every real number, so it
    # would pass every threshold in every future search.
    norm = float(np.linalg.norm(embedding))
    if norm <= 0 or not np.isfinite(norm):
        raise EnrollmentError(
            "invalid_embedding",
            "Invalid face embedding generated. The image quality may be insufficient.",
            details="The generated face signature had zero magnitude.")
    embedding_normalized = embedding / norm

    return PreparedUpload(
        checksum=checksum,
        image=image,
        width=int(width),
        height=int(height),
        landmarks=landmarks,
        embedding_normalized=embedding_normalized,
        extension=_extension_for(original_filename, content_type),
        quality=quality,
        quality_scorer_version=quality_scorer_version,
    )


# ---------------------------------------------------------------------------
# Matching — who might this already be?
# ---------------------------------------------------------------------------

def _model_stamp(path: Optional[str]) -> Optional[str]:
    """'.../w600k_r50.onnx' -> 'w600k_r50', or None when unknown.

    Same rule as IdentityService._resolve_model_version, which stamps every
    stored vector: derived from the configured weights file so swapping the
    model changes the stamp without anyone remembering to bump a constant.
    """
    if not path:
        return None
    name = os.path.splitext(os.path.basename(str(path)))[0]
    return name[:64] or None


def embedding_model_version() -> Optional[str]:
    """Which recognition weights produce our vectors, right now."""
    return _model_stamp(settings.RECOGNITION_MODEL)


def detection_model_version() -> Optional[str]:
    """Which detector locates our faces, right now."""
    return _model_stamp(settings.DETECTION_MODEL)


async def find_checksum_owner(db: AsyncSession, checksum: str,
                              *, exclude_identity_id=None):
    """The searchable identity that already holds this EXACT file, if any.

    Deterministic evidence, and deliberately consulted BEFORE any similarity
    number: "this is byte-for-byte a photo you already have" is a fact, while a
    cosine score is an estimate. An administrator shown both should see the fact
    first.

    Scope is exact bytes and nothing else. A different photo of the same person
    is NOT a duplicate — that is the supported way to give one identity several
    images — so this can only ever fire on a re-upload of the identical file.
    """
    from backend.core.vector_index.base import SEARCHABLE_STATUSES

    statuses = tuple(IdentityStatus[name] for name in SEARCHABLE_STATUSES)
    query = (select(IdentityImage.identity_id)
             .join(Identity, Identity.id == IdentityImage.identity_id)
             .where(IdentityImage.file_checksum == checksum,
                    Identity.status.in_(statuses),
                    Identity.merged_into_id.is_(None)))
    if exclude_identity_id is not None:
        query = query.where(IdentityImage.identity_id != exclude_identity_id)
    return (await db.execute(query.limit(1))).scalar_one_or_none()


# ---------------------------------------------------------------------------
# person_code — the organisation's own identifier for a person
# ---------------------------------------------------------------------------

PERSON_CODE_MAX_LENGTH = 100

# Letters, digits, and the three separators a badge or employee number actually
# uses. Deliberately narrow: the code is displayed in the UI, exported, and used
# as a lookup key, so anything that could be mistaken for markup, a path
# fragment or a CSV delimiter is refused at the boundary rather than escaped
# everywhere it is later rendered.
PERSON_CODE_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


class PersonCodeError(ValueError):
    """A person_code that cannot be stored, with a reason fit to show a user."""


def normalize_person_code(value):
    """(display_form, uniqueness_key) or (None, None) when absent.

    Whitespace-only is absence, not a value: an operator who tabs through the
    field must not create an identity whose code is " ". The key is uppercased
    because badge numbers are read aloud and typed inconsistently — emp-001 and
    EMP-001 are the same code, and letting both exist would make the code
    useless as an identifier, which is its only purpose.
    """
    if value is None:
        return None, None
    display = str(value).strip()
    if not display:
        return None, None
    if len(display) > PERSON_CODE_MAX_LENGTH:
        raise PersonCodeError(
            f"Person code must be at most {PERSON_CODE_MAX_LENGTH} characters.")
    if not PERSON_CODE_PATTERN.match(display):
        raise PersonCodeError(
            "Person code may contain only letters, digits, dots, hyphens and "
            "underscores, and must start with a letter or digit.")
    return display, display.upper()


async def find_similar_identities(
    db: AsyncSession,
    embedding: np.ndarray,
    *,
    exclude_identity_ids: Sequence[Any] = (),
) -> List[Tuple[str, float]]:
    """(identity_id, best similarity) for the people this face could already be.

    Ordered best first, at most ENROLL_MAX_CANDIDATES long.

    Uses vector_index.access.search_similar_embeddings because it is the only
    search entry point that answers on BOTH backends — identity_service's
    wrapper returns [] under pgvector, and the pgvector index's own
    search_known does not exist under faiss — and because it routes its status
    predicate through the shared SEARCHABLE_STATUS_SQL, so enrollment searches
    exactly the ACTIVE + PROMOTED set that recognition does. PROMOTED
    identities are type=KNOWN, so `identity_type="known"` keeps raw unknowns out
    without a second filter.

    It returns one row per EMBEDDING, so a pool much larger than the display
    count is retrieved and then collapsed per identity: a person with eight
    photos would otherwise occupy every slot and hide everyone else. The guard
    at startup enforces pool > displayed for the same reason.
    """
    from backend.core.vector_index.access import search_similar_embeddings

    floor = float(settings.ENROLL_CANDIDATE_MIN)
    pool = int(settings.ENROLL_CANDIDATE_POOL)
    limit = int(settings.ENROLL_MAX_CANDIDATES)

    hits = await search_similar_embeddings(db, embedding, top_k=pool,
                                           threshold=floor, identity_type="known")

    # Excluded BEFORE collapsing, so an excluded identity cannot consume a slot.
    excluded = {str(value) for value in exclude_identity_ids}
    best: Dict[str, float] = {}
    for hit in hits:
        identity_id = str(hit.get("identity_id"))
        if not identity_id or identity_id in excluded:
            continue
        score = float(hit.get("similarity") or 0.0)
        if score > best.get(identity_id, float("-inf")):
            best[identity_id] = score

    ranked = sorted(best.items(), key=lambda item: item[1], reverse=True)
    return ranked[:limit]


def classify_match(top_similarity: Optional[float]) -> str:
    """'strong' | 'uncertain' | 'none' for the best similarity found.

    The thresholds are NOT clamped to each other here. An inverted pair is
    refused at startup by backend/security/config_guard.py, because a read-time
    max() would let a deployment run for months with an empty 'uncertain' band
    — every match reported as confident — and nothing anywhere would say so.
    """
    if top_similarity is None:
        return "none"
    strong_min = float(settings.ENROLL_STRONG_MATCH_MIN)
    candidate_min = float(settings.ENROLL_CANDIDATE_MIN)
    score = float(top_similarity)
    if score >= strong_min:
        return "strong"
    if score >= candidate_min:
        return "uncertain"
    return "none"


async def build_candidate_rows(db: AsyncSession,
                               ranked: Sequence[Tuple[str, float]]) -> List[Dict[str, Any]]:
    """Render ranked (identity_id, score) pairs for an administrator to choose from.

    Delegates to AdvancedSearchService._build_match — a pure function of a
    hydrated Identity — so the card shown during enrollment review and the card
    shown by search are produced by the same code and cannot drift in what they
    call a confident match.
    """
    from backend.core.advanced_search import advanced_search_service
    from backend.utils.time_utils import iso_utc

    if not ranked:
        return []

    parsed = []
    for identity_id, _score in ranked:
        try:
            parsed.append(uuid_module.UUID(str(identity_id)))
        except (ValueError, TypeError):
            continue
    if not parsed:
        return []

    rows = {str(row.id): row for row in (await db.execute(
        select(Identity).where(Identity.id.in_(parsed)))).scalars().all()}

    out: List[Dict[str, Any]] = []
    for identity_id, score in ranked:
        identity = rows.get(str(identity_id))
        if identity is None:
            # Deleted between the search and this hydrate. Skip it: offering a
            # name we cannot verify is worse than offering one candidate fewer.
            continue
        match = advanced_search_service._build_match(
            identity, str(identity_id), float(score), "known")
        out.append({
            "identity_id": match.identity_id,
            "display_name": match.display_name,
            "similarity": match.similarity,
            "preview_image": match.snapshot_url,
            "confidence_band": match.confidence_band,
            "last_seen_at": iso_utc(match.last_seen_at),
            "appearances_count": match.appearances_count,
        })
    return out


# ---------------------------------------------------------------------------
# Pending decisions — the claim ticket between the two phases
# ---------------------------------------------------------------------------

# A decision is a human action taken while the operator is still looking at the
# upload dialog. Fifteen minutes is generous for that and short enough that an
# abandoned upload does not linger; a setting would add deployment surface for
# a number nobody tunes.
PENDING_ENROLLMENT_TTL_SECONDS = 900

# One administrator cannot park more than this many undecided uploads. The cap
# bounds disk use from a client that opens the dialog repeatedly and never
# answers; the oldest are swept first.
MAX_PENDING_PER_USER = 20

# How many expired rows one opportunistic sweep clears. Bounded so a request
# that happens to run the sweep does not pay for an unbounded backlog.
PENDING_SWEEP_BATCH = 100


def hash_upload_token(raw_token: str) -> str:
    """The stored form of an upload token. The token itself is never persisted."""
    return hashlib.sha256(str(raw_token).encode("utf-8")).hexdigest()


def pending_absolute_path(relative_path: str) -> str:
    """Resolve a stored pending path, refusing anything outside STORAGE_DIR.

    The inverse of `relative_storage_path`, with the same containment check:
    the value comes from a database row, and a row is not a reason to open an
    arbitrary path.
    """
    storage_dir = os.path.abspath(settings.STORAGE_DIR)
    relative = str(relative_path or "")
    if relative.startswith("storage/"):
        relative = relative[len("storage/"):]
    target = os.path.abspath(os.path.join(storage_dir, relative))
    if os.path.commonpath([storage_dir, target]) != storage_dir:
        raise EnrollmentError("invalid_storage_path",
                              "Refusing to read a file outside the storage directory.",
                              status_code=500)
    return target


def consolidate_image_file(stored_relative_path: str, winner_identity_id) -> Tuple[Optional[str], bool]:
    """Copy a merge loser's gallery file into the winner's UUID folder.

    Lives HERE, not in identity_service: tests/test_face_storage_single_format.py
    pins that only this module places files under FACES_DIR, which is what keeps
    every gallery file in one layout (UUID folder, image_NNN name).

    Copy, never move — the loser's file and folder are left exactly as they
    were, which is the whole crash-safety story: no failure mode of this
    function can lose a file, and `reserve_image_filename` claims the
    destination name with O_EXCL, so it can never overwrite one either (a
    collision simply yields the next free image_NNN slot).

    Returns (new_relative_path, True) on success, or (None, False) when the
    source is missing or unreadable — the caller then re-parents the database
    row but keeps its old path, which remains readable precisely because the
    loser's folder is never deleted.
    """
    try:
        source = pending_absolute_path(stored_relative_path)
        if not os.path.isfile(source):
            logger.warning("[ENROLL] consolidate: source file missing: %s",
                           stored_relative_path)
            return None, False

        extension = _extension_for(source, None)
        folder = identity_folder(winner_identity_id)
        os.makedirs(folder, exist_ok=True)
        destination = os.path.join(folder, reserve_image_filename(folder, extension))

        try:
            shutil.copy2(source, destination)
        except Exception:
            # Release the reservation so a failed copy cannot leave an empty
            # placeholder occupying an image_NNN slot forever.
            _safe_unlink(destination)
            raise
        return relative_storage_path(destination), True
    except Exception as exc:                                   # noqa: BLE001
        logger.warning("[ENROLL] consolidate: could not copy %s for identity %s: %s",
                       stored_relative_path, winner_identity_id, exc)
        return None, False


async def sweep_expired_pending(db: AsyncSession) -> int:
    """Delete expired claim tickets and their files. Never raises.

    Called opportunistically at the top of each review endpoint rather than
    from a background loop: the only thing that creates these rows is an
    upload, so a system with no uploads has nothing to sweep, and this avoids
    adding another supervised task to the lifespan for a 15-minute TTL.
    """
    swept = 0
    try:
        rows = (await db.execute(
            select(PendingEnrollment)
            .where(PendingEnrollment.expires_at <= datetime.utcnow())
            .limit(PENDING_SWEEP_BATCH))).scalars().all()
        if rows:
            for row in rows:
                _safe_unlink_pending(row.storage_path)
                await db.delete(row)
            await db.commit()
            swept = len(rows)
            logger.info("[ENROLL] swept %d expired pending enrollment(s)", swept)
    except Exception as exc:                                      # noqa: BLE001
        logger.warning("[ENROLL] pending sweep failed: %s", exc)
        await _rollback_quietly(db)

    swept += await _sweep_orphan_pending_files(db)
    return swept


async def _sweep_orphan_pending_files(db: AsyncSession) -> int:
    """Delete parked FILES that no longer have a row.

    Rows and files are written together but can be separated: a maintenance
    script that truncates the table, a restore from a database-only backup, or
    a crash between `_write_temp_file` and the insert. Without this the file
    survives forever — nothing else ever looks in this directory — and the
    gallery's own sweeps do not reach it because it is deliberately outside
    FACES_DIR.

    Only files older than the TTL are considered, so an upload being written
    right now by another request is never mistaken for an orphan.
    """
    directory = settings.PENDING_UPLOAD_DIR
    if not os.path.isdir(directory):
        return 0
    try:
        known = {os.path.basename(str(path)) for path in (await db.execute(
            select(PendingEnrollment.storage_path))).scalars().all()}
    except Exception as exc:                                      # noqa: BLE001
        logger.warning("[ENROLL] could not list pending rows for orphan sweep: %s", exc)
        return 0

    cutoff = datetime.utcnow().timestamp() - PENDING_ENROLLMENT_TTL_SECONDS
    removed = 0
    for name in os.listdir(directory)[:PENDING_SWEEP_BATCH]:
        if name in known:
            continue
        path = os.path.join(directory, name)
        try:
            if os.path.isfile(path) and os.path.getmtime(path) < cutoff:
                os.remove(path)
                removed += 1
        except OSError as exc:
            logger.warning("[ENROLL] could not remove orphan %s: %s", name, exc)
    if removed:
        logger.info("[ENROLL] removed %d orphaned pending upload file(s)", removed)
    return removed


def _safe_unlink_pending(relative_path: Optional[str]) -> None:
    """Remove a parked upload's file, tolerating a path we refuse to resolve."""
    if not relative_path:
        return
    try:
        _safe_unlink(pending_absolute_path(relative_path))
    except EnrollmentError:
        logger.warning("[ENROLL] refused to resolve pending path for cleanup")


async def create_pending_enrollment(
    db: AsyncSession,
    *,
    prepared: PreparedUpload,
    image_bytes: bytes,
    display_name: str,
    is_face_image: bool,
    original_filename: Optional[str],
    content_type: Optional[str],
    user_id: int,
    decision: str,
    candidates: List[Dict[str, Any]],
    top_similarity: Optional[float],
) -> Tuple[str, PendingEnrollment]:
    """Park an upload and return (raw token, row). The token is returned ONCE.

    Writes the file first and the row second, so a failure between them leaves
    an orphan file the sweeper collects — the other order would leave a ticket
    promising a file that does not exist, which the confirm path would have to
    treat as a server error.
    """
    await _enforce_pending_quota(db, user_id)

    absolute = _write_temp_file(image_bytes, prepared.extension,
                                directory=settings.PENDING_UPLOAD_DIR)
    try:
        raw_token = secrets.token_urlsafe(32)
        now = datetime.utcnow()
        row = PendingEnrollment(
            token_hash=hash_upload_token(raw_token),
            user_id=int(user_id),
            display_name=display_name[:255],
            display_name_key=name_lookup_key(display_name)[:255],
            storage_path=relative_storage_path(absolute),
            original_filename=(os.path.basename(original_filename)[:255]
                               if original_filename else None),
            file_checksum=prepared.checksum,
            content_type=(content_type or "")[:100] or None,
            file_size=len(image_bytes),
            width=prepared.width,
            height=prepared.height,
            is_face_image=bool(is_face_image),
            embedding_model_version=embedding_model_version(),
            detection_model_version=detection_model_version(),
            decision=decision,
            top_similarity=(float(top_similarity)
                            if top_similarity is not None else None),
            candidates=candidates,
            created_at=now,
            expires_at=now + timedelta(seconds=PENDING_ENROLLMENT_TTL_SECONDS),
        )
        db.add(row)
        await db.commit()
    except Exception:
        _safe_unlink(absolute)
        await _rollback_quietly(db)
        raise
    return raw_token, row


async def _enforce_pending_quota(db: AsyncSession, user_id: int) -> None:
    """Drop this user's oldest tickets once they hold too many undecided ones."""
    rows = (await db.execute(
        select(PendingEnrollment)
        .where(PendingEnrollment.user_id == int(user_id))
        .order_by(PendingEnrollment.created_at.asc()))).scalars().all()
    excess = len(rows) - (MAX_PENDING_PER_USER - 1)
    if excess <= 0:
        return
    for row in rows[:excess]:
        _safe_unlink_pending(row.storage_path)
        await db.delete(row)
    await db.commit()
    logger.info("[ENROLL] dropped %d oldest pending enrollment(s) for user %s "
                "(cap %d)", excess, user_id, MAX_PENDING_PER_USER)


def _pending_not_found() -> EnrollmentError:
    """One refusal for every way a ticket can be unusable.

    Expired, already decided, never existed, or belongs to a different
    administrator all answer identically — otherwise the response would confirm
    that somebody else's upload exists to anyone holding a stolen token.
    """
    return EnrollmentError(
        "pending_upload_not_found",
        "This upload is no longer awaiting a decision. It may have expired, "
        "already been decided, or belong to another administrator. Please "
        "upload the photo again.",
        status_code=410)


def _token_predicates(raw_token: str, user_id: int):
    token = str(raw_token or "").strip()
    if not token:
        raise EnrollmentError("invalid_upload_token",
                              "No upload token was supplied.", status_code=400)
    return (PendingEnrollment.token_hash == hash_upload_token(token),
            PendingEnrollment.user_id == int(user_id),
            PendingEnrollment.expires_at > datetime.utcnow())


async def peek_pending_enrollment(db: AsyncSession, raw_token: str,
                                  user_id: int) -> PendingEnrollment:
    """Read a claim ticket WITHOUT consuming it, or refuse with 410.

    Validation reads the ticket through this, so a refusal the administrator
    can act on — the wrong person chosen, a second confirmation still needed —
    leaves the ticket usable and the dialog answerable. Consuming first would
    make every mis-click cost a re-upload.

    Exactly-once is still guaranteed, because nothing is applied until
    `claim_pending_enrollment` below succeeds.
    """
    row = (await db.execute(
        select(PendingEnrollment).where(*_token_predicates(raw_token, user_id))
    )).scalar_one_or_none()
    if row is None:
        raise _pending_not_found()
    return row


async def claim_pending_enrollment(db: AsyncSession, raw_token: str,
                                   user_id: int) -> PendingEnrollment:
    """Consume a claim ticket exactly once, or refuse with 410.

    A single DELETE ... RETURNING is what makes one-time use race-free without
    a consumed flag or a lock: two concurrent confirmations both run it, one
    matches a row and one matches nothing. The row is gone before enrollment
    starts, which is also what makes "deleted after approval, rejection,
    cancellation or expiry" literally true.

    Call this LAST, once every check has passed. CONSEQUENCE, by design: a
    failure in the enrollment itself still burns the token and the
    administrator uploads again — replaying a claim ticket after a partial
    failure is what would let one review approve two enrollments.

    The delete is COMMITTED here rather than left to the caller's transaction.
    `enroll_image` rolls its own transaction back on any refusal, and that
    rollback would otherwise un-consume the ticket while this route had already
    deleted the file it points at — a live claim aimed at nothing.
    """
    row = (await db.execute(
        delete(PendingEnrollment)
        .where(*_token_predicates(raw_token, user_id))
        .returning(PendingEnrollment))).scalar_one_or_none()
    if row is None:
        raise _pending_not_found()
    # Detach before the commit expires the instance: the caller still needs
    # storage_path, checksum and is_face_image from a row that no longer exists.
    db.expunge(row)
    await db.commit()
    return row


async def enroll_image(
    db: AsyncSession,
    *,
    image_bytes: bytes,
    original_filename: Optional[str],
    content_type: Optional[str],
    is_face_image: bool = False,
    identity_id: Optional[str] = None,
    person_name: Optional[str] = None,
    actor_user_id: Optional[int] = None,
    allow_create: bool = True,
    prepared: Optional[PreparedUpload] = None,
    allowed_statuses: Sequence[IdentityStatus] = (IdentityStatus.ACTIVE,),
) -> EnrollmentResult:
    """Enroll one photo, creating the identity only when asked and needed.

    Exactly one of identity_id (explicit, preferred) or person_name (legacy
    name lookup) selects the target. Raises EnrollmentError on any refusal;
    returns only when identity + image + embedding are all durable and usable.

    `prepared` lets a caller that has already decoded, detected and embedded
    these exact bytes hand the result in rather than paying for all three a
    second time. Omitting it is the original behaviour, unchanged.
    """
    if not identity_id and not person_name:
        raise EnrollmentError("invalid_request",
                              "A person must be identified by id or by name.")

    # ---- cheap validation, before any side effect -------------------------
    if prepared is None:
        prepared = prepare_upload(image_bytes,
                                  original_filename=original_filename,
                                  content_type=content_type,
                                  is_face_image=is_face_image)
    checksum = prepared.checksum
    width, height = prepared.width, prepared.height
    embedding_normalized = prepared.embedding_normalized

    # ---- the identity service is mandatory under pgvector -----------------
    service = _identity_service()
    if service is None:
        if settings.VECTOR_BACKEND == "pgvector":
            logger.error("[ENROLL] identity_service unavailable while VECTOR_BACKEND=pgvector")
            raise EnrollmentError(
                "service_unavailable",
                "Identity service is not available. Please try again later.",
                status_code=503,
                details="The identity management service is not initialized.")
        raise EnrollmentError(
            "service_unavailable",
            "Identity service is not available. Please try again later.",
            status_code=503,
            details="The identity management service is not initialized.")

    extension = prepared.extension
    temp_path = _write_temp_file(image_bytes, extension)
    final_path: Optional[str] = None
    identity_created = False

    try:
        # ---- resolve or create the identity -------------------------------
        if identity_id:
            identity = await get_active_identity(db, identity_id,
                                                 allowed_statuses=allowed_statuses)
        else:
            display_name = validate_person_name(person_name)
            # Serialize everyone enrolling THIS name for the rest of the
            # transaction. Looking a name up and then inserting it is a
            # check-then-act: two uploads of a new name both found nothing
            # (neither had committed) and both inserted, producing two people
            # with one name — after which every later upload for that name
            # failed forever with ambiguous_identity, needing a manual merge.
            #
            # Transaction-scoped, so it is released by the commit or rollback
            # below and never leaks. Keyed on the same casefolded key the
            # lookup uses, so two spellings that resolve to one person also
            # take one lock. hashtext() is stable within a major version, and a
            # collision only means two unrelated names serialize briefly.
            await db.execute(
                text("SELECT pg_advisory_xact_lock(hashtext(:key))"),
                {"key": "enroll:" + name_lookup_key(display_name)})
            identity = await resolve_identity_by_name(db, display_name)
            if identity is None:
                if not allow_create:
                    raise EnrollmentError("identity_not_found", "Person not found.",
                                          status_code=404)
                now = datetime.utcnow()
                # Both timestamps are NOT NULL and this person has no sighting
                # history at all, so enrollment time is the only honest seed:
                # it is genuinely the first moment the system knew of them.
                # Later uploads must not move last_seen_at — see the bookkeeping
                # block below.
                identity = Identity(
                    id=uuid_module.uuid4(),
                    type=IdentityType.KNOWN,
                    status=IdentityStatus.ACTIVE,
                    display_name=display_name,
                    first_seen_at=now,
                    last_seen_at=now,
                    appearances_count=0,
                    created_at=now,
                    updated_at=now,
                )
                db.add(identity)
                await db.flush()
                identity_created = True
                logger.info("[ENROLL] created identity %s for '%s'", identity.id, display_name)

        # ---- duplicate photo for this identity is idempotent --------------
        # Only meaningful for a pre-existing identity: one we just created
        # cannot already own an image, so we never have to unwind a creation
        # here (which would contradict the identity_id we return).
        existing_image = None
        if not identity_created:
            existing_image = (await db.execute(
                select(IdentityImage).where(
                    IdentityImage.identity_id == identity.id,
                    IdentityImage.file_checksum == checksum,
                ))).scalar_one_or_none()
        if existing_image is not None:
            # Nothing was created; drop the temp copy and report the original.
            _safe_unlink(temp_path)
            logger.info("[ENROLL] duplicate photo for identity %s -> image %s",
                        identity.id, existing_image.id)
            return EnrollmentResult(
                identity_id=str(identity.id),
                image_id=existing_image.id,
                identity_created=False,
                image_created=False,
                embedding_created=False,
                is_primary=bool(existing_image.is_primary),
                duplicate=True,
                filename=os.path.basename(existing_image.storage_path or ""),
                display_name=identity.display_name,
                source_type=existing_image.source_type,
                storage_path=existing_image.storage_path,
            )

        # ---- decide placement + primary status ----------------------------
        folder = identity_folder(identity.id)
        os.makedirs(folder, exist_ok=True)
        filename = reserve_image_filename(folder, extension)
        final_path = os.path.join(folder, filename)
        stored_path = relative_storage_path(final_path)

        has_primary = (await db.execute(
            select(func.count(IdentityImage.id)).where(
                IdentityImage.identity_id == identity.id,
                IdentityImage.is_primary.is_(True),
            ))).scalar() or 0
        # Only the FIRST image self-promotes. A newer photo never displaces an
        # existing primary just for being newer — that is an explicit action.
        is_primary = has_primary == 0

        image_row = IdentityImage(
            identity_id=identity.id,
            storage_path=stored_path,
            original_filename=(os.path.basename(original_filename)[:255]
                               if original_filename else None),
            file_checksum=checksum,
            content_type=(content_type or "")[:100] or None,
            file_size=len(image_bytes),
            width=int(width),
            height=int(height),
            is_primary=is_primary,
            # Audit trail: a pre-cropped upload is stored in the same folder,
            # by the same workflow, and is distinguishable only by this field.
            source_type=("cropped_face" if is_face_image else "upload"),
            quality_score=prepared.quality,
            quality_scorer_version=prepared.quality_scorer_version,
            processing_status="pending",
            created_at=datetime.utcnow(),
            created_by=actor_user_id,
            updated_at=datetime.utcnow(),
        )
        db.add(image_row)
        # Read these BEFORE the flush: a rollback expires every ORM attribute,
        # and the duplicate branch below needs them after one.
        identity_uuid = identity.id
        identity_display = identity.display_name
        try:
            await db.flush()   # assigns image_row.id and enforces the unique keys
        except IntegrityError:
            # Lost a race with a concurrent upload of the SAME photo for the
            # same person: both passed the dedup SELECT above, and
            # uq_identity_image_checksum caught the second one here. The
            # sequential path reports that as a friendly duplicate, so losing
            # by microseconds must not surface as a 500.
            #
            # Safe to unwind: a checksum conflict is only reachable for a
            # pre-existing identity (one we just created owns no images), so
            # the rollback cannot erase an identity this call created.
            await _rollback_quietly(db)
            _safe_unlink(temp_path)
            temp_path = None
            _safe_unlink(final_path)
            final_path = None

            winner = (await db.execute(
                select(IdentityImage).where(
                    IdentityImage.identity_id == identity_uuid,
                    IdentityImage.file_checksum == checksum,
                ))).scalar_one_or_none()
            if winner is None:
                raise      # a different constraint — let the caller handle it

            logger.info("[ENROLL] concurrent duplicate photo for identity %s "
                        "-> existing image %s", identity_uuid, winner.id)
            return EnrollmentResult(
                identity_id=str(identity_uuid),
                image_id=winner.id,
                identity_created=False,
                image_created=False,
                embedding_created=False,
                is_primary=bool(winner.is_primary),
                duplicate=True,
                filename=os.path.basename(winner.storage_path or ""),
                display_name=identity_display,
                source_type=winner.source_type,
                storage_path=winner.storage_path,
            )

        # ---- persist the embedding, linked to identity AND image ----------
        #
        # defer_commit=True keeps this inside OUR transaction. Without it the
        # FAISS branch commits here — before `os.replace` below — which split
        # enrollment across two transactions: the except-block below would then
        # have nothing left to roll back, and a failure between that commit and
        # the file move left a committed identity_images row pointing at a file
        # that never landed. The deferred path also withholds the index call, so
        # a rollback cannot strand a phantom entry keyed to a vanished row.
        embedding_record = await service.save_embedding(
            identity=identity,
            embedding=embedding_normalized,
            detection_id=None,
            pipeline_id=None,  # enrolled photo: not a camera sighting (provenance = image_id)
            quality_score=None,
            db=db,
            defer_commit=True,
        )
        validate_saved_embedding(embedding_record, identity.id, None)

        # save_embedding predates identity_images and does not take an
        # image_id; attribute the row here, inside the same transaction.
        embedding_record.image_id = image_row.id
        # Quality is recorded, NOT enforced. Passing it into save_embedding
        # would engage the gate at identity_service.save_embedding, which
        # returns None below its threshold — and a None return here raises
        # embedding_save_failed, so a merely blurry photo would be refused with
        # an opaque 500 instead of a clear message. Whether to reject on
        # quality is a product decision; recording it is not, and it has to be
        # recorded before that decision can even be made.
        embedding_record.quality = prepared.quality
        embedding_record.quality_scorer_version = prepared.quality_scorer_version
        await db.flush()
        validate_saved_embedding(embedding_record, identity.id, image_row.id)

        # ---- identity bookkeeping -----------------------------------------
        # last_seen_at is NOT touched here. It means "when a camera last saw
        # this person", and an administrator adding a photo is not a sighting.
        # Stamping it on upload overwrote real observations — a person last
        # seen a week ago read as seen just now, while appearances_count still
        # showed the old total, so the review card contradicted itself.
        #
        # Consequence accepted deliberately: a person nobody has seen for
        # INACTIVE_THRESHOLD_DAYS is still marked INACTIVE by identity
        # retention even if a photo was just added. That is the intended
        # meaning of "inactive" — no sightings — and hiding it behind an upload
        # is what made the field untrustworthy in the first place.
        identity.updated_at = datetime.utcnow()
        if is_primary or not identity.best_snapshot_path:
            # best_snapshot_path stays the primary image's path so every
            # existing consumer keeps working unchanged.
            identity.best_snapshot_path = stored_path

        image_row.processing_status = "completed"
        image_row.updated_at = datetime.utcnow()

        # ---- the file moves LAST, just before commit ----------------------
        os.replace(temp_path, final_path)
        temp_path = None  # ownership transferred to final_path

        await db.commit()

    except EnrollmentError:
        await _rollback_quietly(db)
        _safe_unlink(temp_path)
        if final_path:
            _safe_unlink(final_path)
        raise
    except Exception as exc:
        # A database, filesystem or driver failure must never surface as
        # success, and must never leak its text to the client.
        logger.error("[ENROLL] enrollment failed: %s", exc, exc_info=True)
        await _rollback_quietly(db)
        _safe_unlink(temp_path)
        if final_path:
            _safe_unlink(final_path)
        raise EnrollmentError(
            "database_update_failed",
            "The person could not be added.",
            status_code=500,
            details="The enrollment could not be completed. Please try again.")

    # ---- committed. The search index is a cache from here on ---------------
    # Per-vector sync state lives on identity_embeddings.vector_index_sync_state,
    # not on the image row — the state belongs to the vector, and duplicating it
    # here meant two places to drift.
    warnings = []

    # The vector row was committed above as 'pending'. Index it NOW so the person
    # is findable immediately; a failure here is a warning, never an error. The
    # enrollment is already durable, and reconciliation adds any pending vector
    # within VECTOR_INDEX_RECONCILE_INTERVAL_SECONDS regardless.
    index_state = "synced"
    try:
        index_state = await service.sync_pending_embedding(
            embedding_record.id, embedding_normalized, db)
    except Exception as exc:                                      # noqa: BLE001
        index_state = "failed"
        logger.error("[ENROLL] deferred index sync raised for embedding %s "
                     "(enrollment is committed and safe): %s",
                     embedding_record.id, exc, exc_info=True)
    if index_state != "synced":
        warnings.append("vector_index_sync_pending")

    logger.info("[ENROLL] identity=%s image=%s created_identity=%s primary=%s",
                identity.id, image_row.id, identity_created, is_primary)

    return EnrollmentResult(
        identity_id=str(identity.id),
        image_id=image_row.id,
        identity_created=identity_created,
        image_created=True,
        embedding_created=True,
        is_primary=is_primary,
        duplicate=False,
        filename=filename,
        display_name=identity.display_name,
        source_type=image_row.source_type,
        storage_path=stored_path,
        warnings=warnings,
    )


async def _rollback_quietly(db: AsyncSession) -> None:
    try:
        await db.rollback()
    except Exception as exc:
        logger.warning("[ENROLL] rollback failed: %s", exc)


# ---------------------------------------------------------------------------
# Primary image selection
# ---------------------------------------------------------------------------

async def adopt_existing_file(db: AsyncSession, identity, source_path: str,
                             *, source_type: str = "promotion",
                             actor_user_id: Optional[int] = None) -> Optional[IdentityImage]:
    """Copy a file that already exists on disk into an identity's UUID folder.

    Used by the unknown->known promotion path, which has a detection snapshot
    rather than an HTTP upload. It reuses the same placement rules as
    enroll_image — UUID folder, server-generated image_NNN name, normalized
    relative storage_path, SHA-256 dedup — so promotion can never recreate the
    old display-name layout.

    Returns the IdentityImage row (added to the session, NOT committed — the
    caller owns the transaction), or None when there is nothing to adopt.
    Never raises: a promotion must not fail because an image could not be
    copied.
    """
    try:
        if not source_path or not os.path.isfile(source_path):
            logger.warning("[ENROLL] adopt: source file missing for identity %s", identity.id)
            return None

        with open(source_path, "rb") as handle:
            payload = handle.read()
        if not payload:
            return None
        checksum = sha256_of(payload)

        existing = (await db.execute(
            select(IdentityImage).where(
                IdentityImage.identity_id == identity.id,
                IdentityImage.file_checksum == checksum,
            ))).scalar_one_or_none()
        if existing is not None:
            logger.info("[ENROLL] adopt: identity %s already owns this image (%s)",
                        identity.id, existing.id)
            return existing

        width = height = None
        try:
            import cv2
            decoded = cv2.imdecode(np.frombuffer(payload, np.uint8), cv2.IMREAD_COLOR)
            if decoded is not None:
                height, width = decoded.shape[:2]
        except Exception:
            pass

        extension = _extension_for(source_path, None)
        folder = identity_folder(identity.id)
        os.makedirs(folder, exist_ok=True)
        filename = reserve_image_filename(folder, extension)
        final_path = os.path.join(folder, filename)
        stored_path = relative_storage_path(final_path)

        has_primary = (await db.execute(
            select(func.count(IdentityImage.id)).where(
                IdentityImage.identity_id == identity.id,
                IdentityImage.is_primary.is_(True),
            ))).scalar() or 0

        shutil.copy2(source_path, final_path)

        image_row = IdentityImage(
            identity_id=identity.id,
            storage_path=stored_path,
            original_filename=os.path.basename(source_path)[:255],
            file_checksum=checksum,
            content_type=None,
            file_size=len(payload),
            width=width,
            height=height,
            is_primary=(has_primary == 0),
            source_type=source_type,
            processing_status="completed",
            created_at=datetime.utcnow(),
            created_by=actor_user_id,
            updated_at=datetime.utcnow(),
        )
        db.add(image_row)
        await db.flush()
        logger.info("[ENROLL] adopt: identity %s -> %s", identity.id, stored_path)
        return image_row
    except Exception as exc:
        logger.warning("[ENROLL] adopt failed for identity %s: %s",
                       getattr(identity, "id", "?"), exc, exc_info=True)
        return None


async def set_primary_image(db: AsyncSession, identity_id: str, image_id: int,
                            actor_user_id: Optional[int] = None) -> Dict[str, Any]:
    """Promote one image to primary. Explicit, administrator-driven only."""
    identity = await get_active_identity(db, identity_id)

    image = (await db.execute(
        select(IdentityImage).where(
            IdentityImage.id == image_id,
            IdentityImage.identity_id == identity.id,
        ))).scalar_one_or_none()
    if image is None:
        raise EnrollmentError("image_not_found",
                              "That photo does not belong to this person.",
                              status_code=404)
    if image.processing_status != "completed":
        raise EnrollmentError("image_not_usable",
                              "That photo has not finished processing.",
                              status_code=409)

    try:
        # Demote first: the partial unique index permits exactly one primary,
        # so both statements must land in the same transaction.
        await db.execute(
            IdentityImage.__table__.update()
            .where(IdentityImage.identity_id == identity.id,
                   IdentityImage.is_primary.is_(True))
            .values(is_primary=False, updated_at=datetime.utcnow()))
        await db.execute(
            IdentityImage.__table__.update()
            .where(IdentityImage.id == image.id)
            .values(is_primary=True, updated_at=datetime.utcnow()))
        identity.best_snapshot_path = image.storage_path
        identity.updated_at = datetime.utcnow()
        await db.commit()
    except Exception as exc:
        await _rollback_quietly(db)
        logger.error("[ENROLL] set_primary_image failed: %s", exc, exc_info=True)
        raise EnrollmentError("primary_update_failed",
                              "The primary photo could not be changed.",
                              status_code=500)

    return {"identity_id": str(identity.id), "image_id": image.id, "is_primary": True}


def serialize_image(image: IdentityImage) -> Dict[str, Any]:
    """Client view of a photo — relative path only, never an absolute one."""
    return {
        "image_id": image.id,
        "identity_id": str(image.identity_id),
        "filename": os.path.basename(image.storage_path or ""),
        "url": "/" + image.storage_path.lstrip("/") if image.storage_path else None,
        "content_type": image.content_type,
        "file_size": image.file_size,
        "width": image.width,
        "height": image.height,
        "quality_score": image.quality_score,
        "is_primary": bool(image.is_primary),
        "source_type": image.source_type,
        "processing_status": image.processing_status,
        "created_at": image.created_at.isoformat() + "Z" if image.created_at else None,
    }

"""Server-side admission checks for promoting a stored unknown identity."""
from sqlalchemy import select, func
from config import settings
from db_models import (Identity, IdentityEmbedding, IdentityImage, IdentityStatus, IdentityType,
                       IdentityAppearance, Face, Detection, Pipeline)
import asyncio
import hashlib
from pathlib import Path
from types import SimpleNamespace


class PromotionError(ValueError):
    def __init__(self, code, message, status_code=409, review=None):
        super().__init__(message)
        self.code, self.status_code, self.review = code, status_code, review


async def selected_sighting(db, person, appearance_id, *, photo_sha256=None, lock=False):
    """Resolve one persisted event, never the group's representative portrait.

    Older ambiguous events must be reviewed/re-enrolled explicitly. Guessing
    which face owns a vector would reintroduce the original whole-group error.
    """
    from backend.core.enrollment_service import pending_absolute_path
    from backend.utils.path_utils import normalize_storage_path
    if person.status != IdentityStatus.ACTIVE or person.merged_into_id is not None:
        raise PromotionError('IDENTITY_NOT_ACTIVE', 'Refresh: this Unknown group is no longer active.')
    if person.type != IdentityType.UNKNOWN:
        raise PromotionError('IDENTITY_NOT_UNKNOWN', 'This profile is already Known. Refresh the page.')
    if not appearance_id:
        raise PromotionError('PROMOTION_SELECTION_REQUIRED', 'Select a saved sighting before promoting.', 400)
    query = select(IdentityAppearance).where(IdentityAppearance.id == appearance_id,
                                           IdentityAppearance.identity_id == person.id)
    if lock:
        query = query.with_for_update().execution_options(populate_existing=True)
    appearance = (await db.execute(query)).scalar_one_or_none()
    if appearance is None:
        raise PromotionError('PROMOTION_SELECTION_CHANGED', 'This sighting no longer belongs to this Unknown group. Refresh and select it again.')
    faces = (await db.execute(select(Face).join(Detection, Detection.id == Face.detection_id).where(
        Face.identity_id == person.id, Face.detection_id == appearance.detection_id,
        Detection.pipeline_id == appearance.pipeline_id))).scalars().all() if appearance.detection_id else []
    count = (await db.execute(select(func.count()).select_from(IdentityAppearance).where(
        IdentityAppearance.identity_id == person.id,
        IdentityAppearance.detection_id == appearance.detection_id))).scalar() if appearance.detection_id else 0
    path = normalize_storage_path(appearance.best_snapshot_path) if appearance.best_snapshot_path else None
    if len(faces) != 1 or count != 1 or not path or normalize_storage_path(faces[0].face_image_path) != path:
        raise PromotionError('PROMOTION_SELECTION_AMBIGUOUS',
            'This saved sighting cannot be linked unambiguously to one face. Select another sighting or enroll its photo through Known Faces.', 400)
    try:
        absolute = pending_absolute_path(path)
        payload = await asyncio.to_thread(Path(absolute).read_bytes)
    except (OSError, ValueError) as exc:
        raise PromotionError('PROMOTION_IMAGE_REQUIRED', 'The selected photo is no longer available. Select another saved sighting.', 400) from exc
    checksum = hashlib.sha256(payload).hexdigest()
    if photo_sha256 is not None and checksum != photo_sha256:
        raise PromotionError('PROMOTION_PHOTO_CHANGED', 'The selected photo changed. Close this dialog and preview it again.')
    camera = (await db.execute(select(Pipeline.location_name).where(Pipeline.pipeline_id == appearance.pipeline_id))).scalar_one_or_none()
    return SimpleNamespace(appearance=appearance, face=faces[0], path=absolute,
                           payload=payload, checksum=checksum, camera=camera or appearance.location_name or appearance.pipeline_id)


async def prepare_selected_photo(selection):
    from backend.core.enrollment_service import prepare_upload
    try:
        return await asyncio.to_thread(prepare_upload, selection.payload, is_face_image=True)
    except Exception as exc:
        raise PromotionError('PROMOTION_REFERENCE_INVALID',
            'The selected photo is not a usable single-face reference. Select a clear photo or enroll one through Known Faces.', 400) from exc


async def review_promotion(db, person, display_name, model_version, *, selected_vector):
    if not model_version:
        raise PromotionError('PROMOTION_MODEL_UNAVAILABLE', 'The recognition model is not ready. Try again when it is available.', 503)
    vectors = [selected_vector]
    # Query PostgreSQL directly: the duplicate check must not mistake an unavailable
    # optional search index for "no matching person". Compare verified model spaces.
    best = {}
    for vector in vectors:
        distance = IdentityEmbedding.embedding.cosine_distance(vector)
        nearest = (await db.execute(select(Identity.id, func.min(distance).label('distance'))
            .join(IdentityEmbedding, IdentityEmbedding.identity_id == Identity.id)
            .join(IdentityImage, (IdentityImage.id == IdentityEmbedding.image_id)
                  & (IdentityImage.identity_id == Identity.id)
                  & (IdentityImage.processing_status == 'completed'))
            .where(Identity.type == IdentityType.KNOWN,
                   Identity.status.in_([IdentityStatus.ACTIVE, IdentityStatus.PROMOTED]),
                   Identity.merged_into_id.is_(None), Identity.id != person.id,
                   IdentityEmbedding.embedding_model_version == model_version,
                   IdentityEmbedding.embedding.isnot(None))
            .group_by(Identity.id).having(func.min(distance) <= 1 - settings.ENROLL_STRONG_MATCH_MIN)
            .order_by(func.min(distance), Identity.id).limit(settings.ENROLL_MAX_CANDIDATES))).all()
        for identifier, value in nearest:
            best[identifier] = max(best.get(identifier, -1), 1 - float(value))

    normalized = func.lower(func.regexp_replace(func.btrim(Identity.display_name), r'\s+', ' ', 'g'))
    name_key = ' '.join(display_name.split()).lower()
    same_names = (await db.execute(select(Identity.id).where(
        Identity.type == IdentityType.KNOWN,
        Identity.status.in_([IdentityStatus.ACTIVE, IdentityStatus.PROMOTED]),
        Identity.merged_into_id.is_(None), Identity.id != person.id,
        normalized == name_key))).scalars().all()
    ids = set(best) | set(same_names)
    people = (await db.execute(select(Identity).where(Identity.id.in_(ids)).order_by(Identity.id))).scalars().all() if ids else []
    return {'candidates': [{'identity_id': str(p.id), 'display_name': p.display_name,
                           'similarity': best.get(p.id), 'same_name': p.id in same_names} for p in people],
            'threshold': float(settings.ENROLL_STRONG_MATCH_MIN),
            'model_version': model_version}

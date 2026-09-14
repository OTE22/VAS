"""Bounded Quick Search inputs and exact, filtered per-person ranking."""
from datetime import datetime, timezone
from io import BytesIO
import warnings
from PIL import Image
from fastapi import HTTPException
from sqlalchemy import select, func, or_, and_
from config import settings
from db_models import Identity, IdentityEmbedding, IdentityAppearance, IdentityType, IdentityStatus
from backend.core.merge_compatibility import _parse_vector


def search_dates(date_from, date_to):
    def parse(value):
        if not value:
            return None
        try:
            result = datetime.fromisoformat(value.replace('Z', '+00:00'))
            return result.astimezone(timezone.utc).replace(tzinfo=None) if result.tzinfo else result
        except (ValueError, TypeError):
            raise HTTPException(422, 'Dates must use ISO date/time format.')
    start, end = parse(date_from), parse(date_to)
    if start and end and start > end:
        raise HTTPException(422, 'Start date must not be later than end date.')
    return start, end


def decode_search_image(payload):
    from backend.core.enrollment_service import (decode_and_validate_image, EnrollmentError,
                                               MAX_IMAGE_SIDE, MAX_DECODED_PIXELS)
    # Inspect dimensions before OpenCV allocates the decoded pixel buffer.
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('error', Image.DecompressionBombWarning)
            with Image.open(BytesIO(payload)) as header:
                width, height = header.size
                if max(width, height) > MAX_IMAGE_SIDE or width * height > MAX_DECODED_PIXELS:
                    raise HTTPException(413, 'Image resolution is too large to process.')
    except HTTPException:
        raise
    except (Image.DecompressionBombError, Image.DecompressionBombWarning):
        raise HTTPException(413, 'Image resolution is too large to process.')
    except Exception:
        raise HTTPException(400, 'Invalid image file. Upload a JPG, PNG or WEBP image.')
    try:
        return decode_and_validate_image(payload)
    except EnrollmentError as exc:
        raise HTTPException(400, str(exc))


async def rank_people(db, embedding, model_version, scope, top_k, start=None, end=None, pipeline_id=None):
    vector = _parse_vector(embedding)
    if vector is None or vector.size != 512 or not model_version:
        raise HTTPException(503, 'The current recognition model is not ready for search.')
    distance = IdentityEmbedding.embedding.cosine_distance(vector)
    filters = [Identity.merged_into_id.is_(None),
               Identity.status.in_([IdentityStatus.ACTIVE, IdentityStatus.PROMOTED]),
               IdentityEmbedding.embedding_model_version == model_version,
               IdentityEmbedding.embedding.isnot(None)]
    thresholds = []
    if scope in ('known', 'both'):
        thresholds.append(and_(Identity.type == IdentityType.KNOWN, distance <= 1 - float(settings.SIMILARITY_THRESHOLD)))
    if scope in ('unknown', 'both'):
        thresholds.append(and_(Identity.type == IdentityType.UNKNOWN, distance <= 1 - float(settings.UNKNOWN_SIMILARITY_THRESHOLD)))
    filters.append(or_(*thresholds))
    if start or end or pipeline_id:
        appearance = select(IdentityAppearance.id).where(IdentityAppearance.identity_id == Identity.id)
        if start: appearance = appearance.where(IdentityAppearance.start_time >= start)
        if end: appearance = appearance.where(IdentityAppearance.start_time <= end)
        if pipeline_id: appearance = appearance.where(IdentityAppearance.pipeline_id == pipeline_id)
        filters.append(appearance.exists())
    # Filter first, collapse ALL embeddings to one identity, then apply top_k.
    best_by_identity = (select(Identity.id.label('identity_id'), func.min(distance).label('distance'))
        .join(IdentityEmbedding, IdentityEmbedding.identity_id == Identity.id)
        .where(*filters).group_by(Identity.id).subquery())
    result = await db.execute(select(Identity, (1 - best_by_identity.c.distance).label('similarity'))
        .join(best_by_identity, best_by_identity.c.identity_id == Identity.id)
        .order_by(best_by_identity.c.distance, Identity.id).limit(top_k))
    return result.all()

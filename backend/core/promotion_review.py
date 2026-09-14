"""Server-side admission checks for promoting a stored unknown identity."""
from sqlalchemy import select, func
from config import settings
from db_models import Identity, IdentityEmbedding, IdentityStatus, IdentityType
from backend.core.merge_compatibility import _load_valid_embeddings


class PromotionError(ValueError):
    def __init__(self, code, message, status_code=409, review=None):
        super().__init__(message)
        self.code, self.status_code, self.review = code, status_code, review


async def review_promotion(db, person, display_name, model_version):
    samples, _ = await _load_valid_embeddings(db, person.id)
    vectors = samples.get(model_version, []) if model_version else []
    if not vectors:
        raise PromotionError('PROMOTION_EMBEDDINGS_REQUIRED',
            'This identity has no usable face embeddings for the current recognition model. '
            'Capture a new clear detection before promoting it.')

    # Query PostgreSQL directly: the duplicate check must not mistake an unavailable
    # optional search index for "no matching person". Compare verified model spaces.
    best = {}
    for vector in vectors:
        distance = IdentityEmbedding.embedding.cosine_distance(vector)
        nearest = (await db.execute(select(Identity.id, func.min(distance).label('distance'))
            .join(IdentityEmbedding, IdentityEmbedding.identity_id == Identity.id)
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

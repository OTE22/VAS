"""Paged, direct-query sighting search. No identity expansion or reference learning.

The original query lives in the existing Redis service for 30 minutes, bound to
the requesting admin. Only an opaque ID goes to the browser. No new DB objects.
"""
from datetime import datetime
from uuid import UUID, uuid4

from fastapi import HTTPException
from sqlalchemy import select, func, and_, tuple_

from config import settings
from db_models import Identity, IdentityEmbedding, IdentityAppearance, IdentityStatus, IdentityType, Face, Pipeline
from backend.core.merge_compatibility import _parse_vector
from backend.core.redis_cache import redis_cache_service
from backend.core.search_evidence import hydrate_evidence
from backend.utils.path_utils import path_to_url

CONTEXT_TTL = 1800


def current_model():
    from backend.core.advanced_search import _embedding_model_version
    return _embedding_model_version()


async def create_context(db, faces, *, user_id, scope, filters, exclude_identity_ids=None):
    vectors = {}
    for face in faces:
        vector = _parse_vector(face.embedding) if not face.skipped else None
        if vector is not None and vector.size == 512:
            vectors[str(face.face_index)] = vector.tolist()
    if not vectors or not current_model():
        return None
    watermark = await db.scalar(select(func.coalesce(func.max(IdentityAppearance.id), 0)))
    context = {
        'user_id': str(user_id), 'model': current_model(), 'scope': scope,
        'threshold': float(settings.SIMILARITY_THRESHOLD), 'vectors': vectors,
        'filters': {key: value.isoformat() if isinstance(value, datetime) else value
                    for key, value in (filters or {}).items()},
        'exclude_ids': exclude_identity_ids or [], 'watermark': watermark,
    }
    token = str(uuid4())
    if await redis_cache_service.set('search-appearances:' + token, context, ttl=CONTEXT_TTL):
        return token
    return None


async def load_context(token, user_id, face_index):
    try:
        token = str(UUID(str(token)))
    except (ValueError, TypeError):
        raise HTTPException(404, 'Search context not found.')
    context = await redis_cache_service.get('search-appearances:' + token)
    if not context or context.get('user_id') != str(user_id):
        raise HTTPException(410, 'This search has expired or is unavailable. Run the image search again.')
    if (context['model'] != current_model() or
            context['threshold'] != float(settings.SIMILARITY_THRESHOLD)):
        raise HTTPException(409, 'Recognition settings changed. Run the image search again.')
    if str(face_index) not in context['vectors']:
        raise HTTPException(404, 'This face was not searchable in the submitted image.')
    return context


def appearance_candidates(context, face_index):
    """One usable vector per sighting, including Known and Unknown groups.

    Count links before the similarity filter: two vectors cannot become an
    apparently unique link merely because only one passed the threshold.
    Group membership and the best match's vector never broaden the query.
    """
    vector = _parse_vector(context['vectors'][str(face_index)])
    if vector is None or vector.size != 512:
        raise HTTPException(410, 'Search context is invalid. Run the image search again.')
    distance = IdentityEmbedding.embedding.cosine_distance(vector)
    vectors = select(
        IdentityEmbedding.id.label('embedding_id'), IdentityEmbedding.identity_id,
        IdentityEmbedding.detection_id, IdentityEmbedding.pipeline_id,
        distance.label('distance'),
        func.count().over(partition_by=(IdentityEmbedding.identity_id,
            IdentityEmbedding.detection_id)).label('links')
    ).where(IdentityEmbedding.embedding_model_version == context['model'],
            IdentityEmbedding.detection_id.isnot(None)).subquery()
    appearances = select(IdentityAppearance.id.label('appearance_id'),
        func.count().over(partition_by=(IdentityAppearance.identity_id,
            IdentityAppearance.detection_id, IdentityAppearance.pipeline_id)).label('links')
    ).where(IdentityAppearance.detection_id.isnot(None)).subquery()
    faces = select(Face.identity_id, Face.detection_id, func.count().label('links')).group_by(
        Face.identity_id, Face.detection_id).subquery()
    filters = [Identity.merged_into_id.is_(None),
        Identity.status.in_([IdentityStatus.ACTIVE, IdentityStatus.PROMOTED, IdentityStatus.INACTIVE]),
        IdentityAppearance.id <= context['watermark'], vectors.c.links == 1,
        appearances.c.links == 1, func.coalesce(faces.c.links, 0) <= 1,
        vectors.c.distance >= -0.000001, vectors.c.distance <= 1 - context['threshold']]
    scope = context['scope']
    if scope not in ('both', 'known', 'unknown'):
        raise HTTPException(422, 'Choose a valid search scope.')
    if scope != 'both':
        filters.append(Identity.type == (IdentityType.KNOWN if scope == 'known' else IdentityType.UNKNOWN))
    if context['exclude_ids']:
        filters.append(Identity.id.notin_([UUID(value) for value in context['exclude_ids']]))
    values = context['filters']
    if values.get('date_from'):
        filters.append(IdentityAppearance.start_time >= datetime.fromisoformat(values['date_from']))
    if values.get('date_to'):
        filters.append(IdentityAppearance.start_time <= datetime.fromisoformat(values['date_to']))
    if values.get('pipeline_id'):
        filters.append(IdentityAppearance.pipeline_id == values['pipeline_id'])
    return select(IdentityAppearance.id.label('appearance_id'), vectors.c.embedding_id,
        (1 - vectors.c.distance).label('similarity'), IdentityAppearance.start_time,
        IdentityAppearance.pipeline_id, func.coalesce(faces.c.links, 0).label('face_count'))\
        .join(Identity, Identity.id == IdentityAppearance.identity_id)\
        .join(vectors, and_(vectors.c.identity_id == IdentityAppearance.identity_id,
            vectors.c.detection_id == IdentityAppearance.detection_id,
            vectors.c.pipeline_id == IdentityAppearance.pipeline_id))\
        .join(appearances, appearances.c.appearance_id == IdentityAppearance.id)\
        .outerjoin(faces, and_(faces.c.identity_id == IdentityAppearance.identity_id,
            faces.c.detection_id == IdentityAppearance.detection_id))\
        .where(*filters).cte('matching_appearances')


async def search_appearances(db, context, face_index, *, after_id=None, limit=50):
    if not 1 <= limit <= 100:
        raise HTTPException(422, 'Page size must be between 1 and 100.')
    candidates = appearance_candidates(context, face_index)
    total, cameras = (await db.execute(select(func.count(),
        func.count(func.distinct(candidates.c.pipeline_id))).select_from(candidates))).one()
    statement = select(IdentityAppearance, Identity, IdentityEmbedding,
        candidates.c.similarity, candidates.c.face_count, Pipeline.location_name)\
        .select_from(candidates)\
        .join(IdentityAppearance, IdentityAppearance.id == candidates.c.appearance_id)\
        .join(Identity, Identity.id == IdentityAppearance.identity_id)\
        .join(IdentityEmbedding, IdentityEmbedding.id == candidates.c.embedding_id)\
        .outerjoin(Pipeline, Pipeline.pipeline_id == IdentityAppearance.pipeline_id)
    if after_id is not None:
        cursor = (await db.execute(select(candidates.c.start_time, candidates.c.appearance_id)
            .where(candidates.c.appearance_id == after_id))).first()
        if not cursor:
            raise HTTPException(409, 'This appearance page changed. Reopen the appearance view or rerun search.')
        statement = statement.where(tuple_(candidates.c.start_time, candidates.c.appearance_id) > tuple_(*cursor))
    rows = (await db.execute(statement.order_by(candidates.c.start_time,
        candidates.c.appearance_id).limit(limit + 1))).all()
    more, rows = len(rows) > limit, rows[:limit]
    evidence = await hydrate_evidence(db, [(person, embedding, score)
        for app, person, embedding, score, face_count, camera in rows],
        _parse_vector(context['vectors'][str(face_index)]), context['model'],
        reference_threshold=context['threshold'])
    items = []
    for row, hit in zip(rows, evidence):
        app, person, embedding, score, face_count, camera = row
        # Never substitute a profile portrait or query photo for event evidence.
        # If Face was removed but the unique appearance link survives, its own
        # snapshot is still valid. Ambiguous Face links were excluded above.
        path = hit.snapshot_path if hit.appearance_id == app.id else (
            app.best_snapshot_path if face_count == 0 else None)
        item = hit.result()
        item.update(id=app.id, start_time=app.start_time.isoformat() + 'Z',
            pipeline_id=app.pipeline_id, location_name=app.location_name or camera or app.pipeline_id,
            matched_appearance_id=app.id, matched_at=app.start_time.isoformat() + 'Z',
            matched_pipeline_id=app.pipeline_id, match_source='sighting',
            best_snapshot_path=None, snapshot_url=path_to_url(path, settings.STORAGE_DIR) if path else None)
        items.append(item)
    return {'appearances': items, 'total': total, 'cameras': cameras,
        'has_more': more, 'next_after_id': rows[-1][0].id if more else None,
        'threshold': context['threshold'], 'filters': context['filters'], 'scope': context['scope'],
        'coverage': 'Direct matches to the uploaded face among recorded sightings with a usable, uniquely linked embedding. Missing or incompatible evidence cannot be verified. Similarity is not proof of identity.'}

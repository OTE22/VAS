"""Paged direct-query search plus explicit, one-hop operator-confirmed sightings.

The original query lives in the existing Redis service for 30 minutes, bound to
the requesting admin. Only an opaque ID goes to the browser. No new DB objects.
"""
from datetime import datetime
from uuid import UUID, uuid4

from fastapi import HTTPException
from sqlalchemy import select, func, and_, or_, tuple_, literal, union_all, cast, String, Integer, Float

from config import settings
from db_models import Identity, IdentityEmbedding, IdentityAppearance, IdentityStatus, IdentityType, Face, Pipeline, IdentityAuditLog
from backend.core.merge_compatibility import _parse_vector
from backend.core.redis_cache import redis_cache_service
from backend.core.search_evidence import hydrate_evidence
from backend.utils.path_utils import path_to_url
from backend.core.sighting_review import ACTION, proof_matches

CONTEXT_TTL = 1800
CONTEXT_VERSION = 2


async def review_revision(db):
    return await db.scalar(select(func.coalesce(func.max(IdentityAuditLog.id), 0)).where(
        IdentityAuditLog.action_type == ACTION))


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
        'version': CONTEXT_VERSION, 'review_revision': await review_revision(db),
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
    if (context.get('version') != CONTEXT_VERSION or context['model'] != current_model() or
            context['threshold'] != float(settings.SIMILARITY_THRESHOLD)):
        raise HTTPException(409, 'Search or recognition settings changed. Run the image search again.')
    if str(face_index) not in context['vectors']:
        raise HTTPException(404, 'This face was not searchable in the submitted image.')
    return context


def appearance_candidates(context, face_index, *, apply_display_filters=True):
    """One usable vector per sighting, including Known and Unknown groups.

    Count links before the similarity filter: two vectors cannot become an
    apparently unique link merely because only one passed the threshold.
    Automatic group membership never broadens the query. Review anchors may
    precede the selected date range or come from a different camera.
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
    filters = appearance_filters(context, apply_display_filters=apply_display_filters) + [
        vectors.c.links == 1, appearances.c.links == 1, func.coalesce(faces.c.links, 0) <= 1,
        vectors.c.distance >= -0.000001, vectors.c.distance <= 1 - context['threshold']]
    return select(IdentityAppearance.id.label('appearance_id'), IdentityAppearance.identity_id, vectors.c.embedding_id,
        (1 - vectors.c.distance).label('similarity'), IdentityAppearance.start_time,
        IdentityAppearance.pipeline_id, func.coalesce(faces.c.links, 0).label('face_count'))\
        .join(Identity, Identity.id == IdentityAppearance.identity_id)\
        .join(vectors, and_(vectors.c.identity_id == IdentityAppearance.identity_id,
            vectors.c.detection_id == IdentityAppearance.detection_id,
            vectors.c.pipeline_id == IdentityAppearance.pipeline_id))\
        .join(appearances, appearances.c.appearance_id == IdentityAppearance.id)\
        .outerjoin(faces, and_(faces.c.identity_id == IdentityAppearance.identity_id,
            faces.c.detection_id == IdentityAppearance.detection_id))\
        .where(*filters).cte('matching_appearances' if apply_display_filters else 'review_anchors')


def appearance_filters(context, *, apply_display_filters=True):
    filters = [Identity.merged_into_id.is_(None),
        Identity.status.in_([IdentityStatus.ACTIVE, IdentityStatus.PROMOTED, IdentityStatus.INACTIVE]),
        IdentityAppearance.id <= context['watermark']]
    scope = context['scope']
    if scope not in ('both', 'known', 'unknown'):
        raise HTTPException(422, 'Choose a valid search scope.')
    if scope != 'both':
        filters.append(Identity.type == (IdentityType.KNOWN if scope == 'known' else IdentityType.UNKNOWN))
    if context['exclude_ids']:
        filters.append(Identity.id.notin_([UUID(value) for value in context['exclude_ids']]))
    values = context['filters'] if apply_display_filters else {}
    if values.get('date_from'):
        filters.append(IdentityAppearance.start_time >= datetime.fromisoformat(values['date_from']))
    if values.get('date_to'):
        filters.append(IdentityAppearance.start_time <= datetime.fromisoformat(values['date_to']))
    if values.get('pipeline_id'):
        filters.append(IdentityAppearance.pipeline_id == values['pipeline_id'])
    return filters


async def confirmed_expansion(db, context, face_index):
    """Explicit pairs only. An added sighting never becomes another search seed.

    Load only latest decisions touching a direct query match. Proof validation
    shares the journey contract, before filtering/counting/pagination. No photo
    reads, vector rewriting or learning happens during search.
    """
    anchors = appearance_candidates(context, face_index, apply_display_filters=False)
    details = IdentityAuditLog.action_details
    latest = select(func.max(IdentityAuditLog.id).label('id')).where(
        IdentityAuditLog.action_type == ACTION, IdentityAuditLog.success.is_(True),
        IdentityAuditLog.identity_id.in_(select(anchors.c.identity_id).distinct())
    ).group_by(IdentityAuditLog.identity_id, details['pair']).subquery()
    records = (await db.execute(select(IdentityAuditLog, anchors.c.appearance_id,
        anchors.c.similarity).join(latest, latest.c.id == IdentityAuditLog.id)
        .join(anchors, and_(anchors.c.identity_id == IdentityAuditLog.identity_id,
            or_(details['pair'][0].astext == cast(anchors.c.appearance_id, String),
                details['pair'][1].astext == cast(anchors.c.appearance_id, String))))
        .where(details['decision'].astext.in_(['confirmed', 'rejected'])))).all()
    pairs = []
    for record, reference_id, score in records:
        data = record.action_details or {}
        pair = data.get('pair')
        if (data.get('version') != 1 or not isinstance(pair, list) or len(pair) != 2
                or not all(type(i) is int and i > 0 for i in pair) or pair[0] == pair[1]
                or reference_id not in pair):
            continue
        pairs.append((record, reference_id, score, pair))
    if not pairs:
        return {}
    ids = {i for _, _, _, pair in pairs for i in pair}
    apps = {a.id: a for a in (await db.scalars(select(IdentityAppearance).where(
        IdentityAppearance.id.in_(ids)))).all()}
    confirmed, rejected = {}, set()
    for record, reference_id, score, pair in pairs:
        reference = apps.get(reference_id)
        target_id = pair[1] if pair[0] == reference_id else pair[0]
        target = apps.get(target_id)
        data = record.action_details
        saved = {p.get('id'): p for p in data.get('sightings', []) if isinstance(p, dict)}
        if not reference or not target or any(a.identity_id != record.identity_id or
                a.id > context['watermark'] or a.id not in saved or not proof_matches(saved[a.id], a)
                for a in (reference, target)):
            continue
        if data['decision'] == 'rejected':
            rejected.add(target_id)
            continue
        info = dict(decision='confirmed', reviewer=record.username,
            reviewed_at=record.created_at.isoformat() + 'Z', revision=record.id,
            reference_appearance_id=reference_id, reference_similarity=float(score),
            reference_pipeline_id=reference.pipeline_id,
            reference_at=reference.start_time.isoformat() + 'Z')
        existing = confirmed.get(target_id)
        if not existing or (-info['reference_similarity'], reference_id) < (
                -existing['reference_similarity'], existing['reference_appearance_id']):
            confirmed[target_id] = info
    # Contradictory manual links must not broaden the query. A direct match to
    # the uploaded image remains independent of any pairwise review decision.
    return {key: value for key, value in confirmed.items() if key not in rejected}


def operator_result(app, person, camera, review):
    return dict(identity_id=str(person.id), id=app.id,
        type='unverified' if person.type == IdentityType.KNOWN else 'unknown', display_name=None,
        similarity=None, matched_embedding_id=None, identity_verification='operator_confirmed_association',
        match_basis='operator_confirmed', association_status='operator_confirmed', manual_review=review,
        start_time=app.start_time.isoformat() + 'Z', pipeline_id=app.pipeline_id,
        location_name=app.location_name or camera or app.pipeline_id,
        matched_appearance_id=app.id, matched_at=app.start_time.isoformat() + 'Z',
        matched_pipeline_id=app.pipeline_id, match_source='operator_review', best_snapshot_path=None,
        snapshot_url=path_to_url(app.best_snapshot_path, settings.STORAGE_DIR) if app.best_snapshot_path else None)


async def search_appearances(db, context, face_index, *, after_id=None, limit=50):
    if not 1 <= limit <= 100:
        raise HTTPException(422, 'Page size must be between 1 and 100.')
    revision = await review_revision(db)
    if context.get('review_revision', revision) != revision:
        raise HTTPException(409, 'Sighting review decisions changed. Run the image search again to refresh results.')
    direct = appearance_candidates(context, face_index)
    confirmed = await confirmed_expansion(db, context, face_index) if revision else {}
    # Both branches use the same output filters and chronological cursor.
    manual = select(IdentityAppearance.id.label('appearance_id'),
        literal(None, Integer).label('embedding_id'), literal(None, Float).label('similarity'),
        IdentityAppearance.start_time, IdentityAppearance.pipeline_id,
        literal(None, Integer).label('face_count'), literal('operator_confirmed').label('basis'))\
        .join(Identity, Identity.id == IdentityAppearance.identity_id)\
        .where(IdentityAppearance.id.in_(list(confirmed)), *appearance_filters(context),
            ~select(direct.c.appearance_id).where(direct.c.appearance_id == IdentityAppearance.id).exists())
    candidates = union_all(select(direct.c.appearance_id, direct.c.embedding_id, direct.c.similarity,
        direct.c.start_time, direct.c.pipeline_id, direct.c.face_count, literal('direct').label('basis')),
        manual).cte('result_appearances')
    total, cameras, direct_count = (await db.execute(select(func.count(),
        func.count(func.distinct(candidates.c.pipeline_id)),
        func.count().filter(candidates.c.basis == 'direct')).select_from(candidates))).one()
    statement = select(IdentityAppearance, Identity, IdentityEmbedding,
        candidates.c.similarity, candidates.c.face_count, Pipeline.location_name, candidates.c.basis)\
        .select_from(candidates)\
        .join(IdentityAppearance, IdentityAppearance.id == candidates.c.appearance_id)\
        .join(Identity, Identity.id == IdentityAppearance.identity_id)\
        .outerjoin(IdentityEmbedding, IdentityEmbedding.id == candidates.c.embedding_id)\
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
    direct_rows = [row for row in rows if row[-1] == 'direct']
    evidence = await hydrate_evidence(db, [(row[1], row[2], row[3]) for row in direct_rows],
        _parse_vector(context['vectors'][str(face_index)]), context['model'],
        reference_threshold=context['threshold'])
    hits = {row[0].id: hit for row, hit in zip(direct_rows, evidence)}
    items = []
    for app, person, embedding, score, face_count, camera, basis in rows:
        if basis == 'operator_confirmed':
            items.append(operator_result(app, person, camera, confirmed[app.id]))
            continue
        hit = hits[app.id]
        # Never substitute a profile portrait or query photo for event evidence.
        path = hit.snapshot_path if hit.appearance_id == app.id else (
            app.best_snapshot_path if face_count == 0 else None)
        item = hit.result()
        item.update(id=app.id, start_time=app.start_time.isoformat() + 'Z',
            pipeline_id=app.pipeline_id, location_name=app.location_name or camera or app.pipeline_id,
            matched_appearance_id=app.id, matched_at=app.start_time.isoformat() + 'Z',
            matched_pipeline_id=app.pipeline_id, match_source='sighting', match_basis='direct',
            association_status='direct_match',
            best_snapshot_path=None, snapshot_url=path_to_url(path, settings.STORAGE_DIR) if path else None)
        items.append(item)
    if await review_revision(db) != revision:
        raise HTTPException(409, 'Sighting review decisions changed. Run the image search again to refresh results.')
    return {'appearances': items, 'total': total, 'cameras': cameras,
        'direct_count': direct_count, 'operator_confirmed_count': total - direct_count,
        'has_more': more, 'next_after_id': rows[-1][0].id if more else None,
        'threshold': context['threshold'], 'filters': context['filters'], 'scope': context['scope'],
        'coverage': 'Direct image matches meet the similarity threshold. Operator-confirmed sightings are separately labeled and linked to a direct match, which may be outside the selected date or camera. Unreviewed group membership, conflicting reviews and chains of confirmations do not add results. Similarity is not proof of identity.'}

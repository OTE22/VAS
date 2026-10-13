"""Authoritative camera-specific occurrence payloads shared by HTTP and WebSocket."""
from sqlalchemy import select, func
from db_models import IdentityAppearance
from config import settings
from backend.utils.path_utils import path_to_url


def event_payload(appearance):
    return {
        'event_id': appearance.event_id or f'appearance:{appearance.id}',
        'appearance_id': appearance.id,
        'detection_id': appearance.detection_id,
        'detection_uuid': appearance.detection_uuid,
        'identity_id': str(appearance.identity_id),
        'pipeline_id': appearance.pipeline_id,
        'location_name': appearance.location_name,
        'timestamp': appearance.start_time.isoformat() + 'Z',
        'timestamp_source': appearance.timestamp_source or 'legacy_server',
        'snapshot_path': appearance.best_snapshot_path,
        'snapshot_url': path_to_url(appearance.best_snapshot_path, settings.STORAGE_DIR)
                        if appearance.best_snapshot_path else None,
    }


async def latest_camera_events(db, identity_ids, allowed_pipelines=None, *, date_from=None, date_to=None):
    if not identity_ids:
        return {}
    conditions = [IdentityAppearance.identity_id.in_(identity_ids)]
    if allowed_pipelines is not None:
        conditions.append(IdentityAppearance.pipeline_id.in_(allowed_pipelines))
    if date_from is not None:
        conditions.append(IdentityAppearance.start_time >= date_from)
    if date_to is not None:
        conditions.append(IdentityAppearance.start_time < date_to)
    query = select(IdentityAppearance).where(*conditions)
    query = query.distinct(IdentityAppearance.identity_id, IdentityAppearance.pipeline_id).order_by(
        IdentityAppearance.identity_id, IdentityAppearance.pipeline_id,
        IdentityAppearance.start_time.desc(), IdentityAppearance.id.desc())
    result = {}
    for appearance in (await db.execute(query)).scalars():
        result.setdefault(str(appearance.identity_id), {})[appearance.pipeline_id] = event_payload(appearance)
    counts = select(IdentityAppearance.identity_id, IdentityAppearance.pipeline_id, func.count()).where(
        *conditions).group_by(IdentityAppearance.identity_id, IdentityAppearance.pipeline_id)
    for iid, pid, count in (await db.execute(counts)).all():
        if pid in result.get(str(iid), {}):
            result[str(iid)][pid]['appearances_count'] = count
    return result


async def publish_unknown_events(events):
    """Caller must have committed. Missed delivery is recoverable through HTTP/history."""
    if not events:
        return
    import logging
    try:
        from backend.core.redis_cache import redis_cache_service
        if redis_cache_service._enabled:
            await redis_cache_service.invalidate_unknown_cache(user_id=None)
            await redis_cache_service.invalidate_dashboard_cache(user_id=None)
    except Exception:
        logging.getLogger(__name__).exception('Post-commit unknown cache invalidation failed')
    from backend.core.websocket_manager import ws_manager
    for event in events:
        try:
            await ws_manager.broadcast({'type': 'new_unknown_detection', 'data': event},
                                       pipeline_id=event['pipeline_id'])
            await ws_manager.broadcast({'type': 'unknown_activity', **event,
                                        'created_at': event['timestamp']}, pipeline_id=event['pipeline_id'])
        except Exception:
            logging.getLogger(__name__).exception('Committed unknown event delivery failed')


async def review_unknown_associations(db, identity_id, appearances, selected_appearance_id=None,
                                      *, reference_embedding_id=None, threshold=None):
    """Read-only, direct comparison; never infer a person's movement by chaining.

    All rows remain in the response and in storage. A uniquely linked current-
    model vector is required before an appearance can join the displayed journey.
    No vector, ambiguous multi-face detection, or mismatched photo => unverified.
    The selected photo is context, not a newly approved recognition reference.
    """
    from collections import defaultdict
    import os
    import numpy as np
    from db_models import IdentityEmbedding, Face, Detection
    from backend.core.identity_service import IdentityService

    model = IdentityService._resolve_model_version()
    threshold = float(settings.UNKNOWN_SIMILARITY_THRESHOLD) if threshold is None else float(threshold)
    vectors = (await db.execute(select(IdentityEmbedding).where(
        IdentityEmbedding.identity_id == identity_id,
        IdentityEmbedding.embedding_model_version == model,
        IdentityEmbedding.embedding.isnot(None)
    ).order_by(IdentityEmbedding.id))).scalars().all() if model else []
    by_detection = defaultdict(list)
    for row in vectors:
        by_detection[row.detection_id].append(row)
    faces = defaultdict(list)
    for face, pipeline in (await db.execute(select(Face, Detection.pipeline_id)
            .join(Detection, Face.detection_id == Detection.id)
            .where(Face.identity_id == identity_id))).all():
        faces[face.detection_id].append((face, pipeline))

    def normalized(value):
        if value is None:
            return None
        vector = np.asarray(value, dtype=np.float32)
        if vector.shape != (512,) or not np.isfinite(vector).all():
            return None
        norm = np.linalg.norm(vector)
        return vector / norm if np.isfinite(norm) and norm > 0 else None

    linked = {}
    for app in appearances:
        candidates = by_detection.get(app.detection_id, [])
        owners = faces.get(app.detection_id, [])
        if app.detection_id is None or len(candidates) != 1 or len(owners) != 1:
            continue
        row = candidates[0]
        face, pipeline = owners[0]
        if row.pipeline_id != app.pipeline_id or pipeline != app.pipeline_id:
            continue
        if app.best_snapshot_path and (not face.face_image_path or
                os.path.normpath(app.best_snapshot_path) != os.path.normpath(face.face_image_path)):
            continue
        value = normalized(row.embedding)
        if value is not None:
            linked[app.id] = (row.id, value)

    reference_id = selected_appearance_id
    if reference_embedding_id is not None:
        row = next((row for row in vectors if row.id == reference_embedding_id), None)
        reference = normalized(row.embedding) if row else None
    elif selected_appearance_id is not None:
        reference = linked.get(selected_appearance_id, (None, None))[1]
    else:
        # Use the exact same stable reference as live recognition, not the
        # best-looking/current portrait, which can be a later misassociation.
        reference = normalized(vectors[0].embedding) if vectors else None
        if vectors:
            reference_id = next((app.id for app in appearances
                if linked.get(app.id, (None,))[0] == vectors[0].id), None)

    result = {}
    for app in appearances:
        score = None
        state = 'unverified'
        if app.id == reference_id:
            state = 'selected'
        elif reference is not None and app.id in linked:
            score = float(np.clip(np.dot(reference, linked[app.id][1]), -1., 1.))
            state = 'consistent' if score >= threshold else 'review_required'
        result[app.id] = {
            'association_status': state,
            'association_similarity': round(score, 6) if score is not None else None,
            'association_reason': {
                'selected': 'Selected reference sighting; identity is not independently verified',
                'consistent': 'Direct similarity meets the comparison threshold',
                'review_required': 'Direct similarity is below the comparison threshold',
                'unverified': 'No unambiguous current-model vector link for this comparison',
            }[state],
        }
    included = sum(r['association_status'] in ('selected', 'consistent') for r in result.values())
    return result, {
        'reference_appearance_id': reference_id,
        'reference_kind': 'search_observation' if reference_embedding_id is not None else 'selected_sighting' if selected_appearance_id is not None else 'initial_reference',
        'reference_embedding_id': reference_embedding_id,
        'reference_available': reference is not None,
        'threshold': threshold,
        'included_count': included,
        'review_count': len(appearances) - included,
        'total_count': len(appearances),
    }

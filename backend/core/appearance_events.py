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

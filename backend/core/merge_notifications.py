"""Post-commit merge notifications. Failure never undoes a durable merge."""
import logging
from sqlalchemy import select
from db_models import IdentityAppearance


async def publish_merge(db, source_ids, target_id):
    try:
        from backend.core.redis_cache import redis_cache_service
        await redis_cache_service.invalidate_unknown_cache()
        await redis_cache_service.invalidate_dashboard_cache()
    except Exception:
        logging.getLogger(__name__).exception('Merge cache invalidation failed')
    try:
        from backend.core.websocket_manager import ws_manager
        pipelines = (await db.execute(select(IdentityAppearance.pipeline_id).where(
            IdentityAppearance.identity_id == target_id).distinct())).scalars().all()
        message = {'type': 'identities_merged', 'data': {
            'source_ids': [str(i) for i in source_ids], 'target_id': str(target_id)}}
        for pipeline in pipelines:
            await ws_manager.broadcast(message, pipeline_id=pipeline)
        if not pipelines:
            await ws_manager.broadcast(message, admin_only=True)
    except Exception:
        logging.getLogger(__name__).exception('Committed merge notification failed')


async def publish_promotion(db, identity_id):
    try:
        from backend.core.redis_cache import redis_cache_service
        await redis_cache_service.invalidate_unknown_cache()
        await redis_cache_service.invalidate_dashboard_cache()
    except Exception:
        logging.getLogger(__name__).exception('Promotion cache invalidation failed')
    try:
        from backend.core.identity_pipelines import pipelines_for
        from backend.core.websocket_manager import ws_manager
        membership = await pipelines_for(db, [identity_id])
        pipelines = membership.get(identity_id, set())
        event = {'type': 'identity_promoted', 'data': {'identity_id': str(identity_id)}}
        for pipeline_id in pipelines:
            await ws_manager.broadcast(event, pipeline_id=pipeline_id)
        if not pipelines:
            await ws_manager.broadcast(event, admin_only=True)
    except Exception:
        logging.getLogger(__name__).exception('Committed promotion notification failed')

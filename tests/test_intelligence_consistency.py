"""Intelligence regressions using only disposable database records."""
import os
import pytest
from unittest.mock import AsyncMock
import asyncio
from datetime import datetime, timedelta
from uuid import uuid4
from sqlalchemy import select, delete
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from fastapi import HTTPException
from config import settings
from db_models import Identity, IdentityType, IdentityStatus, Pipeline, IdentityAppearance, IdentityRelationship
from backend.core.intelligence_service import intelligence_service as service
from backend.routes import intelligence as routes

async def exercise(monkeypatch):
    engine = create_async_engine(settings.DATABASE_URL)
    Session = async_sessionmaker(engine, expire_on_commit=False)
    ids = sorted([uuid4(), uuid4()])
    pid = 'audit_intel_' + uuid4().hex
    day = (datetime.utcnow() - timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    times = [day+timedelta(hours=12, minutes=m) for m in (0, 10, 20)] + [day+timedelta(hours=23, minutes=59, seconds=59, microseconds=500000)]
    try:
        async with Session() as db:
            db.add(Pipeline(pipeline_id=pid))
            db.add_all([Identity(id=i, type=IdentityType.KNOWN, status=IdentityStatus.ACTIVE, display_name='audit only') for i in ids])
            await db.flush()
            db.add_all([IdentityAppearance(identity_id=ids[0],pipeline_id=pid,start_time=t) for t in times])
            db.add(IdentityAppearance(identity_id=ids[1],pipeline_id=pid,start_time=times[0]))
            await db.commit()
            patterns = await service.get_temporal_patterns(db, str(ids[0]), days_back=2)
            assert patterns.daily_distribution == {day.weekday(): 4}
            tracks = await service.get_cross_camera_track(db, str(ids[0]), date=day.strftime('%Y-%m-%d'))
            assert sum(len(t.movements) for t in tracks) == 4
            await service.refresh_relationships(db, str(ids[0]), time_window_minutes=1, min_co_appearances=1)
            before = (await db.execute(select(IdentityRelationship.co_appearance_percentage).where(IdentityRelationship.identity_id_1==ids[0],IdentityRelationship.identity_id_2==ids[1]))).scalar_one()
            await service.refresh_relationships(db, str(ids[1]), time_window_minutes=1, min_co_appearances=1)
            after = (await db.execute(select(IdentityRelationship.co_appearance_percentage).where(IdentityRelationship.identity_id_1==ids[0],IdentityRelationship.identity_id_2==ids[1]))).scalar_one()
            assert before == after == 25
            # Refreshing either side repeatedly must retain the same denominator.
            await service.refresh_relationships(db, str(ids[0]), time_window_minutes=1, min_co_appearances=1)
            for name in ['RELATED_IDENTITIES_ENABLED','TEMPORAL_PATTERNS_ENABLED','CROSS_CAMERA_TRACKING_ENABLED']:
                monkeypatch.setattr(settings,name,False)
            try:
                await routes.get_temporal_patterns(str(ids[0]), days_back=2,db=db,current_user={'id':1})
            except HTTPException as exc:
                assert exc.status_code == 403
            else:
                raise AssertionError('Direct endpoint should respect feature flag')
            spies = {}
            for method in ['get_related_identities', 'get_temporal_patterns', 'get_cross_camera_track']:
                spies[method] = AsyncMock()
                monkeypatch.setattr(service, method, spies[method])
            complete = await routes.analyze_identity(str(ids[0]),db=db,current_user={'id':1})
            assert all(section['status'] == 'disabled' for section in complete['sections'].values())
            with pytest.raises(HTTPException) as error:
                await routes.get_tracking_map_data(str(ids[0]), db=db, current_user={'id': 1})
            assert error.value.status_code == 403
            for spy in spies.values():
                spy.assert_not_awaited()
            monkeypatch.setattr(settings, 'TEMPORAL_PATTERNS_ENABLED', True)
            spies['get_temporal_patterns'].return_value = patterns
            complete = await routes.analyze_identity(str(ids[0]), db=db, current_user={'id': 1})
            assert complete['sections']['temporal']['status'] == 'ready'
            assert complete['sections']['related']['status'] == 'disabled'
            assert complete['sections']['tracking']['status'] == 'disabled'
            spies['get_temporal_patterns'].assert_awaited_once()
    finally:
        async with Session() as db:
            await db.execute(delete(Identity).where(Identity.id.in_(ids)))
            await db.execute(delete(Pipeline).where(Pipeline.pipeline_id==pid))
            await db.commit()
        await engine.dispose()

@pytest.mark.skipif(not os.environ.get('REGRESSION_ISOLATION_ID'),
                    reason='Requires disposable regression database')
def test_intelligence_boundaries_percentages_and_feature_settings(monkeypatch):
    asyncio.run(exercise(monkeypatch))

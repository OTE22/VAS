import asyncio
import uuid
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession

from config import settings
from db_models import Identity, IdentityType, IdentityStatus, Pipeline, IdentityAppearance
from backend.core.intelligence_service import IntelligenceService


@pytest.mark.parametrize('cross_camera', [False, True])
def test_filters_change_real_query_results_even_when_cache_exists(monkeypatch, cross_camera):
    from backend.core.threshold_store import threshold_store
    monkeypatch.setattr(settings, 'MULTI_CAMERA_CO_APPEARANCE_ENABLED', cross_camera)
    monkeypatch.setattr(settings, 'MULTI_CAMERA_MIN_CO_APPEARANCES', 1)
    monkeypatch.setattr(settings, 'ACTIVITY_CORRELATION_ENABLED', False)
    monkeypatch.setattr(threshold_store, 'resolve', AsyncMock(
        return_value=SimpleNamespace(value=30, provenance='test')))
    service = IntelligenceService()
    cache = AsyncMock(return_value=['stale unfiltered cache'])
    monkeypatch.setattr(service, '_get_cached_relationships', cache)
    async def exercise():
        engine = create_async_engine(settings.DATABASE_URL)
        try:
            async with engine.connect() as connection:
                transaction = await connection.begin()
                try:
                    async with AsyncSession(bind=connection, expire_on_commit=False) as db:
                        target = Identity(type=IdentityType.UNKNOWN, status=IdentityStatus.ACTIVE)
                        other = Identity(type=IdentityType.UNKNOWN, status=IdentityStatus.ACTIVE)
                        a, b = 'filter-a-' + uuid.uuid4().hex, 'filter-b-' + uuid.uuid4().hex
                        db.add_all([target, other, Pipeline(pipeline_id=a), Pipeline(pipeline_id=b)])
                        await db.flush()
                        base = datetime(2026, 1, 1)
                        db.add(IdentityAppearance(identity_id=target.id, pipeline_id=a, start_time=base))
                        for minutes in [3, 7]:
                            db.add(IdentityAppearance(identity_id=other.id, pipeline_id=b if cross_camera else a,
                                                      start_time=base + timedelta(minutes=minutes)))
                        await db.flush()
                        monkeypatch.setattr(service, '_get_pipeline_coordinates_cache', AsyncMock(
                            return_value={a: (33.0, 35.0), b: (33.0, 35.0)}))
                        async def query(minimum, window):
                            return await service.get_related_identities(db, str(target.id), minimum, window)
                        assert await query(1, 1) == []
                        assert [r.co_appearance_count for r in await query(1, 5)] == [1]
                        assert [r.co_appearance_count for r in await query(1, 10)] == [2]
                        assert await query(3, 10) == []
                        cache.assert_not_awaited()
                finally:
                    await transaction.rollback()
        finally:
            await engine.dispose()
    asyncio.run(exercise())


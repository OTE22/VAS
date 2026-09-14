"""Camera-specific sightings, replay and late arrival. All DB writes roll back."""
import asyncio
import importlib
import uuid
from datetime import datetime

from sqlalchemy import select, delete
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from config import settings
import db_models as m
import pytest


def test_independent_camera_occurrences_and_late_arrival(monkeypatch):
    async def exercise():
        from backend.core.detection_evidence import persist_detection
        from backend.core.appearance_events import latest_camera_events
        service = importlib.import_module('backend.core.identity_service')
        monkeypatch.setattr(service, 'identity_service', service.IdentityService(None))
        monkeypatch.setattr(settings, 'LIVE_ALERTS_ENABLED', False)
        monkeypatch.setattr(settings, 'WATCHLIST_ENABLED', False)
        engine = create_async_engine(settings.DATABASE_URL)
        try:
            async with engine.connect() as connection:
                outer = await connection.begin()
                try:
                    async with AsyncSession(bind=connection, expire_on_commit=False,
                                            join_transaction_mode='create_savepoint') as db:
                        a, b = [m.Identity(type=m.IdentityType.UNKNOWN, status=m.IdentityStatus.ACTIVE) for _ in range(2)]
                        c1, c4 = [m.Pipeline(pipeline_id='occurrence-qa-' + uuid.uuid4().hex) for _ in range(2)]
                        db.add_all([a, b, c1, c4])
                        await db.flush()
                        outcomes = []
                        for person, camera, time in [(a,c1,'10:15:32'), (a,c4,'10:21:07'),
                                                     (a,c1,'10:45:18'), (b,c1,'10:45:19'),
                                                     (a,c1,'10:17:00')]:
                            stamp = datetime.fromisoformat('2026-09-13T' + time)
                            data = {'pipeline_id': camera.pipeline_id, 'location_name': 'Original location',
                                'timestamp_source': 'server_received',
                                'detection': {'uuid': str(uuid.uuid4()), 'pipeline_id': camera.pipeline_id, 'timestamp': stamp},
                                'faces': [{'name': 'Unknown', 'identity_id': person.id, 'similarity': .8,
                                           'label_state': 'auto_unknown', '_event_id': uuid.uuid4().hex}]}
                            result = await persist_detection(db, detection_data=data)
                            await db.commit()
                            outcomes.append(result)
                            replay = await persist_detection(db, detection_data=data)
                            assert replay.detection_id == result.detection_id and replay.unknown_events == []
                        events = await latest_camera_events(db, [a.id, b.id])
                        assert events[str(a.id)][c1.pipeline_id]['timestamp'].endswith('10:45:18Z')
                        assert events[str(a.id)][c4.pipeline_id]['timestamp'].endswith('10:21:07Z')
                        assert events[str(b.id)][c1.pipeline_id]['timestamp'].endswith('10:45:19Z')
                        assert events[str(a.id)][c1.pipeline_id]['appearances_count'] == 3
                        await db.refresh(a)
                        assert a.last_seen_at == datetime(2026,9,13,10,45,18)
                        appearances = (await db.execute(select(m.IdentityAppearance).where(
                            m.IdentityAppearance.identity_id.in_([a.id,b.id])))).scalars().all()
                        assert len(appearances) == len({x.event_id for x in appearances}) == 5
                        assert all(x.detection_id and x.detection_uuid for x in appearances)
                        c1.location_name = 'Renamed later'
                        await db.flush()
                        assert all(x.location_name == 'Original location' for x in appearances)
                        # Detection retention cannot erase appearance provenance.
                        old = appearances[0]
                        old_uuid = old.detection_uuid
                        await db.execute(delete(m.Detection).where(m.Detection.id == old.detection_id))
                        await db.flush()
                        await db.refresh(old)
                        assert old.detection_id is None and old.detection_uuid == old_uuid and old.event_id
                        scoped = await latest_camera_events(db, [a.id,b.id], [c4.pipeline_id])
                        assert set(scoped[str(a.id)]) == {c4.pipeline_id}
                        assert str(b.id) not in scoped
                finally:
                    await outer.rollback()
        finally:
            await engine.dispose()
    asyncio.run(exercise())


def test_camera_time_normalization_and_receipt_fallback():
    from backend.core.event_time import observation_time
    received = datetime(2026, 9, 13, 12, 0, 0)
    assert observation_time(None, received) == (received, 'server_received')
    assert observation_time('2026-09-13T15:00:00+03:00', received) == (received, 'camera_reported')
    with pytest.raises(ValueError, match='timezone'):
        observation_time('2026-09-13T15:00:00', received)

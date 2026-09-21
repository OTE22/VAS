"""Run only with the isolated regression database; creates and removes its own rows."""
import asyncio
import os

import pytest
from datetime import datetime, timedelta
from uuid import uuid4
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from config import settings
from db_models import Identity, IdentityImage, IdentityType, IdentityStatus
from backend.routes.known_faces import list_known_faces
from backend.core.enrollment_service import set_primary_image, EnrollmentError

async def exercise_photo_changes():
    engine = create_async_engine(settings.DATABASE_URL)
    Session = async_sessionmaker(engine, expire_on_commit=False)
    ids = [uuid4(), uuid4()]
    now = datetime.utcnow()
    prefix = 'audit_probe_' + uuid4().hex
    try:
        async with Session() as db:
            db.add_all([
                Identity(id=ids[0], type=IdentityType.KNOWN, status=IdentityStatus.ACTIVE,
                         display_name=prefix + ' unseen', appearances_count=0, last_seen_at=now),
                Identity(id=ids[1], type=IdentityType.KNOWN, status=IdentityStatus.ACTIVE,
                         display_name=prefix + ' seen', appearances_count=1,
                         last_seen_at=now - timedelta(days=1)),
            ])
            await db.flush()
            photos = [IdentityImage(identity_id=ids[0],
                storage_path=f'storage/faces/{ids[0]}/image_{i}.jpg', file_checksum=str(i) * 64,
                is_primary=i == 1, processing_status='completed') for i in range(1, 4)]
            db.add_all(photos)
            await db.commit()
            result = await list_known_faces(q=prefix, status='current', sort='last_seen',
                                            page=1, page_size=24, db=db)
            assert [p['last_seen_at'] is not None for p in result['items']] == [True, False]
        a, b = Session(), Session()
        original_a, original_b = a.execute, b.execute
        a_demoted = asyncio.Event()
        b_started = asyncio.Event()
        async def execute_a(statement, *args, **kwargs):
            result = await original_a(statement, *args, **kwargs)
            if (getattr(statement, 'is_update', False)
                    and statement.compile().params.get('is_primary') is False):
                # Hold the first transaction after demotion until the second has started.
                a_demoted.set()
                await b_started.wait()
                await asyncio.sleep(.3)
            return result
        async def execute_b(statement, *args, **kwargs):
            b_started.set()
            return await original_b(statement, *args, **kwargs)
        a.execute, b.execute = execute_a, execute_b
        async def run(db, image_id):
            try:
                return await set_primary_image(db, str(ids[0]), image_id)
            except EnrollmentError as exc:
                return {'error': exc.code, 'status': exc.status_code}
        async with a, b:
            first = asyncio.create_task(run(a, photos[1].id))
            await asyncio.wait_for(a_demoted.wait(), 10)
            second = asyncio.create_task(run(b, photos[2].id))
            outcomes = await asyncio.wait_for(asyncio.gather(first, second), 10)
            assert all(r.get('is_primary') for r in outcomes), outcomes
        async with Session() as db:
            primaries = (await db.execute(select(IdentityImage.id).where(
                IdentityImage.identity_id == ids[0], IdentityImage.is_primary.is_(True)
            ))).scalars().all()
            assert primaries == [photos[2].id]
    finally:
        async with Session() as db:
            await db.execute(delete(Identity).where(Identity.id.in_(ids)))
            await db.commit()
        await engine.dispose()


@pytest.mark.skipif(not os.environ.get('REGRESSION_ISOLATION_ID'),
                    reason='Requires disposable regression database')
def test_last_seen_order_and_concurrent_primary_changes():
    asyncio.run(exercise_photo_changes())

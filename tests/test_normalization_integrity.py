"""Populated checks against a disposable copy of the deployed PostgreSQL schema.

Run only with NORMALIZATION_TEST_DATABASE=1 and a disposable database URL.
These safeguards must pass after the operational-integrity migration.
"""
import os
import uuid
from datetime import datetime

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from config import settings
import db_models as m

pytestmark = pytest.mark.skipif(
    os.environ.get('NORMALIZATION_TEST_DATABASE') != '1',
    reason='Requires an explicitly isolated normalization test database',
)


@pytest.fixture
def db():
    url = make_url(settings.DATABASE_URL).set(drivername='postgresql+psycopg2')
    assert url.host == '127.0.0.1' and url.database == 'normalization_qa'
    engine = create_engine(url)
    with engine.connect() as connection:
        transaction = connection.begin()
        with Session(connection, expire_on_commit=False, join_transaction_mode='create_savepoint') as session:
            yield session
        transaction.rollback()
    engine.dispose()


def person(db):
    row = m.Identity(type=m.IdentityType.UNKNOWN, status=m.IdentityStatus.ACTIVE)
    db.add(row)
    db.flush()
    return row


def camera(db):
    row = m.Pipeline(pipeline_id='qa-' + uuid.uuid4().hex)
    db.add(row)
    db.flush()
    return row


def photo(db, owner):
    row = m.IdentityImage(identity_id=owner.id, storage_path='storage/faces/qa.jpg',
                          file_checksum=uuid.uuid4().hex * 2)
    db.add(row)
    db.flush()
    return row


def test_real_foreign_key_rejects_unknown_camera(db):
    db.add(m.Detection(pipeline_id='missing-camera'))
    with pytest.raises(IntegrityError):
        db.flush()


def test_camera_with_detection_cannot_be_deleted(db):
    cam = camera(db)
    db.add(m.Detection(pipeline_id=cam.pipeline_id))
    db.flush()
    with pytest.raises(IntegrityError):
        db.execute(text('DELETE FROM pipelines WHERE id=:id'), {'id': cam.id})


def test_watchlist_membership_is_unique(db):
    owner = person(db)
    watchlist = m.Watchlist(name='qa-' + uuid.uuid4().hex)
    db.add(watchlist)
    db.flush()
    db.add_all([m.WatchlistEntry(watchlist_id=watchlist.id, identity_id=owner.id) for _ in range(2)])
    with pytest.raises(IntegrityError):
        db.flush()


def test_only_one_primary_photo_per_identity(db):
    owner = person(db)
    a, b = photo(db, owner), photo(db, owner)
    a.is_primary = b.is_primary = True
    with pytest.raises(IntegrityError):
        db.flush()


def test_alert_detection_trigger_is_idempotent(db):
    cam, owner = camera(db), person(db)
    detection = m.Detection(pipeline_id=cam.pipeline_id)
    alert = m.LiveSearchAlert(name='QA', identity_id=owner.id)
    db.add_all([detection, alert])
    db.flush()
    db.add_all([m.LiveAlertTrigger(alert_id=alert.id, detection_id=detection.id,
                                 pipeline_id=cam.pipeline_id) for _ in range(2)])
    with pytest.raises(IntegrityError):
        db.flush()


def test_detection_retention_preserves_camera_provenance(db):
    cam, owner = camera(db), person(db)
    detection = m.Detection(pipeline_id=cam.pipeline_id)
    db.add(detection)
    db.flush()
    appearance = m.IdentityAppearance(identity_id=owner.id, pipeline_id=cam.pipeline_id,
                                      detection_id=detection.id, detection_uuid=detection.uuid,
                                      start_time=datetime.utcnow())
    db.add(appearance)
    db.flush()
    previous_uuid = detection.uuid
    db.execute(text('DELETE FROM detections WHERE id=:id'), {'id': detection.id})
    db.refresh(appearance)
    assert appearance.detection_id is None
    assert appearance.pipeline_id == cam.pipeline_id
    assert appearance.detection_uuid == previous_uuid


def test_image_deletion_preserves_embedding_owner(db):
    owner = person(db)
    image = photo(db, owner)
    embedding = m.IdentityEmbedding(identity_id=owner.id, image_id=image.id)
    db.add(embedding)
    db.flush()
    db.execute(text('DELETE FROM identity_images WHERE id=:id'), {'id': image.id})
    db.refresh(embedding)
    assert embedding.image_id is None
    assert embedding.identity_id == owner.id


def test_identity_deletion_removes_its_images_embeddings_and_alerts(db):
    owner = person(db)
    image = photo(db, owner)
    embedding = m.IdentityEmbedding(identity_id=owner.id, image_id=image.id)
    alert = m.LiveSearchAlert(name='QA', identity_id=owner.id)
    db.add_all([embedding, alert])
    db.flush()
    db.execute(text('DELETE FROM identities WHERE id=:id'), {'id': owner.id})
    for table in (m.IdentityImage, m.IdentityEmbedding, m.LiveSearchAlert):
        assert db.execute(select(table.id).where(table.identity_id == owner.id)).first() is None


def test_alert_camera_list_rejects_missing_camera(db):
    owner = person(db)
    db.add(m.LiveSearchAlert(name='QA', identity_id=owner.id, pipeline_ids=['missing-camera']))
    with pytest.raises(IntegrityError):
        db.flush()


def test_alert_weekdays_reject_out_of_range_values(db):
    owner = person(db)
    db.add(m.LiveSearchAlert(name='QA', identity_id=owner.id, active_days=[9]))
    with pytest.raises(IntegrityError):
        db.flush()


def test_embedding_cannot_reference_another_identity_photo(db):
    a, b = person(db), person(db)
    image = photo(db, a)
    db.add(m.IdentityEmbedding(identity_id=b.id, image_id=image.id))
    with pytest.raises(IntegrityError):
        db.flush()
        db.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))


def test_merge_suggestion_rejects_nonexistent_members(db):
    db.add(m.MergeSuggestion(identity_ids=[str(uuid.uuid4()), str(uuid.uuid4())], confidence=0.9))
    with pytest.raises(IntegrityError):
        db.flush()


def test_multiple_embeddings_per_image_are_currently_permitted(db):
    """Characterize cardinality; business policy must decide if it is a defect."""
    owner = person(db)
    image = photo(db, owner)
    db.add_all([m.IdentityEmbedding(identity_id=owner.id, image_id=image.id) for _ in range(2)])
    db.flush()
    assert len(db.execute(select(m.IdentityEmbedding.id).where(m.IdentityEmbedding.image_id == image.id)).all()) == 2


async def with_async_database(work):
    from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
    url = make_url(settings.DATABASE_URL)
    assert url.host == '127.0.0.1' and url.database == 'normalization_qa'
    engine = create_async_engine(url)
    try:
        async with engine.connect() as connection:
            transaction = await connection.begin()
            try:
                async with AsyncSession(connection, expire_on_commit=False,
                                        join_transaction_mode='create_savepoint') as session:
                    await work(session)
            finally:
                await transaction.rollback()
    finally:
        await engine.dispose()


@pytest.mark.parametrize('invalid', [{'pipeline_ids': ['missing-camera']}, {'active_days': [9]}])
def test_alert_creation_service_rejects_invalid_configuration(invalid):
    from conftest import run_on_shared_loop as run
    from backend.core.live_alert_service import live_alert_service
    async def work(db):
        owner = m.Identity(type=m.IdentityType.UNKNOWN, status=m.IdentityStatus.ACTIVE)
        db.add(owner)
        await db.flush()
        with pytest.raises((ValueError, IntegrityError)):
            await live_alert_service.create_alert(db, name='QA', identity_id=str(owner.id),
                                                  created_by=None, **invalid)
    run(with_async_database(work))


@pytest.mark.parametrize('shared_checksum', [False, True])
def test_merge_preserves_image_embedding_ownership_and_alert_membership(monkeypatch, shared_checksum):
    """Exercise actual DB consolidation; similarity scoring is outside this test."""
    from conftest import run_on_shared_loop as run
    from unittest.mock import AsyncMock
    from backend.core.identity_service import IdentityService
    service = IdentityService()
    monkeypatch.setattr(service, '_gate_merge_compatibility', AsyncMock())
    async def work(db):
        loser = m.Identity(type=m.IdentityType.UNKNOWN, status=m.IdentityStatus.ACTIVE)
        winner = m.Identity(type=m.IdentityType.UNKNOWN, status=m.IdentityStatus.ACTIVE)
        watchlist = m.Watchlist(name='qa-' + uuid.uuid4().hex)
        db.add_all([loser, winner, watchlist])
        await db.flush()
        original = m.IdentityImage(identity_id=loser.id, storage_path='storage/faces/qa-missing.jpg',
                                    file_checksum='a' * 64, is_primary=True)
        target = m.IdentityImage(identity_id=winner.id, storage_path='storage/faces/qa-other.jpg',
                                  file_checksum=('a' if shared_checksum else 'b') * 64, is_primary=True)
        alert = m.LiveSearchAlert(name='QA', identity_id=loser.id)
        membership = m.WatchlistEntry(watchlist_id=watchlist.id, identity_id=loser.id)
        db.add_all([original, target, alert, membership])
        await db.flush()
        embedding = m.IdentityEmbedding(identity_id=loser.id, image_id=original.id)
        db.add(embedding)
        await db.flush()
        await service.merge_identities(loser.id, winner.id, None, 'QA integrity', db)
        for row in (original, embedding, alert, membership, loser):
            await db.refresh(row)
        referenced_image = await db.get(m.IdentityImage, embedding.image_id)
        assert embedding.identity_id == referenced_image.identity_id == winner.id
        assert embedding.image_id == (target.id if shared_checksum else original.id)
        assert alert.identity_id == membership.identity_id == winner.id
        assert loser.status == m.IdentityStatus.MERGED
        primaries = (await db.execute(select(m.IdentityImage.id).where(
            m.IdentityImage.identity_id == winner.id, m.IdentityImage.is_primary.is_(True)))).scalars().all()
        assert primaries == [target.id]
        await db.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
        await db.execute(text("SET CONSTRAINTS ALL DEFERRED"))
        merge_id=(await db.execute(select(m.IdentityMerge.id).where(
            m.IdentityMerge.from_identity_id == loser.id,
            m.IdentityMerge.to_identity_id == winner.id))).scalar_one()
        await service.unmerge_identity(merge_id, user_id=None, username='qa', db=db)
        await db.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
        for row in (embedding,alert,membership,loser):
            await db.refresh(row)
        assert embedding.identity_id == loser.id and embedding.image_id == original.id
        assert alert.identity_id == membership.identity_id == loser.id
        assert loser.status == m.IdentityStatus.ACTIVE

    run(with_async_database(work))


@pytest.mark.parametrize('action', ['delete', 'inactive'])
def test_member_lifecycle_invalidates_pending_suggestions_preserving_history(db, action):
    a, b = person(db), person(db)
    ids = [str(a.id), str(b.id)]
    suggestion = m.MergeSuggestion(identity_ids=ids, confidence=.9, status=m.MergeSuggestionStatus.PENDING)
    historical = m.MergeSuggestion(identity_ids=ids, confidence=.9, status=m.MergeSuggestionStatus.REJECTED)
    db.add_all([suggestion, historical])
    db.flush()
    if action == 'delete':
        db.execute(text('DELETE FROM identities WHERE id=:id'), {'id': a.id})
    else:
        a.status = m.IdentityStatus.INACTIVE
        db.flush()
    db.refresh(suggestion)
    db.refresh(historical)
    assert suggestion.status == m.MergeSuggestionStatus.INVALIDATED
    assert suggestion.identity_ids == historical.identity_ids == ids
    assert historical.status == m.MergeSuggestionStatus.REJECTED
    assert db.execute(text('SELECT count(*) FROM pending_merge_members WHERE suggestion_id=:id'),
                      {'id': suggestion.id}).scalar_one() == 0


def test_reparenting_image_alone_is_rejected(db):
    a, b = person(db), person(db)
    image = photo(db, a)
    db.add(m.IdentityEmbedding(identity_id=a.id, image_id=image.id))
    db.flush()
    db.execute(text('SET CONSTRAINTS ALL IMMEDIATE'))
    with pytest.raises(IntegrityError):
        image.identity_id = b.id
        db.flush()


def test_reparenting_embedding_then_image_is_valid_at_transaction_end(db):
    a, b = person(db), person(db)
    image = photo(db, a)
    embedding = m.IdentityEmbedding(identity_id=a.id, image_id=image.id)
    db.add(embedding)
    db.flush()
    embedding.identity_id = b.id
    db.flush()
    image.identity_id = b.id
    db.flush()
    db.execute(text('SET CONSTRAINTS ALL IMMEDIATE'))


def test_explicit_alert_camera_blocks_deletion(db):
    owner, cam = person(db), camera(db)
    db.add(m.LiveSearchAlert(name='QA', identity_id=owner.id, pipeline_ids=[cam.pipeline_id]))
    db.flush()
    with pytest.raises(IntegrityError):
        db.execute(text('DELETE FROM pipelines WHERE id=:id'), {'id': cam.id})


def test_alert_updates_preserve_scope_and_reject_invalid_values():
    from conftest import run_on_shared_loop as run
    from backend.core.live_alert_service import live_alert_service
    async def work(db):
        owner = m.Identity(type=m.IdentityType.UNKNOWN, status=m.IdentityStatus.ACTIVE)
        cam = m.Pipeline(pipeline_id='qa-' + uuid.uuid4().hex)
        db.add_all([owner, cam])
        await db.flush()
        alert = await live_alert_service.create_alert(db, name='QA', identity_id=str(owner.id),
                                                      created_by=None, pipeline_ids=[], active_days=[])
        alert_id = str(alert.id)
        camera_id = cam.pipeline_id
        assert alert.pipeline_ids == [] and alert.active_days == []
        updated = await live_alert_service.update_alert(db, alert_id,
                         pipeline_ids=[cam.pipeline_id], active_days=[0, 6])
        assert updated.pipeline_ids == [cam.pipeline_id] and updated.active_days == [0, 6]
        for values in ({'pipeline_ids':['missing']}, {'active_days':[9]}):
            with pytest.raises(ValueError):
                await live_alert_service.update_alert(db, alert_id, **values)
            unchanged = await live_alert_service.get_alert(db, alert_id)
            assert unchanged.active_days == [0, 6]
            assert unchanged.pipeline_ids == [camera_id]
    run(with_async_database(work))


def test_camera_rename_preserves_alert_restrictions(monkeypatch):
    from conftest import run_on_shared_loop as run
    from backend.routes.users import rename_pipeline
    from unittest.mock import AsyncMock
    from backend.core.redis_cache import redis_cache_service
    monkeypatch.setattr(redis_cache_service, 'invalidate_unknown_cache', AsyncMock())
    async def work(db):
        owner = m.Identity(type=m.IdentityType.UNKNOWN, status=m.IdentityStatus.ACTIVE)
        cam = m.Pipeline(pipeline_id='qa-' + uuid.uuid4().hex)
        db.add_all([owner,cam])
        await db.flush()
        alert = m.LiveSearchAlert(name='QA', identity_id=owner.id, pipeline_ids=[cam.pipeline_id])
        unrestricted = m.LiveSearchAlert(name='QA all', identity_id=owner.id, pipeline_ids=[])
        db.add_all([alert, unrestricted])
        await db.flush()
        new_name = 'renamed-' + uuid.uuid4().hex
        result = await rename_pipeline(cam.pipeline_id, {'new_name':new_name}, db, None)
        assert result['success']
        await db.refresh(alert)
        await db.refresh(unrestricted)
        assert alert.pipeline_ids == [new_name] and unrestricted.pipeline_ids == []
        links = (await db.execute(text('SELECT pipeline_id FROM live_alert_pipeline_links WHERE alert_id=:id'),
                                  {'id':alert.id})).scalars().all()
        assert links == [new_name]
    run(with_async_database(work))


def isolated_sync_engine():
    url = make_url(settings.DATABASE_URL).set(drivername='postgresql+psycopg2')
    assert url.host == '127.0.0.1' and url.database == 'normalization_qa'
    return create_engine(url)


def test_concurrent_image_owner_change_rejects_stale_embedding():
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    engine = isolated_sync_engine()
    locked, release = Event(), Event()
    with Session(engine) as db:
        a, b = person(db), person(db)
        image = photo(db, a)
        aid, bid, image_id = a.id, b.id, image.id
        db.commit()
    def move_image():
        with engine.begin() as c:
            c.execute(text('UPDATE identity_images SET identity_id=:owner WHERE id=:id'),
                      {'owner':bid, 'id':image_id})
            locked.set()
            assert release.wait(5)
    def insert_stale_embedding():
        assert locked.wait(5)
        with Session(engine) as db:
            db.add(m.IdentityEmbedding(identity_id=aid, image_id=image_id))
            with pytest.raises(IntegrityError):
                db.commit()
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            move = pool.submit(move_image)
            assert locked.wait(5)
            insert = pool.submit(insert_stale_embedding)
            release.set()
            move.result(timeout=10)
            insert.result(timeout=10)
        with engine.connect() as c:
            assert c.execute(text('SELECT count(*) FROM identity_embeddings WHERE image_id=:id'),
                             {'id':image_id}).scalar_one() == 0
    finally:
        release.set()
        with engine.begin() as c:
            c.execute(text('DELETE FROM identities WHERE id IN (:a,:b)'), {'a':aid,'b':bid})
        engine.dispose()


def test_migration_roundtrip_preserves_populated_rows_and_refuses_corruption():
    """Exercise upgrade refusal/rollback using committed synthetic data only."""
    import subprocess
    import sys
    engine = isolated_sync_engine()
    def migration(action, revision, expected_success=True):
        result = subprocess.run([sys.executable, '-m', 'alembic', '-c', 'alembic.ini', action, revision],
                                cwd='/app/alembic', capture_output=True, text=True, timeout=90)
        assert (result.returncode == 0) == expected_success, result.stderr[-3500:]
    with Session(engine) as db:
        a, b = person(db), person(db)
        cam = camera(db)
        image = photo(db, a)
        embedding = m.IdentityEmbedding(identity_id=a.id, image_id=image.id)
        alert = m.LiveSearchAlert(name='QA migration', identity_id=a.id, pipeline_ids=[cam.pipeline_id], active_days=[0,6])
        suggestion = m.MergeSuggestion(identity_ids=[str(a.id),str(b.id)], confidence=.9,
                                        status=m.MergeSuggestionStatus.PENDING)
        db.add_all([embedding,alert,suggestion])
        db.flush()
        aid,bid,image_id,alert_id,suggestion_id,cam_id = a.id,b.id,image.id,alert.id,suggestion.id,cam.id
        pipeline_id=cam.pipeline_id
        db.commit()
    try:
        migration('downgrade','fdd4e5f6a7b8')
        with Session(engine) as db:
            corrupt=m.IdentityEmbedding(identity_id=bid, image_id=image_id)
            db.add(corrupt)
            db.flush()
            corrupt_id=corrupt.id
            db.commit()
        migration('upgrade','fee5f6a7b8c9',expected_success=False)
        with engine.connect() as c:
            assert c.execute(text('SELECT version_num FROM alembic_version')).scalar_one() == 'fdd4e5f6a7b8'
            assert c.execute(text("SELECT to_regclass('live_alert_pipeline_links')")).scalar_one() is None
            assert c.execute(text('SELECT count(*) FROM identity_embeddings WHERE id=:id'),{'id':corrupt_id}).scalar_one() == 1
        with engine.begin() as c:
            c.execute(text('DELETE FROM identity_embeddings WHERE id=:id'),{'id':corrupt_id})
        migration('upgrade','fee5f6a7b8c9')
        with Session(engine) as db:
            assert db.get(m.LiveSearchAlert,alert_id).pipeline_ids == [pipeline_id]
            assert db.get(m.MergeSuggestion,suggestion_id).identity_ids == [str(aid),str(bid)]
            assert db.execute(text('SELECT count(*) FROM live_alert_pipeline_links WHERE alert_id=:id'),{'id':alert_id}).scalar_one() == 1
            assert db.execute(text('SELECT count(*) FROM pending_merge_members WHERE suggestion_id=:id'),{'id':suggestion_id}).scalar_one() == 2
            assert db.execute(text('SELECT count(*) FROM identity_embeddings WHERE image_id=:id'),{'id':image_id}).scalar_one() == 1
    finally:
        with engine.begin() as c:
            c.execute(text('DELETE FROM merge_suggestions WHERE id=:id'),{'id':suggestion_id})
            c.execute(text('DELETE FROM identities WHERE id IN (:a,:b)'),{'a':aid,'b':bid})
            c.execute(text('DELETE FROM pipelines WHERE id=:id'),{'id':cam_id})
        engine.dispose()


def test_alert_http_create_update_validation_with_real_database(monkeypatch):
    from conftest import run_on_shared_loop as run
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient
    from unittest.mock import AsyncMock
    from backend.routes import live_alerts as routes
    monkeypatch.setattr(routes, '_audit', AsyncMock())
    async def work(db):
        owner=m.Identity(type=m.IdentityType.UNKNOWN, status=m.IdentityStatus.ACTIVE)
        tag=uuid.uuid4().hex
        user=m.User(username='qa-'+tag, email=tag+'@example.invalid',password_hash='not-a-login',role='admin')
        cam=m.Pipeline(pipeline_id='qa-'+tag)
        db.add_all([owner,user,cam])
        await db.flush()
        owner_id,camera_id,user_id=str(owner.id),cam.pipeline_id,user.id
        # Commit the savepoint so validation rollback does not remove setup rows.
        await db.commit()
        app=FastAPI()
        app.include_router(routes.router)
        async def session_override():
            yield db
        app.dependency_overrides[routes.get_db]=session_override
        from types import SimpleNamespace
        app.dependency_overrides[routes.get_current_user]=lambda: SimpleNamespace(
            id=user_id, role='admin', username='qa', email='qa@example.invalid', is_active=True)
        async with AsyncClient(transport=ASGITransport(app),base_url='http://qa',
                               headers={'X-Requested-With':'XMLHttpRequest'}) as client:
            for invalid in ({'pipeline_ids':['missing-camera']},{'active_days':[9]}):
                response=await client.post('/api/live-alerts',json={'name':'QA','identity_id':owner_id,**invalid})
                assert response.status_code == 400, response.text
            response=await client.post('/api/live-alerts',json={'name':'QA','identity_id':owner_id,
                                                             'pipeline_ids':[camera_id],'active_days':[0,6]})
            assert response.status_code == 200, response.text
            alert_id=response.json()['id']
            for invalid in ({'pipeline_ids':['missing-camera']},{'active_days':[9]}):
                response=await client.put('/api/live-alerts/'+alert_id,json=invalid)
                assert response.status_code == 400, response.text
            response=await client.put('/api/live-alerts/'+alert_id,json={'pipeline_ids':[],'active_days':[]})
            assert response.status_code == 200, response.text
            assert response.json()['pipeline_ids'] == [] and response.json()['active_days'] == []
    run(with_async_database(work))

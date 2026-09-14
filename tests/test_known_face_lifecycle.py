"""Deletion tests use temporary files and an OUTER database transaction.

Service commits release only SAVEPOINTs. Nothing in these tests can commit a
person, deletion, or audit row into the running application's database.
"""
import asyncio
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4
from unittest.mock import AsyncMock

import numpy as np
import pytest
from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

import db_models as m
from config import settings
from backend.core import known_face_lifecycle as lifecycle


def test_storage_boundary_and_symlink(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, 'STORAGE_DIR', str(tmp_path / 'storage'))
    root = Path(settings.STORAGE_DIR); root.mkdir()
    assert lifecycle.storage_file('storage/faces/a.jpg') == root / 'faces/a.jpg'
    for value in ['../outside.jpg', '/etc/passwd', str(root)]:
        with pytest.raises(ValueError): lifecycle.storage_file(value)
    (root / 'link.jpg').symlink_to(tmp_path / 'outside.jpg')
    with pytest.raises(ValueError): lifecycle.storage_file('link.jpg')


def test_every_direct_identity_foreign_key_has_a_deletion_rule():
    rules = lifecycle.related_filters([uuid4()])
    for table in m.Base.metadata.tables.values():
        if table.name != 'identities' and any(fk.target_fullname == 'identities.id' for fk in table.foreign_keys):
            assert table.name in rules, table.name


async def exercise(tmp_path, monkeypatch, mode):
    monkeypatch.setattr(settings, 'STORAGE_DIR', str(tmp_path))
    monkeypatch.setattr(settings, 'VECTOR_BACKEND', 'pgvector')
    # Never touch the process's real recognition index or tracker.
    import backend.core as core
    monkeypatch.setattr(core, 'face_tracker', None, raising=False)
    engine = create_async_engine(settings.DATABASE_URL, echo=False)
    async with engine.connect() as connection:
        outer = await connection.begin()
        try:
            async with AsyncSession(bind=connection, join_transaction_mode='create_savepoint', expire_on_commit=False) as db:
                uid, other_id = uuid4(), uuid4()
                actor = m.User(username='qa_delete_' + uuid4().hex, email=uuid4().hex+'@example.invalid', password_hash='test-only', role='admin')
                person = m.Identity(id=uid, type=m.IdentityType.KNOWN, status=m.IdentityStatus.ACTIVE, display_name='QA Delete Person')
                other = m.Identity(id=other_id, type=m.IdentityType.KNOWN, status=m.IdentityStatus.ACTIVE, display_name='QA Survivor')
                db.add_all([actor, person, other]); await db.flush()
                folder = tmp_path / 'faces' / str(uid); folder.mkdir(parents=True)
                own_file = folder / 'image_001.jpg'; own_file.write_bytes(b'qa-private-photo')
                shared_file = tmp_path / 'shared.jpg'; shared_file.write_bytes(b'qa-shared-photo')
                person.best_snapshot_path = 'storage/shared.jpg'
                other.best_snapshot_path = 'storage/shared.jpg'
                gallery = m.IdentityImage(identity_id=uid, storage_path=f'storage/faces/{uid}/image_001.jpg', file_checksum='a'*64, is_primary=True, processing_status='completed')
                db.add(gallery); await db.flush()
                db.add_all([m.IdentityEmbedding(identity_id=uid, image_id=gallery.id, embedding=np.ones(512)/np.sqrt(512)),
                            m.IdentityEmbedding(identity_id=other_id, embedding=np.ones(512)/np.sqrt(512))])
                if mode == 'relations':
                    alias = m.Identity(id=uuid4(), type=m.IdentityType.KNOWN, status=m.IdentityStatus.MERGED,
                                       display_name='QA Old Alias', merged_into_id=uid)
                    pipeline = m.Pipeline(pipeline_id='qa_delete_' + uuid4().hex)
                    watchlist = m.Watchlist(name='QA ' + uuid4().hex)
                    db.add_all([alias, pipeline, watchlist]); await db.flush()
                    detection = m.Detection(pipeline_id=pipeline.pipeline_id)
                    entry = m.WatchlistEntry(watchlist_id=watchlist.id, identity_id=uid)
                    other_entry = m.WatchlistEntry(watchlist_id=watchlist.id, identity_id=other_id)
                    alert = m.LiveSearchAlert(name='QA alert', identity_id=uid, created_by=actor.id)
                    db.add_all([detection, entry, other_entry, alert]); await db.flush()
                    db.add_all([
                        m.Face(detection_id=detection.id, name=person.display_name, identity_id=uid, similarity=.9, face_image_path=str(own_file)),
                        m.Face(detection_id=detection.id, name=other.display_name, identity_id=other_id, similarity=.9, face_image_path=str(shared_file)),
                        m.WatchlistAlert(watchlist_entry_id=entry.id, triggered_by='detection', snapshot_path=str(own_file)),
                        m.LiveAlertTrigger(alert_id=alert.id, snapshot_path=str(own_file)),
                        m.LiveAlertAuditLog(alert_id=alert.id, action='create', username=actor.username),
                        m.IdentityMerge(from_identity_id=alias.id, to_identity_id=uid, merged_by=actor.id),
                        m.MergeSuggestion(identity_ids=[str(uid), str(other_id)], confidence=.9),
                        m.SearchHistory(user_id=actor.id, search_type=m.SearchType.SINGLE, results_summary={'identity_id': str(uid)}),
                        m.PendingEnrollment(token_hash=uuid4().hex*2, user_id=actor.id, display_name='QA pending', display_name_key='qa pending',
                            storage_path=str(own_file), file_checksum='b'*64, decision='uncertain', candidates=[{'identity_id': str(uid)}],
                            expires_at=person.created_at),
                    ])
                await db.flush()
                actor_id = actor.id
                _, preview = await lifecycle.deletion_plan(db, uid)
                assert preview['photos'] == 1 and preview['embeddings'] == 1
                assert own_file.exists(), 'preview never deletes'
                if mode == 'deactivate':
                    await lifecycle.set_active(db, uid, False, actor)
                    assert (await db.execute(select(m.Identity.status).where(m.Identity.id == uid))).scalar_one() == m.IdentityStatus.INACTIVE
                    assert own_file.exists()
                    assert (await db.execute(select(func.count()).select_from(m.IdentityEmbedding).where(m.IdentityEmbedding.identity_id == uid))).scalar() == 1
                    await lifecycle.set_active(db, uid, True, actor)
                    assert (await db.execute(select(m.Identity.status).where(m.Identity.id == uid))).scalar_one() == m.IdentityStatus.ACTIVE
                    return
                if mode == 'wrong_name':
                    with pytest.raises(HTTPException) as error:
                        await lifecycle.permanently_delete(db, uid, 'wrong', preview['preview_token'], actor)
                    assert error.value.status_code == 400 and own_file.exists()
                    return
                if mode == 'stale_preview':
                    with pytest.raises(HTTPException) as error:
                        await lifecycle.permanently_delete(db, uid, person.display_name, '0'*64, actor)
                    assert error.value.status_code == 409 and own_file.exists()
                    return
                if mode == 'retry':
                    purge = AsyncMock(side_effect=RuntimeError('simulated index failure'))
                    monkeypatch.setattr(lifecycle, '_purge_index', purge)
                    with pytest.raises(HTTPException) as error:
                        await lifecycle.permanently_delete(db, uid, 'QA Delete Person', preview['preview_token'], actor)
                    assert error.value.status_code == 503 and own_file.exists()
                    assert await lifecycle._journal(db, uid) is not None
                    # Read plain actor values: a rollback expired old ORM objects.
                    actor = SimpleNamespace(id=actor_id, username='qa-delete-actor')
                    with pytest.raises(HTTPException):
                        await lifecycle.set_active(db, uid, True, actor)
                    _, preview = await lifecycle.deletion_plan(db, uid)
                    purge.side_effect = None
                    monkeypatch.setattr(lifecycle, '_purge_index', AsyncMock())
                result = await lifecycle.permanently_delete(db, uid, 'QA Delete Person', preview['preview_token'], actor)
                assert result['deleted'] is True and result['shared_files_retained'] == 1
                assert not own_file.exists() and shared_file.exists()
                assert (await db.execute(select(func.count()).select_from(m.Identity).where(m.Identity.id == uid))).scalar() == 0
                assert (await db.execute(select(func.count()).select_from(m.IdentityEmbedding).where(m.IdentityEmbedding.identity_id == uid))).scalar() == 0
                assert (await db.execute(select(func.count()).select_from(m.Identity).where(m.Identity.id == other_id))).scalar() == 1
                assert (await db.execute(select(func.count()).select_from(m.IdentityEmbedding).where(m.IdentityEmbedding.identity_id == other_id))).scalar() == 1
                receipt = (await db.execute(select(m.IdentityAuditLog).where(m.IdentityAuditLog.action_type == 'person_deleted', m.IdentityAuditLog.action_details['deleted_record_id'].astext == str(uid)))).scalar_one()
                assert receipt.identity_id is None and receipt.before_state is None
                assert 'files' not in receipt.action_details and 'embedding_ids' not in receipt.action_details
                if mode == 'relations':
                    assert (await db.execute(select(func.count()).select_from(m.Identity).where(m.Identity.id == alias.id))).scalar() == 0
                    assert (await db.execute(select(func.count()).select_from(m.Face).where(m.Face.detection_id == detection.id))).scalar() == 1
                    assert (await db.execute(select(func.count()).select_from(m.WatchlistEntry).where(m.WatchlistEntry.watchlist_id == watchlist.id))).scalar() == 1
                    assert (await db.execute(select(func.count()).select_from(m.LiveSearchAlert).where(m.LiveSearchAlert.id == alert.id))).scalar() == 0
                    assert (await db.execute(select(func.count()).select_from(m.IdentityMerge).where(m.IdentityMerge.to_identity_id == uid))).scalar() == 0
        finally:
            await outer.rollback()
    await engine.dispose()


@pytest.mark.parametrize('mode', ['deactivate', 'wrong_name', 'stale_preview', 'delete', 'retry', 'relations'])
def test_lifecycle_with_rollback_only_database(tmp_path, monkeypatch, mode):
    asyncio.run(exercise(tmp_path, monkeypatch, mode))


def test_faiss_purge_removes_old_snapshot_biometrics(tmp_path):
    from backend.core.vector_index.flat_faiss import FlatFaissIndex
    index = FlatFaissIndex(str(tmp_path), dim=512)
    # Exercise real vectors and real snapshots exclusively under pytest tmpdir.
    for key in (1, 2):
        index.add(key, np.ones(512, dtype=np.float32)/np.sqrt(512), model_version='test')
    index.save(); index.save()
    index.purge([1])
    assert len(list(tmp_path.glob('snapshot-*'))) == 1
    assert 1 not in index.keys() and 2 in index.keys()

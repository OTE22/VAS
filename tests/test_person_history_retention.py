"""Retention separation against a disposable PostgreSQL server and synthetic files.

Set RETENTION_TEST_DATABASE_URL to a database called retention_test on the
isolated host retention-test-db. Never points at the application database.
"""
import os
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pytest
from sqlalchemy import select, func, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from conftest import run_on_shared_loop as run
from config import settings
from db_connection import db_manager
from db_models import (Base, Pipeline, Identity, IdentityType, IdentityStatus,
                       Detection, Face, IdentityAppearance, IdentityEmbedding,
                       IdentityMerge, SearchHistory, SearchType)
from backend.core.data_retention import DataRetentionManager
from backend.core.identity_retention import IdentityRetentionManager


@pytest.fixture
def isolated(monkeypatch, tmp_path):
    url = os.environ.get('RETENTION_TEST_DATABASE_URL')
    if not url:
        pytest.skip('requires disposable retention-test-db')
    parsed = make_url(url)
    assert parsed.host == 'retention-test-db' and parsed.database == 'retention_test'
    engine = create_async_engine(url)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async def prepare():
        async with engine.begin() as conn:
            await conn.execute(text('CREATE EXTENSION IF NOT EXISTS vector'))
            await conn.run_sync(Base.metadata.drop_all)
            await conn.run_sync(Base.metadata.create_all)
    run(prepare())
    @asynccontextmanager
    async def session():
        async with sessions() as db:
            yield db
    monkeypatch.setattr(db_manager, 'engine', engine)
    monkeypatch.setattr(db_manager, 'get_session', session)
    monkeypatch.setattr(settings, 'STORAGE_DIR', str(tmp_path))
    monkeypatch.setattr(settings, 'PRESERVE_PERSON_HISTORY', True)
    monkeypatch.setattr(settings, 'SNAPSHOT_RETENTION_DAYS', 90)
    monkeypatch.setattr(settings, 'DATA_RETENTION_DAYS', 365)
    from backend.core import detection_spool
    monkeypatch.setattr(detection_spool, 'protected_paths', lambda: set())
    monkeypatch.setattr(detection_spool, 'protected_embedding_ids', lambda: set())
    yield sessions, tmp_path
    run(engine.dispose())


async def seed(sessions, folder, age=500, copies=12, gallery=False):
    when = datetime.utcnow() - timedelta(days=age)
    path = folder / ('faces/enrolled.jpg' if gallery else 'camera/capture.jpg')
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b'synthetic-image-fixture')
    vector = np.zeros(512, dtype=np.float32); vector[0] = 1
    async with sessions() as db:
        db.add(Pipeline(pipeline_id='synthetic-camera'))
        identity = Identity(type=IdentityType.UNKNOWN, first_seen_at=when,
                            last_seen_at=when, best_snapshot_path=str(path), appearances_count=1)
        db.add(identity); await db.flush()
        detection = Detection(pipeline_id='synthetic-camera', timestamp=when)
        db.add(detection); await db.flush()
        db.add(Face(detection_id=detection.id, name='Synthetic person', similarity=.9,
                    identity_id=identity.id, face_image_path=str(path)))
        db.add(IdentityAppearance(identity_id=identity.id, pipeline_id='synthetic-camera',
                                 detection_id=detection.id, start_time=when, best_snapshot_path=str(path)))
        for _ in range(copies):
            db.add(IdentityEmbedding(identity_id=identity.id, detection_id=detection.id,
                    pipeline_id='synthetic-camera', embedding=vector, quality=.8,
                    embedding_model_version='synthetic-v1', created_at=when))
        await db.commit()
        return identity.id, detection.id, vector, path


def test_expire_image_preserve_history_and_search_after_inactivation(isolated):
    sessions, folder = isolated
    async def exercise():
        iid, did, vector, path = await seed(sessions, folder)
        manager = DataRetentionManager()
        preview = await manager.cleanup_old_data(dry_run=True, job_id='synthetic-preview')
        assert preview['status'] == 'completed', preview['failures']
        assert preview['candidate_rows'] == 0 and path.exists()
        result = await manager.cleanup_old_data(job_id='synthetic-cleanup')
        assert result['status'] == 'completed', result['failures']
        assert result['rows_deleted'] == 0
        from backend.core.identity_index_pgvector import IdentityIndexPgVector
        async with sessions() as db:
            live = await IdentityIndexPgVector().search_unknown(vector, db, threshold=.8)
            assert live and live[0][0] == str(iid)
        retention = IdentityRetentionManager()
        assert await retention._cleanup_old_snapshots() == 1
        assert not path.exists()
        assert await retention._cleanup_old_snapshots() == 0
        assert await retention._cleanup_excess_embeddings() == 0
        assert (await retention.reconcile_orphan_camera_embeddings())['embeddings'] == 0
        assert await retention._mark_inactive_identities() == 1
        async with sessions() as db:
            for model, expected in [(Identity, 1), (Detection, 1), (Face, 1),
                                    (IdentityAppearance, 1), (IdentityEmbedding, 12)]:
                assert (await db.scalar(select(func.count()).select_from(model))) == expected
            person = await db.get(Identity, iid)
            assert person.status == IdentityStatus.INACTIVE and person.best_snapshot_path is None
            assert (await db.scalar(select(Face.face_image_path))) is None
            appearance = await db.scalar(select(IdentityAppearance))
            assert appearance.best_snapshot_path is None and appearance.detection_id == did
            from backend.core.identity_index_pgvector import IdentityIndexPgVector
            index = IdentityIndexPgVector()
            assert await index.search_unknown(vector, db, threshold=.8) == []
            historical = await index.search_unknown(vector, db, threshold=.8, include_inactive=True)
            assert historical and historical[0][0] == str(iid)
            from backend.core.quick_image_search import rank_people
            assert (await rank_people(db, vector, 'synthetic-v1', 'both', 10))[0][0].id == iid
            from backend.core.vector_index.access import search_similar_embeddings
            hits = await search_similar_embeddings(db, vector, include_inactive=True)
            assert hits and hits[0]['identity_id'] == str(iid)
            from backend.core.advanced_search import AdvancedSearchService
            for use_pgvector in (True, False):
                service = AdvancedSearchService(pgvector_index=index)
                service.use_pgvector = use_pgvector
                matches, _ = await service._search_indexes(vector, db, 'both', 10, [], {})
                assert [m.identity_id for m in matches] == [str(iid)]
                assert matches[0].best_snapshot_path is None
            assert person.status == IdentityStatus.INACTIVE
    run(exercise())


def test_history_preservation_still_cleans_search_logs(isolated):
    sessions, folder = isolated
    async def exercise():
        await seed(sessions, folder)
        async with sessions() as db:
            db.add(SearchHistory(search_type=list(SearchType)[0],
                                 created_at=datetime.utcnow()-timedelta(days=500)))
            await db.commit()
        result = await DataRetentionManager().cleanup_old_data(job_id='synthetic-cleanup')
        assert result['status'] == 'completed', result['failures']
        assert result['rows_deleted'] == 0
        assert result['extra']['search_history_deleted'] == 1
        async with sessions() as db:
            assert await db.scalar(select(func.count(Detection.id))) == 1
            assert await db.scalar(select(func.count(SearchHistory.id))) == 0
    run(exercise())


@pytest.mark.parametrize('protection', ['gallery', 'merge_evidence', 'recent_sighting', 'pending'])
def test_protected_image_survives(isolated, monkeypatch, protection):
    sessions, folder = isolated
    async def exercise():
        iid, did, vector, path = await seed(sessions, folder, gallery=protection == 'gallery')
        async with sessions() as db:
            if protection == 'merge_evidence':
                other = Identity(type=IdentityType.UNKNOWN); db.add(other); await db.flush()
                db.add(IdentityMerge(from_identity_id=other.id, to_identity_id=iid,
                                     provenance={'image': str(path)}))
            elif protection == 'recent_sighting':
                db.add(IdentityAppearance(identity_id=iid, pipeline_id='synthetic-camera',
                                         start_time=datetime.utcnow(), best_snapshot_path=str(path)))
            elif protection == 'pending':
                from backend.core import detection_spool
                monkeypatch.setattr(detection_spool, 'protected_paths', lambda: {str(path)})
            await db.commit()
        assert await IdentityRetentionManager()._cleanup_old_snapshots() == 0
        assert path.exists()
        async with sessions() as db:
            assert await db.scalar(select(func.count(Detection.id))) == 1
            assert await db.scalar(select(func.count(IdentityEmbedding.id))) == 12
    run(exercise())


def test_failed_unlink_keeps_retryable_reference(isolated, monkeypatch):
    sessions, folder = isolated
    async def exercise():
        iid, did, vector, path = await seed(sessions, folder)
        original = Path.unlink
        def denied(p, *args, **kwargs):
            if p == path:
                raise PermissionError('synthetic unlink failure')
            return original(p, *args, **kwargs)
        monkeypatch.setattr(Path, 'unlink', denied)
        with pytest.raises(PermissionError):
            await IdentityRetentionManager()._cleanup_old_snapshots()
        assert path.exists()
        async with sessions() as db:
            assert (await db.get(Identity, iid)).best_snapshot_path == str(path)
            assert await db.scalar(select(func.count(Detection.id))) == 1
        monkeypatch.setattr(Path, 'unlink', original)
        assert await IdentityRetentionManager()._cleanup_old_snapshots() == 1
    run(exercise())


def test_explicitly_disabling_preservation_restores_detection_expiry(isolated, monkeypatch):
    sessions, folder = isolated
    async def exercise():
        await seed(sessions, folder)
        monkeypatch.setattr(settings, 'PRESERVE_PERSON_HISTORY', False)
        result = await DataRetentionManager().cleanup_old_data(job_id='synthetic-cleanup')
        assert result['status'] == 'completed', result['failures']
        assert result['rows_deleted'] == 1
        async with sessions() as db:
            assert await db.scalar(select(func.count(Detection.id))) == 0
    run(exercise())


def test_legacy_orphan_with_history_is_preserved_but_failed_frame_is_reconciled(isolated):
    sessions, folder = isolated
    async def exercise():
        iid, did, vector, path = await seed(sessions, folder)
        async with sessions() as db:
            await db.execute(text('UPDATE identity_embeddings SET detection_id = NULL'))
            ghost = Identity(type=IdentityType.UNKNOWN); db.add(ghost); await db.flush()
            ghost_id = ghost.id
            db.add(IdentityEmbedding(identity_id=ghost.id, pipeline_id='synthetic-camera',
                    embedding=vector, created_at=datetime.utcnow()-timedelta(days=1)))
            await db.commit()
        result = await IdentityRetentionManager().reconcile_orphan_camera_embeddings()
        assert result['embeddings'] == 1 and result['identities'] == 1
        async with sessions() as db:
            assert await db.scalar(select(func.count(IdentityEmbedding.id))) == 12
            assert await db.get(Identity, iid) is not None
            assert await db.get(Identity, ghost_id) is None
    run(exercise())


@pytest.mark.parametrize('kind', [IdentityType.UNKNOWN, IdentityType.KNOWN])
def test_historical_search_returns_distinct_people_and_excludes_merges(isolated, kind):
    sessions, folder = isolated
    async def exercise():
        iid, did, vector, path = await seed(sessions, folder, copies=60)
        async with sessions() as db:
            person = await db.get(Identity, iid); person.type = kind; person.status = IdentityStatus.INACTIVE
            second = Identity(type=kind, status=IdentityStatus.INACTIVE)
            merged = Identity(type=kind, status=IdentityStatus.MERGED, merged_into_id=iid)
            db.add_all([second, merged]); await db.flush()
            second_vector = vector.copy(); second_vector[0] = .9; second_vector[1] = np.sqrt(.19)
            for other, v in [(second, second_vector), (merged, vector)]:
                db.add(IdentityEmbedding(identity_id=other.id, embedding=v, embedding_model_version='synthetic-v1'))
            await db.commit()
            from backend.core.identity_index_pgvector import IdentityIndexPgVector
            from backend.core.vector_index.access import search_similar_embeddings
            index = IdentityIndexPgVector()
            method = index.search_known if kind == IdentityType.KNOWN else index.search_unknown
            assert await method(vector, db, top_k=2, threshold=.8) == []
            rows = await method(vector, db, top_k=2, threshold=.8, include_inactive=True)
            assert [r[0] for r in rows] == [str(iid), str(second.id)]
            rows = await search_similar_embeddings(db, vector, top_k=2, threshold=.8,
                                                   identity_type=kind.value.lower(), include_inactive=True)
            assert [r['identity_id'] for r in rows] == [str(iid), str(second.id)]
    run(exercise())


def test_batched_shared_image_and_missing_file_cleanup(isolated):
    sessions, folder = isolated
    async def exercise():
        iid, did, vector, path = await seed(sessions, folder)
        async with sessions() as db:
            db.add_all([Face(detection_id=did, name='Synthetic', similarity=.9,
                             identity_id=iid, face_image_path=str(path)) for _ in range(260)])
            db.add(Face(detection_id=did, name='Synthetic', similarity=.9,
                        identity_id=iid, face_image_path=str(folder/'camera/missing.jpg')))
            await db.commit()
        assert await IdentityRetentionManager()._cleanup_old_snapshots() == 1
        async with sessions() as db:
            assert await db.scalar(select(func.count(Face.id))) == 262
            assert await db.scalar(select(func.count(Face.id)).where(Face.face_image_path.isnot(None))) == 0
    run(exercise())

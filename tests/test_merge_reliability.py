"""Merge regression checks; database fixtures live only inside rolled-back transactions."""
import asyncio
import importlib
import uuid
from contextlib import asynccontextmanager
from datetime import datetime
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch

import numpy as np
import pytest
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from config import settings
import db_models as m
from backend.core.identity_service import IdentityService
from backend.core.merge_compatibility import assess_merge_compatibility, _load_valid_embeddings


@asynccontextmanager
async def sandbox():
    engine = create_async_engine(settings.DATABASE_URL)
    try:
        async with engine.connect() as conn:
            outer = await conn.begin()
            try:
                async with AsyncSession(bind=conn, expire_on_commit=False,
                                        join_transaction_mode='create_savepoint') as db:
                    admin = (await db.execute(select(m.User.id).where(m.User.role == 'admin').limit(1))).scalar_one()
                    yield db, IdentityService(None), admin
            finally:
                await outer.rollback()
    finally:
        await engine.dispose()


def vector(angle):
    result = np.zeros(512, dtype=np.float32)
    result[:2] = [np.cos(np.deg2rad(angle)), np.sin(np.deg2rad(angle))]
    return result


def test_queued_detection_follows_merge_chain_and_preserves_events():
    async def run():
        from backend.core.detection_evidence import persist_detection
        module = importlib.import_module('backend.core.identity_service')
        async with sandbox() as (db, service, admin):
            a, b, c = [m.Identity(type=m.IdentityType.UNKNOWN, status=m.IdentityStatus.ACTIVE) for _ in range(3)]
            camera = m.Pipeline(pipeline_id='merge-test-' + uuid.uuid4().hex)
            db.add_all([a, b, c, camera]); await db.flush()
            def frame(stamp):
                return {'pipeline_id': camera.pipeline_id, 'location_name': 'Test location',
                    'detection': {'uuid': str(uuid.uuid4()), 'pipeline_id': camera.pipeline_id, 'timestamp': stamp},
                    'faces': [{'identity_id': a.id, 'name': 'Unknown', 'similarity': .8,
                               'label_state': 'auto_unknown', '_event_id': uuid.uuid4().hex}]}
            with patch.object(module, 'identity_service', service), patch.object(settings, 'LIVE_ALERTS_ENABLED', False), patch.object(settings, 'WATCHLIST_ENABLED', False):
                first = await persist_detection(db, detection_data=frame(datetime(2026, 9, 13, 10)))
                queued = frame(datetime(2026, 9, 13, 11))
                await service.merge_identities(a.id, b.id, admin, None, db, confirm_merge_risk=True)
                await service.merge_identities(b.id, c.id, admin, None, db, confirm_merge_risk=True)
                await db.commit()
                delayed = await persist_detection(db, detection_data=queued)
                assert delayed.unknown_events[0]['identity_id'] == str(c.id)
                assert delayed.unknown_events[0]['timestamp'] == '2026-09-13T11:00:00Z'
                rows = (await db.execute(select(m.IdentityAppearance).where(m.IdentityAppearance.identity_id.in_([a.id, b.id, c.id])))).scalars().all()
                assert len(rows) == 2 and all(row.identity_id == c.id for row in rows)
                assert len({row.event_id for row in rows}) == 2
                assert all(row.pipeline_id == camera.pipeline_id for row in rows)
                faces = (await db.execute(select(m.Face).where(m.Face.detection_id.in_([first.detection_id, delayed.detection_id])))).scalars().all()
                assert all(face.identity_id == c.id for face in faces)
                assert queued['faces'][0]['identity_id'] == a.id, 'queue payload remains unchanged'
                replay = await persist_detection(db, detection_data=queued)
                assert replay.unknown_events == []
    asyncio.run(run())


def test_known_identity_is_survivor_even_with_fewer_sightings():
    async def run():
        async with sandbox() as (db, service, _):
            known = m.Identity(type=m.IdentityType.KNOWN, status=m.IdentityStatus.ACTIVE, appearances_count=1)
            unknown = m.Identity(type=m.IdentityType.UNKNOWN, status=m.IdentityStatus.ACTIVE, appearances_count=1000)
            db.add_all([known, unknown]); await db.flush()
            winner, details = await service.find_best_identity([unknown.id, known.id], db)
            assert winner == known.id
            assert details['candidates'][0]['id'] == str(known.id)
            db.add_all([m.IdentityEmbedding(identity_id=known.id, embedding=vector(0), embedding_model_version='qa', faiss_index_type='known'),
                        m.IdentityEmbedding(identity_id=unknown.id, embedding=vector(0), embedding_model_version='qa', faiss_index_type='unknown')])
            await db.flush()
            result = await service.merge_multiple_identities([unknown.id, known.id], user_id=_, db=db)
            assert result['identity'].id == known.id
            labels = (await db.execute(select(m.IdentityEmbedding.faiss_index_type).where(m.IdentityEmbedding.identity_id == known.id))).scalars().all()
            assert labels == ['known', 'known']
    asyncio.run(run())


def test_self_merge_refused_in_service():
    async def run():
        identifier = uuid.uuid4()
        with pytest.raises(ValueError, match='itself'):
            await IdentityService(None).merge_identities(identifier, identifier, None, None, AsyncMock())
    asyncio.run(run())


def test_sampling_retains_older_quality_and_rejects_missing_models():
    async def run():
        from backend.core.face_quality import QUALITY_SCORER_VERSION
        async with sandbox() as (db, _, __):
            a, b = [m.Identity(type=m.IdentityType.UNKNOWN, status=m.IdentityStatus.ACTIVE) for _ in range(2)]
            db.add_all([a, b]); await db.flush()
            db.add(m.IdentityEmbedding(identity_id=a.id, embedding=vector(40), quality=.99,
                quality_scorer_version=QUALITY_SCORER_VERSION, embedding_model_version='qa',
                created_at=datetime(2020, 1, 1)))
            for _ in range(30):
                db.add(m.IdentityEmbedding(identity_id=a.id, embedding=vector(0), quality=.2,
                    quality_scorer_version=QUALITY_SCORER_VERSION, embedding_model_version='qa'))
            db.add(m.IdentityEmbedding(identity_id=b.id, embedding=vector(0), embedding_model_version=None))
            await db.flush()
            samples, _ = await _load_valid_embeddings(db, a.id)
            assert len(samples['qa']) == 2
            assert np.allclose(samples['qa'][0], vector(40))
            assessment = await assess_merge_compatibility(db, [a.id, b.id])
            assert assessment.risk_level == 'unavailable'
    asyncio.run(run())


def test_original_group_requires_warning_before_sequential_merge():
    async def run():
        import backend.core.merge_compatibility as gate
        ids = [uuid.uuid4() for _ in range(3)]
        vectors = dict(zip(ids, [[vector(0)], [vector(35)], [vector(70)]]))
        async def load(db, identifier): return {'qa': vectors[identifier]}, 0
        with patch.object(gate, '_load_valid_embeddings', load), patch.object(settings, 'MERGE_WARNING_MIN_SIMILARITY', .4):
            assessment = await gate.assess_merge_compatibility(None, ids)
            assert assessment.requires_confirmation
            assert assessment.robust_similarity == pytest.approx(.3420201)
    asyncio.run(run())


def test_audit_failure_rolls_back_before_commit_and_cleans_only_staged_files(tmp_path):
    async def run():
        import backend.routes.identities as route
        from fastapi import HTTPException
        a, b = [NS(id=uuid.uuid4(), status=m.IdentityStatus.ACTIVE, type=m.IdentityType.UNKNOWN,
                   display_name=None, appearances_count=1) for _ in range(2)]
        staged = tmp_path / 'staged.jpg'; staged.write_bytes(b'test')
        async def merge(**kwargs):
            kwargs['copied_files'].append(str(staged)); return b
        db = NS(execute=AsyncMock(side_effect=[NS(scalar_one_or_none=lambda: a), NS(scalar_one_or_none=lambda: b)]),
                commit=AsyncMock(), rollback=AsyncMock())
        request = route.MergeRequest(from_identity_id=str(a.id), to_identity_id=str(b.id), decision='merge_existing')
        with patch.object(route, 'get_identity_service', return_value=NS(merge_identities=merge)), patch.object(route, 'get_client_info', return_value=(None, None)), patch.object(route, 'request_snapshot', AsyncMock()), patch.object(route.IdentityAuditLogger, 'log_merge', AsyncMock(side_effect=RuntimeError('audit unavailable'))):
            with pytest.raises(HTTPException) as error:
                await route.merge_identities(request, None, db, NS(role='admin', id=1, username='qa'))
            assert error.value.status_code == 500
            db.commit.assert_not_awaited()
            assert not staged.exists()
    asyncio.run(run())


def test_detection_readers_share_lock_and_merge_waits():
    async def run():
        from backend.core.identity_merge_lock import lock_identity_mutation
        engine = create_async_engine(settings.DATABASE_URL)
        try:
            async with engine.connect() as one, engine.connect() as two, engine.connect() as writer:
                await lock_identity_mutation(one)
                await asyncio.wait_for(lock_identity_mutation(two), timeout=5)
                pending = asyncio.create_task(lock_identity_mutation(writer, exclusive=True))
                try:
                    await asyncio.sleep(.1)
                    assert not pending.done(), 'merge must wait for active detections'
                    await one.rollback()
                    await asyncio.sleep(.05)
                    assert not pending.done(), 'second reader still holds its lock'
                    await two.rollback()
                    await asyncio.wait_for(pending, timeout=5)
                finally:
                    if not pending.done():
                        pending.cancel()
                        try: await pending
                        except asyncio.CancelledError: pass
                    await writer.rollback()
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_suggestions_refuse_unverified_or_different_models():
    async def run():
        from backend.core.pipeline_aware_clustering import PipelineAwareClusteringService
        service = PipelineAwareClusteringService()
        for first, second in [(None, None), ('one', 'two')]:
            result = await service._calculate_pipeline_aware_similarity(
                NS(embedding_model_version=first), NS(embedding_model_version=second), [])
            assert result == (0.0, False)
    asyncio.run(run())


def test_suggestion_route_checks_original_group_without_partial_merge():
    async def run():
        import backend.routes.identities as route
        async with sandbox() as (db, service, admin):
            people = [m.Identity(type=m.IdentityType.UNKNOWN, status=m.IdentityStatus.ACTIVE) for _ in range(3)]
            db.add_all(people); await db.flush()
            ids = [p.id for p in people]
            for person, angle in zip(people, [0, 35, 70]):
                db.add(m.IdentityEmbedding(identity_id=person.id, embedding=vector(angle), embedding_model_version='qa'))
            suggestion = m.MergeSuggestion(cluster_id='qa-' + uuid.uuid4().hex,
                identity_ids=[str(i) for i in ids], confidence=.9, status=m.MergeSuggestionStatus.PENDING)
            db.add(suggestion); await db.flush()
            suggestion_id = suggestion.id
            await db.commit()
            with patch.object(route, 'get_identity_service', return_value=service), patch.object(settings, 'MERGE_WARNING_MIN_SIMILARITY', .4):
                response = await route.approve_merge_suggestion(suggestion_id, None, None, db, NS(role='admin', id=admin, username='qa'))
            assert response.status_code == 409
            states = (await db.execute(select(m.Identity.status).where(m.Identity.id.in_(ids)))).scalars().all()
            assert states == [m.IdentityStatus.ACTIVE] * 3
    asyncio.run(run())


def test_multi_merge_uses_snapshot_measurement_not_unrelated_embedding():
    async def run():
        async with sandbox() as (db, service, admin):
            a, b = [m.Identity(type=m.IdentityType.UNKNOWN, status=m.IdentityStatus.ACTIVE,
                              best_snapshot_path=f'storage/detections/qa-{uuid.uuid4().hex}.jpg') for _ in range(2)]
            db.add_all([a, b]); await db.flush()
            db.add_all([m.IdentityEmbedding(identity_id=a.id, embedding=vector(0), quality=.99, embedding_model_version='qa'),
                        m.IdentityEmbedding(identity_id=b.id, embedding=vector(0), quality=.1, embedding_model_version='qa')])
            await db.flush()
            async def measured(db, person): return .2 if person.id == a.id else .8
            with patch.object(service, 'measured_snapshot_quality', measured):
                result = await service.merge_multiple_identities([a.id, b.id], a.id, admin, None, db)
            assert result['identity'].best_snapshot_path == b.best_snapshot_path
            assert result['snapshot_selection']['quality'] == .8
    asyncio.run(run())

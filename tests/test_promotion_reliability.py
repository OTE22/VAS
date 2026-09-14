"""Promotion admission and atomicity. All database fixtures roll back."""
import asyncio
import importlib
import json
import uuid
from datetime import datetime
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select
from config import settings
import db_models as m
from backend.core.promotion_review import PromotionError
from test_merge_reliability import sandbox, vector


async def unknown(db, service, *, embedding=True, status=m.IdentityStatus.ACTIVE):
    service.embedding_model_version = 'promotion-qa-' + uuid.uuid4().hex[:12]
    person = m.Identity(type=m.IdentityType.UNKNOWN, status=status)
    db.add(person); await db.flush()
    if embedding:
        db.add(m.IdentityEmbedding(identity_id=person.id, embedding=vector(0),
            embedding_model_version=service.embedding_model_version, faiss_index_type='unknown'))
        await db.flush()
    return person


@pytest.mark.parametrize('case', ['merged', 'inactive', 'empty', 'legacy'])
def test_refuses_unusable_or_retired_sources(case):
    async def run():
        async with sandbox() as (db, service, admin):
            person = await unknown(db, service, embedding=case != 'empty')
            if case == 'merged':
                winner = m.Identity(type=m.IdentityType.UNKNOWN, status=m.IdentityStatus.ACTIVE)
                db.add(winner); await db.flush()
                await service.merge_identities(person.id, winner.id, admin, None, db, confirm_merge_risk=True)
            if case == 'inactive': person.status = m.IdentityStatus.INACTIVE
            if case == 'legacy': service.embedding_model_version = 'another-model'
            await db.flush()
            with pytest.raises(PromotionError):
                await service.promote_unknown_to_known(person.id, 'QA', admin, db)
            assert person.type == m.IdentityType.UNKNOWN
            assert person.status != m.IdentityStatus.PROMOTED
    asyncio.run(run())


def test_duplicate_review_recomputed_and_same_identity_preserved():
    async def run():
        async with sandbox() as (db, service, admin):
            source = await unknown(db, service)
            identifier = source.id
            known = m.Identity(type=m.IdentityType.KNOWN, status=m.IdentityStatus.ACTIVE,
                               display_name='qa-name-' + uuid.uuid4().hex)
            db.add(known); await db.flush()
            db.add(m.IdentityEmbedding(identity_id=known.id, embedding=vector(0),
                embedding_model_version=service.embedding_model_version, faiss_index_type='known'))
            await db.flush()
            with pytest.raises(PromotionError) as error:
                await service.promote_unknown_to_known(source.id, known.display_name, admin, db)
            assert error.value.code == 'PROMOTION_REVIEW_REQUIRED'
            assert error.value.review['candidates'][0]['same_name']
            assert source.type == m.IdentityType.UNKNOWN
            review = {}
            result = await service.promote_unknown_to_known(source.id, known.display_name, admin, db,
                confirm_create_new=True, review_result=review)
            assert result.id == identifier and result.type == m.IdentityType.KNOWN
            assert result.merged_into_id is None and review['confirmed_create_new']
            assert known.status == m.IdentityStatus.ACTIVE
            with pytest.raises(PromotionError):
                await service.promote_unknown_to_known(source.id, 'Second click', admin, db, confirm_create_new=True)
    asyncio.run(run())


def test_route_audit_failure_rolls_back_promotion_and_staged_gallery(tmp_path):
    async def run():
        import backend.routes.identities as route
        from fastapi import HTTPException
        async with sandbox() as (db, service, admin):
            source = await unknown(db, service)
            identifier = source.id
            await db.commit()
            staged = tmp_path / 'promotion.jpg'
            original = service.promote_unknown_to_known
            async def promote(*args, **kwargs):
                result = await original(*args, **kwargs)
                staged.write_bytes(b'qa')
                kwargs['copied_files'].append(str(staged))
                return result
            with patch.object(route, 'get_identity_service', return_value=service), patch.object(service, 'promote_unknown_to_known', promote), patch.object(route, 'get_client_info', return_value=(None, None)), patch.object(route.IdentityAuditLogger, 'log_promote', AsyncMock(side_effect=RuntimeError('audit failed'))):
                with pytest.raises(HTTPException) as error:
                    await route.promote_unknown_to_known(str(identifier), route.PromoteRequest(
                        display_name='qa-' + uuid.uuid4().hex, decision='create_new'), None, db,
                        NS(id=admin, username='qa', role='admin'))
                assert error.value.status_code == 500
            assert not staged.exists()
            current = (await db.execute(select(m.Identity).where(m.Identity.id == identifier))).scalar_one()
            assert current.type == m.IdentityType.UNKNOWN
    asyncio.run(run())


def test_gallery_copy_failure_propagates_to_transaction_owner(tmp_path):
    async def run():
        from backend.core.enrollment_service import adopt_existing_file
        import backend.core.enrollment_service as enrollment
        async with sandbox() as (db, service, admin):
            source = await unknown(db, service)
            snapshot = tmp_path / 'source.jpg'
            snapshot.write_bytes(b'qa snapshot')
            journal = []
            with patch.object(settings, 'STORAGE_DIR', str(tmp_path)), patch.object(enrollment, 'identity_folder', return_value=str(tmp_path / 'gallery')), patch.object(enrollment.shutil, 'copy2', side_effect=OSError('disk full')):
                with pytest.raises(OSError, match='disk full'):
                    await adopt_existing_file(db, source, str(snapshot), copied_files=journal)
            assert len(journal) == 1
    asyncio.run(run())


def test_promotion_audit_and_late_detection_stay_with_same_identity():
    async def run():
        import backend.routes.identities as route
        from backend.core.detection_evidence import persist_detection
        service_module = importlib.import_module('backend.core.identity_service')
        async with sandbox() as (db, service, admin):
            source = await unknown(db, service)
            identifier = source.id
            camera = m.Pipeline(pipeline_id='promotion-qa-' + uuid.uuid4().hex)
            db.add(camera); await db.flush()
            name = 'qa-' + uuid.uuid4().hex
            queued = {'pipeline_id': camera.pipeline_id, 'detection': {'uuid': str(uuid.uuid4()),
                'pipeline_id': camera.pipeline_id, 'timestamp': datetime(2026,9,13,10)},
                'faces': [{'identity_id': identifier, 'name': 'Unknown', 'label_state': 'auto_unknown',
                           'similarity': .8, '_event_id': uuid.uuid4().hex}]}
            with patch.object(route, 'get_identity_service', return_value=service), patch.object(route, 'get_client_info', return_value=(None, None)), patch.object(route, 'request_snapshot', AsyncMock()), patch('backend.core.merge_notifications.publish_promotion', AsyncMock()) as publish:
                response = await route.promote_unknown_to_known(str(identifier), route.PromoteRequest(
                    display_name=name, decision='create_new', person_code='QA-' + uuid.uuid4().hex[:8]), None, db,
                    NS(id=admin, username='qa', role='admin'))
                assert response['success'] and response['identity']['id'] == str(identifier)
                publish.assert_awaited_once()
            logs = (await db.execute(select(m.IdentityAuditLog).where(m.IdentityAuditLog.identity_id == identifier,
                                     m.IdentityAuditLog.action_type == 'promote'))).scalars().all()
            assert len(logs) == 1 and 'promotion_review' in logs[0].action_details
            with patch.object(service_module, 'identity_service', service), patch.object(settings, 'LIVE_ALERTS_ENABLED', False), patch.object(settings, 'WATCHLIST_ENABLED', False):
                outcome = await persist_detection(db, detection_data=queued)
                assert outcome.unknown_events == []
                appearance = (await db.execute(select(m.IdentityAppearance).where(m.IdentityAppearance.detection_id == outcome.detection_id))).scalar_one()
                assert appearance.identity_id == identifier and appearance.start_time == datetime(2026,9,13,10)
                face = (await db.execute(select(m.Face).where(m.Face.detection_id == outcome.detection_id))).scalar_one()
                assert face.name == name and face.label_state == m.LabelState.MANUAL_LABELED
    asyncio.run(run())

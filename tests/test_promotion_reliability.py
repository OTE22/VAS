"""Selected-sighting promotion against a disposable database and synthetic photos."""
import hashlib
import uuid
from datetime import datetime, timedelta
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch
import numpy as np
import pytest
from sqlalchemy import select, func
from conftest import run_on_shared_loop as run
from test_person_history_retention import isolated
from test_known_reference_isolation import service, vector, seed as known_seed
from config import settings
import db_models as m
from backend.core.promotion_review import PromotionError, selected_sighting


@pytest.fixture(autouse=True)
def extraction(monkeypatch):
    from backend.core import enrollment_service as enrollment
    monkeypatch.setattr(enrollment, 'prepare_upload', lambda *a, **kw: NS(
        embedding_normalized=vector(0), quality=.9, quality_scorer_version='synthetic'))
    monkeypatch.setattr(enrollment, 'embedding_model_version', lambda: 'reference-test-v1')


async def seed(db, folder, count=3):
    user=m.User(username='synthetic-admin',email='admin@example.invalid',password_hash='not-a-login',role='admin')
    camera=m.Pipeline(pipeline_id='synthetic-camera',location_name='Synthetic Lobby')
    db.add_all([user,camera]); await db.flush()
    source=m.Identity(type=m.IdentityType.UNKNOWN,appearances_count=count)
    db.add(source); await db.flush()
    sightings=[]
    for i in range(count):
        payload=f'synthetic sighting {i}'.encode();path=folder/f'sighting-{i}.jpg';path.write_bytes(payload)
        when=datetime(2026,1,1,10)+timedelta(minutes=i)
        detection=m.Detection(pipeline_id=camera.pipeline_id,timestamp=when)
        db.add(detection);await db.flush()
        face=m.Face(identity_id=source.id,detection_id=detection.id,name='Unknown',similarity=.7,
            label_state=m.LabelState.AUTO_UNKNOWN,face_image_path=str(path))
        appearance=m.IdentityAppearance(identity_id=source.id,detection_id=detection.id,
            pipeline_id=camera.pipeline_id,start_time=when,best_snapshot_path=str(path),event_id=f'event-{i}')
        embedding=m.IdentityEmbedding(identity_id=source.id,detection_id=detection.id,pipeline_id=camera.pipeline_id,
            embedding=vector(i*45),embedding_model_version='reference-test-v1',faiss_index_type='unknown')
        db.add_all([face,appearance,embedding]);await db.flush()
        sightings.append(NS(appearance=appearance,face=face,embedding=embedding,path=path,
            checksum=hashlib.sha256(payload).hexdigest()))
    source.best_snapshot_path=str(sightings[-1].path)
    await db.commit()
    return source,user,sightings


def kwargs(sighting):
    return dict(appearance_id=sighting.appearance.id,photo_sha256=sighting.checksum,copied_files=[])


def test_only_selected_event_moves_and_exact_photo_becomes_reference(isolated):
    sessions,folder=isolated
    async def exercise():
        svc=service()
        async with sessions() as db:
            source,user,rows=await seed(db,folder);chosen=rows[1];review={}
            original=[(r.appearance.id,r.face.id,r.embedding.id,r.path.read_bytes()) for r in rows]
            result=await svc.promote_unknown_to_known(source.id,'Synthetic Known',user.id,db,**kwargs(chosen),review_result=review)
            await db.commit()
            assert result.id!=source.id and result.type==m.IdentityType.KNOWN
            assert source.type==m.IdentityType.UNKNOWN and source.status==m.IdentityStatus.ACTIVE
            assert source.appearances_count==2 and result.appearances_count==1
            assert result.first_seen_at==chosen.appearance.start_time==result.last_seen_at
            assert chosen.face.identity_id==result.id and chosen.face.name=='Synthetic Known'
            assert chosen.face.label_state==m.LabelState.MANUAL_LABELED
            assert chosen.appearance.identity_id==chosen.embedding.identity_id==result.id
            assert chosen.embedding.faiss_index_type=='known_observation'
            assert chosen.embedding.image_id is None
            for r in [rows[0],rows[2]]:
                assert r.appearance.identity_id==r.face.identity_id==r.embedding.identity_id==source.id
                assert r.face.name=='Unknown' and r.face.label_state==m.LabelState.AUTO_UNKNOWN
            assert original==[(r.appearance.id,r.face.id,r.embedding.id,r.path.read_bytes()) for r in rows]
            assert await db.scalar(select(func.count(m.Face.id)))==3
            assert await db.scalar(select(func.count(m.IdentityAppearance.id)))==3
            assert await db.scalar(select(func.count(m.IdentityEmbedding.id)))==4
            gallery=(await db.execute(select(m.IdentityImage))).scalar_one()
            from backend.core.enrollment_service import pending_absolute_path
            from pathlib import Path
            assert Path(pending_absolute_path(gallery.storage_path)).read_bytes()==chosen.path.read_bytes()
            assert gallery.identity_id==result.id and gallery.is_primary and gallery.file_checksum==chosen.checksum
            assert result.best_snapshot_path==gallery.storage_path
            assert (await svc.search_vector_index(db,vector(0),threshold=.99))[0][0]==str(result.id)
            assert await svc.search_vector_index(db,vector(45),threshold=.99)==[]
            assert review['appearance_id']==chosen.appearance.id and review['scope']=='selected_sighting'
            with pytest.raises(PromotionError,match='no longer belongs'):
                await svc.promote_unknown_to_known(source.id,'Duplicate click',user.id,db,**kwargs(chosen))
    run(exercise())


@pytest.mark.parametrize('case,code',[
    ('missing_selection','PROMOTION_SELECTION_REQUIRED'),('foreign_sighting','PROMOTION_SELECTION_CHANGED'),
    ('missing_photo','PROMOTION_IMAGE_REQUIRED'),('changed_photo','PROMOTION_PHOTO_CHANGED'),
    ('wrong_face_path','PROMOTION_SELECTION_AMBIGUOUS'),('two_faces','PROMOTION_SELECTION_AMBIGUOUS'),
    ('two_appearances','PROMOTION_SELECTION_AMBIGUOUS'),('missing_detection','PROMOTION_SELECTION_AMBIGUOUS'),
    ('inactive','IDENTITY_NOT_ACTIVE'),('already_known','IDENTITY_NOT_UNKNOWN'),
    ('no_model','PROMOTION_MODEL_UNAVAILABLE')])
def test_invalid_or_stale_selection_cannot_rename_any_record(isolated,case,code):
    sessions,folder=isolated
    async def exercise():
        svc=service()
        async with sessions() as db:
            source,user,rows=await seed(db,folder);r=rows[0];args=kwargs(r)
            if case=='missing_selection':args={}
            if case=='foreign_sighting':args['appearance_id']=99999
            if case=='missing_photo':r.path.unlink()
            if case=='changed_photo':r.path.write_bytes(b'changed')
            if case=='wrong_face_path':r.face.face_image_path=str(rows[1].path)
            if case=='two_faces':db.add(m.Face(identity_id=source.id,detection_id=r.face.detection_id,name='Unknown',similarity=.6,face_image_path=str(r.path)))
            if case=='two_appearances':db.add(m.IdentityAppearance(identity_id=source.id,detection_id=r.face.detection_id,pipeline_id='synthetic-camera',start_time=r.appearance.start_time,best_snapshot_path=str(r.path)))
            if case=='missing_detection':r.appearance.detection_id=None
            if case=='inactive':source.status=m.IdentityStatus.INACTIVE
            if case=='already_known':source.type=m.IdentityType.KNOWN
            if case=='no_model':svc.embedding_model_version=None
            await db.flush()
            with pytest.raises(PromotionError) as error:
                await svc.promote_unknown_to_known(source.id,'Do not assign',user.id,db,**args)
            assert error.value.code==code
            assert source.display_name is None and all(row.face.name=='Unknown' for row in rows)
            assert await db.scalar(select(func.count(m.Identity.id)))==1
            assert await db.scalar(select(func.count(m.IdentityImage.id)))==0
    run(exercise())


def test_duplicate_review_uses_selected_photo_not_unrelated_group_vectors(isolated):
    sessions,folder=isolated
    async def exercise():
        svc=service()
        async with sessions() as db:
            source,user,rows=await seed(db,folder);sid=source.id
            known,image,embedding=await known_seed(db,folder,svc,angle=90)
            # The group has a 90-degree observation but the selected photo extracts at 0.
            result=await svc.promote_unknown_to_known(source.id,'Different',user.id,db,**kwargs(rows[0]))
            assert result.id!=known.id
            await db.rollback()
        async with sessions() as db:
            source=await db.get(m.Identity,sid);user=(await db.execute(select(m.User))).scalar_one()
            selected=(await db.execute(select(m.IdentityAppearance).order_by(m.IdentityAppearance.id))).scalars().first()
            known,image,embedding=await known_seed(db,folder,svc,angle=0)
            args=dict(appearance_id=selected.id,photo_sha256=hashlib.sha256((folder/'sighting-0.jpg').read_bytes()).hexdigest(),copied_files=[])
            with pytest.raises(PromotionError) as error:
                await svc.promote_unknown_to_known(source.id,'Different',user.id,db,**args)
            assert error.value.code=='PROMOTION_REVIEW_REQUIRED' and error.value.review['candidates'][0]['identity_id']==str(known.id)
            review={}
            result=await svc.promote_unknown_to_known(source.id,'Different',user.id,db,**args,confirm_create_new=True,review_result=review)
            assert review['confirmed_create_new'] and source.type==m.IdentityType.UNKNOWN
    run(exercise())


def test_preview_is_read_only_and_uses_selected_camera_photo(isolated):
    sessions,folder=isolated
    async def exercise():
        import backend.routes.identities as route
        async with sessions() as db:
            source,user,rows=await seed(db,folder)
            response=await route.unknown_match_candidates(str(source.id),rows[0].appearance.id,db,user)
            assert response['selection']['photo_sha256']==rows[0].checksum
            assert response['selection']['preview_image'].endswith('/sighting-0.jpg')
            assert response['selection']['camera_name']=='Synthetic Lobby'
            assert not db.new and not db.dirty and not db.deleted
            assert await db.scalar(select(func.count(m.IdentityImage.id)))==0
    run(exercise())


@pytest.mark.parametrize('failure',['audit','copy','reference'])
def test_failed_route_rolls_back_all_rows_and_copied_files(isolated,failure):
    sessions,folder=isolated
    async def exercise():
        import backend.routes.identities as route
        import backend.core.enrollment_service as enrollment
        from fastapi import HTTPException
        svc=service()
        async with sessions() as db:
            source,user,rows=await seed(db,folder);sid=source.id;selected=rows[0].appearance.id
            request=route.PromoteRequest(display_name='Synthetic',decision='create_new',appearance_id=selected,photo_sha256=rows[0].checksum)
            target,attribute,effect=(route.IdentityAuditLogger,'log_promote',AsyncMock(side_effect=RuntimeError('audit failed'))) if failure=='audit' else (enrollment.shutil,'copy2',lambda *a: (_ for _ in ()).throw(OSError('disk full'))) if failure=='copy' else (enrollment,'ensure_image_reference',AsyncMock(side_effect=RuntimeError('inference failed')))
            with patch.object(route,'get_identity_service',return_value=svc),patch.object(route,'get_client_info',return_value=(None,None)),patch.object(target,attribute,effect):
                if failure=='audit':
                    with pytest.raises(HTTPException):await route.promote_unknown_to_known(str(sid),request,None,db,user)
                else:
                    response=await route.promote_unknown_to_known(str(sid),request,None,db,user)
                    assert response.status_code==400
            assert await db.scalar(select(func.count(m.Identity.id)))==1
            assert await db.scalar(select(func.count(m.IdentityImage.id)))==0
            assert (await db.get(m.IdentityAppearance,selected)).identity_id==sid
            assert not list((folder/'faces').rglob('*.jpg'))
    run(exercise())


def test_route_audits_transfer_and_queued_detection_stays_unknown(isolated,monkeypatch):
    sessions,folder=isolated
    async def exercise():
        import backend.routes.identities as route
        import importlib
        from backend.core.detection_evidence import persist_detection
        svc=service()
        async with sessions() as db:
            source,user,rows=await seed(db,folder);sid=source.id
            with patch.object(route,'get_identity_service',return_value=svc),patch.object(route,'get_client_info',return_value=(None,None)),patch.object(route,'request_snapshot',AsyncMock()),patch('backend.core.merge_notifications.publish_promotion',AsyncMock()) as publish:
                response=await route.promote_unknown_to_known(str(sid),route.PromoteRequest(display_name='Synthetic',decision='create_new',appearance_id=rows[0].appearance.id,photo_sha256=rows[0].checksum),None,db,user)
                assert response['identity']['id']!=str(sid) and response['source_remaining_appearances']==2
                publish.assert_awaited_once()
            logs=(await db.execute(select(m.IdentityAuditLog))).scalars().all()
            assert {r.action_type for r in logs}=={'promote','promote_sighting'}
            log=next(r for r in logs if r.action_type=='promote_sighting')
            assert log.identity_id==sid and str(log.related_identity_id)==response['identity']['id']
            queued={'pipeline_id':'synthetic-camera','detection':{'uuid':str(uuid.uuid4()),'pipeline_id':'synthetic-camera','timestamp':datetime(2026,1,2)},'faces':[{'identity_id':sid,'name':'Unknown','label_state':'auto_unknown','similarity':.7,'_event_id':uuid.uuid4().hex}]}
            monkeypatch.setattr(importlib.import_module('backend.core.identity_service'),'identity_service',svc)
            monkeypatch.setattr(settings,'LIVE_ALERTS_ENABLED',False);monkeypatch.setattr(settings,'WATCHLIST_ENABLED',False)
            outcome=await persist_detection(db,detection_data=queued)
            face=(await db.execute(select(m.Face).where(m.Face.detection_id==outcome.detection_id))).scalar_one()
            assert face.identity_id==sid and face.name=='Unknown' and face.label_state==m.LabelState.AUTO_UNKNOWN
    run(exercise())


def test_camera_permission_is_checked_on_selected_sighting(isolated):
    sessions,folder=isolated
    async def exercise():
        import backend.routes.identities as route
        from fastapi import HTTPException
        async with sessions() as db:
            source,user,rows=await seed(db,folder);user.role='user'
            with pytest.raises(HTTPException) as err:
                await route.unknown_match_candidates(str(source.id),rows[0].appearance.id,db,user)
            assert err.value.status_code==403
            db.add(m.UserPipelineAccess(user_id=user.id,pipeline_id='synthetic-camera'));await db.flush()
            assert (await route.unknown_match_candidates(str(source.id),rows[0].appearance.id,db,user))['success']
    run(exercise())


def test_single_sighting_retires_empty_source_without_deleting_it(isolated):
    sessions,folder=isolated
    async def exercise():
        async with sessions() as db:
            source,user,rows=await seed(db,folder,count=1)
            target=await service().promote_unknown_to_known(source.id,'Synthetic',user.id,db,**kwargs(rows[0]))
            assert source.type==m.IdentityType.UNKNOWN and source.status==m.IdentityStatus.INACTIVE
            assert source.appearances_count==0 and source.best_snapshot_path is None
            assert await db.scalar(select(func.count(m.Identity.id)))==2
    run(exercise())

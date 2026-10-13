"""Search integrity against disposable PostgreSQL and synthetic vectors only."""
from datetime import datetime
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch
import hashlib
import uuid
import numpy as np
import pytest
from sqlalchemy import select, func
from fastapi import HTTPException
from config import settings
import db_models as m
from conftest import run_on_shared_loop as run
from test_person_history_retention import isolated
from backend.core.search_evidence import search_evidence, hydrate_evidence
from backend.core.advanced_search import AdvancedSearchService, FaceInImage, MultiSearchResult
from backend.routes.identities import get_identity_details

MODEL='search-test-v1'
def vector(angle):
    v=np.zeros(512,dtype=np.float32);v[:2]=[np.cos(np.deg2rad(angle)),np.sin(np.deg2rad(angle))];return v

@pytest.fixture(autouse=True)
def config(monkeypatch):
    monkeypatch.setattr(settings,'SIMILARITY_THRESHOLD',.8)
    monkeypatch.setattr(settings,'UNKNOWN_SIMILARITY_THRESHOLD',.35)
    monkeypatch.setattr(settings,'RECOGNITION_MODEL',MODEL+'.onnx')

async def person(db, kind=m.IdentityType.UNKNOWN):
    p=m.Identity(type=kind,status=m.IdentityStatus.ACTIVE,display_name='Synthetic enrolled name' if kind==m.IdentityType.KNOWN else None,
                 best_snapshot_path='storage/wrong-group-portrait.jpg')
    db.add(p);await db.flush();return p

async def sighting(db, p, angle, camera='demo-camera', day=1, model=MODEL):
    if not await db.scalar(select(m.Pipeline.id).where(m.Pipeline.pipeline_id==camera)):
        db.add(m.Pipeline(pipeline_id=camera));await db.flush()
    d=m.Detection(pipeline_id=camera,timestamp=datetime(2026,1,day));db.add(d);await db.flush()
    path=f'storage/{camera}/evidence-{d.id}.jpg'
    f=m.Face(identity_id=p.id,detection_id=d.id,name='Synthetic',similarity=.9,face_image_path=path)
    a=m.IdentityAppearance(identity_id=p.id,detection_id=d.id,pipeline_id=camera,start_time=d.timestamp,best_snapshot_path=path)
    e=m.IdentityEmbedding(identity_id=p.id,detection_id=d.id,pipeline_id=camera,embedding=vector(angle),embedding_model_version=model)
    db.add_all([f,a,e]);await db.flush();return f,a,e

async def enrollment(db,p,angle,status='completed',model=MODEL):
    path=f'storage/faces/{p.id}/{uuid.uuid4()}.jpg'
    im=m.IdentityImage(identity_id=p.id,storage_path=path,file_checksum=hashlib.sha256(path.encode()).hexdigest(),processing_status=status)
    db.add(im);await db.flush();e=m.IdentityEmbedding(identity_id=p.id,image_id=im.id,embedding=vector(angle),embedding_model_version=model)
    db.add(e);await db.flush();return im,e


def test_mixed_group_returns_exact_photo_and_details_exclude_other_person(isolated):
    sessions,_=isolated
    async def check():
        async with sessions() as db:
            p=await person(db);_,a0,e0=await sighting(db,p,0);face,a,e=await sighting(db,p,90,day=2)
            service=AdvancedSearchService()
            matches,_=await service._search_indexes(vector(90),db,'both',10,[],{})
            hit=matches[0]
            assert hit.similarity==pytest.approx(1.) and hit.best_snapshot_path==face.face_image_path
            assert hit.best_snapshot_path!=p.best_snapshot_path
            assert (hit.matched_embedding_id,hit.matched_appearance_id,hit.matched_face_id)==(e.id,a.id,face.id)
            data=await get_identity_details(str(p.id),db,NS(role='admin'),search_embedding_id=e.id)
            assert data['best_snapshot_path']==face.face_image_path
            states={row['id']:row['association_status'] for row in data['appearances']}
            assert states=={a.id:'selected',a0.id:'review_required'}
            assert data['association_review']['threshold']==.8
            assert await db.scalar(select(func.count(m.IdentityAppearance.id)))==2
    run(check())


def test_known_wrong_history_never_inherits_name_or_watchlist(isolated):
    sessions,_=isolated
    async def check():
        async with sessions() as db:
            p=await person(db,m.IdentityType.KNOWN);im,ref=await enrollment(db,p,0);f,a,e=await sighting(db,p,90)
            service=AdvancedSearchService()
            hits,_=await service._search_indexes(vector(90),db,'known',10,[],{})
            hit=hits[0];assert hit.display_name is None and hit.type=='unverified'
            assert hit.similarity==pytest.approx(1.) and hit.reference_similarity==pytest.approx(0.,abs=1e-6)
            no_db=NS(execute=AsyncMock(side_effect=AssertionError('Unverified name must not query watchlists')))
            checked=await service._check_watchlists(no_db,hits,[])
            assert checked[0].watchlist_match is None
            good=(await search_evidence(db,vector(0),MODEL,'known',10))[0]
            assert good.result()['display_name']==p.display_name and good.snapshot_path==im.storage_path
            data=await get_identity_details(str(p.id),db,NS(role='admin'),search_embedding_id=e.id)
            assert data['display_name'] is None and data['type']=='unverified'
            assert await db.scalar(select(func.count(m.IdentityEmbedding.id)))==2
    run(check())


@pytest.mark.parametrize('case',['failed_image','other_model','wrong_owner','query_only','observation_only'])
def test_reference_verification_requires_owned_completed_same_model_image_and_both_comparisons(isolated,case):
    sessions,_=isolated
    async def check():
        async with sessions() as db:
            p=await person(db,m.IdentityType.KNOWN)
            im,ref=await enrollment(db,p,0,status='failed' if case=='failed_image' else 'completed',model='old' if case=='other_model' else MODEL)
            _,_,e=await sighting(db,p,50 if case=='query_only' else 0)
            if case=='wrong_owner':
                other=await person(db);im.identity_id=other.id;await db.flush()
            query=vector(50 if case=='observation_only' else 0)
            hit=(await hydrate_evidence(db,[(p,e,.9)],query,MODEL))[0]
            assert hit.verification=='unverified' and hit.result()['display_name'] is None
    run(check())


def test_camera_and_date_filter_the_matched_sighting_before_ranking(isolated):
    sessions,_=isolated
    async def check():
        async with sessions() as db:
            p=await person(db);await sighting(db,p,0,'camera-a',1);_,app,e=await sighting(db,p,30,'camera-b',2)
            p.last_seen_at=datetime(2026,5,1)
            hits=await search_evidence(db,vector(0),MODEL,'both',1,pipeline_id='camera-b',start=datetime(2026,1,2),end=datetime(2026,1,2))
            assert hits[0].embedding.id==e.id and hits[0].appearance_id==app.id
            assert hits[0].similarity==pytest.approx(np.cos(np.deg2rad(30)))
            assert not await search_evidence(db,vector(0),MODEL,'both',10,pipeline_id='camera-a',start=datetime(2026,1,2))
            shown,alerts=await AdvancedSearchService()._search_indexes(vector(0),db,'both',10,[],{'pipeline_id':'absent'})
            assert shown==[] and len(alerts)==1
    run(check())


def test_distinct_people_model_threshold_exclusions_and_inactive_history(isolated):
    sessions,_=isolated
    async def check():
        async with sessions() as db:
            people=[await person(db) for _ in range(6)]
            for _ in range(30):await sighting(db,people[0],0)
            await sighting(db,people[1],20);await sighting(db,people[2],60)
            await sighting(db,people[3],0,model='old');await sighting(db,people[4],0);await sighting(db,people[5],0)
            people[4].status=m.IdentityStatus.INACTIVE
            people[5].merged_into_id=people[0].id;people[5].status=m.IdentityStatus.MERGED;await db.flush()
            hits=await search_evidence(db,vector(0),MODEL,'both',10)
            assert {h.identity.id for h in hits}=={people[i].id for i in [0,1,4]}
            assert len(await search_evidence(db,vector(0),MODEL,'both',2))==2
            hits=await search_evidence(db,vector(0),MODEL,'both',10,exclude_identity_ids=[str(people[0].id)])
            assert {h.identity.id for h in hits}=={people[i].id for i in [1,4]}
            for bad in [np.zeros(512),np.full(512,np.nan),np.ones(12)]:
                with pytest.raises(HTTPException):await search_evidence(db,bad,MODEL,'both',10)
    run(check())


@pytest.mark.parametrize('case',['two_faces','two_vectors','wrong_snapshot','missing_detection','expired_photo'])
def test_ambiguous_and_expired_evidence_never_borrows_group_portrait(isolated,case):
    sessions,_=isolated
    async def check():
        async with sessions() as db:
            p=await person(db);f,a,e=await sighting(db,p,0)
            if case=='two_faces':db.add(m.Face(identity_id=p.id,detection_id=e.detection_id,name='Other',similarity=.8,face_image_path='storage/other.jpg'))
            if case=='two_vectors':db.add(m.IdentityEmbedding(identity_id=p.id,detection_id=e.detection_id,embedding=vector(10),embedding_model_version=MODEL))
            if case=='wrong_snapshot':a.best_snapshot_path='storage/unrelated.jpg'
            if case=='missing_detection':e.detection_id=None
            if case=='expired_photo':f.face_image_path=None;a.best_snapshot_path=None
            await db.flush();hit=(await search_evidence(db,vector(0),MODEL,'both',10))[0]
            assert hit.snapshot_path!=p.best_snapshot_path
            if case in ('two_faces','two_vectors','missing_detection','expired_photo'):assert hit.snapshot_path is None
            if case!='expired_photo':assert hit.appearance_id is None
    run(check())


def test_scoped_details_reject_cross_identity_and_cross_sighting_ids(isolated):
    sessions,_=isolated
    async def check():
        async with sessions() as db:
            p,q=await person(db),await person(db);_,a,e=await sighting(db,p,0);_,b,other=await sighting(db,q,0)
            for kwargs in [{'search_embedding_id':other.id},{'search_embedding_id':e.id,'appearance_id':b.id}]:
                with pytest.raises(HTTPException) as err:await get_identity_details(str(p.id),db,NS(role='admin'),**kwargs)
                assert err.value.status_code==404
    run(check())


def test_batch_and_single_serialization_preserve_provenance(isolated,monkeypatch):
    from backend.core.batch_search_service import BatchSearchService
    sessions,_=isolated
    async def check():
        async with sessions() as db:
            p=await person(db);_,a,e=await sighting(db,p,0)
            svc=AdvancedSearchService();matches,_=await svc._search_indexes(vector(0),db,'both',10,[],{})
            face=FaceInImage(0,{},.9,{},matches=matches)
            result=MultiSearchResult('demo',{},[face],[],1,{'total_faces_detected':1,'faces_searchable':1,'watchlist_alerts':0})
            from backend.routes.advanced_search import FaceMatchResponse
            payload=FaceMatchResponse(**svc.to_dict(result)['faces'][0]['matches'][0]).model_dump()
            assert payload['matched_embedding_id']==e.id and payload['matched_appearance_id']==a.id
            svc.search_multi_face=AsyncMock(return_value=result)
            batch=BatchSearchService();batch.initialize(svc)
            with patch('backend.core.search_audit.record_image_search',AsyncMock()):
                result=await batch.search_batch(db,[('synthetic.png',np.zeros((10,10,3),dtype=np.uint8),'abc')],1)
            assert result.results[0].status=='success'
            assert result.results[0].matches[0]['matched_embedding_id']==e.id
    run(check())


def test_http_serialization_keeps_context_and_unverified_status():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from backend.routes.identities import SearchResult
    from backend.routes.advanced_search import FaceMatchResponse
    payload={'identity_id':str(uuid.uuid4()),'display_name':None,'type':'unverified','similarity':.9,
             'confidence_band':'HIGH','best_snapshot_path':None,'appearances_count':1,
             'matched_embedding_id':12,'matched_appearance_id':34,'match_source':'sighting','identity_verification':'unverified'}
    app=FastAPI()
    @app.get('/quick',response_model=list[SearchResult])
    def quick():return [payload]
    @app.get('/advanced',response_model=FaceMatchResponse)
    def advanced():return payload
    with TestClient(app) as client:
        for path in ['/quick','/advanced']:
            response=client.get(path);assert response.status_code==200
            data=response.json();data=data[0] if isinstance(data,list) else data
            assert data['matched_embedding_id']==12 and data['matched_appearance_id']==34
            assert data['type']=='unverified' and data['display_name'] is None

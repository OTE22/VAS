"""Future captures only: synthetic PostgreSQL and CPU stubs, no live data."""
import importlib
import uuid
from datetime import datetime
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock
import numpy as np
import pytest
from sqlalchemy import select, func
from config import settings
from db_models import (Identity, IdentityType, IdentityStatus, IdentityEmbedding, IdentityAppearance,
                       Detection, Face, Pipeline)
from backend.core.identity_service import IdentityService
from backend.core.identity_index_pgvector import IdentityIndexPgVector
from backend.core.detection_evidence import persist_detection, EmbeddingLinkError
from backend.core.search_evidence import search_evidence
from backend.core.appearance_events import review_unknown_associations
from test_person_history_retention import isolated
from test_known_reference_isolation import vector, seed
from conftest import run_on_shared_loop as run


def service(folder, backend='pgvector'):
    svc=IdentityService(pgvector_index=IdentityIndexPgVector())
    svc.use_pgvector=backend=='pgvector'
    if backend=='faiss':
        from backend.core.vector_index.flat_faiss import FlatFaissIndex
        svc.vector_index=FlatFaissIndex(storage_dir=str(folder/'index'),dim=512,
                                       model_version=svc.embedding_model_version)
    return svc


def frame(person, embedding, path=None):
    return {'pipeline_id':'synthetic-camera',
        'detection':{'uuid':str(uuid.uuid4()),'pipeline_id':'synthetic-camera','timestamp':datetime.utcnow()},
        'faces':[{'identity_id':person.id,'name':'Synthetic','similarity':.95,
                  'label_state':'auto_known' if person.type==IdentityType.KNOWN else 'auto_unknown',
                  'face_image_path':str(path) if path else None,'quality':.2,
                  '_embedding_id':embedding.id if embedding else None,
                  '_requires_embedding':True,'_embedding_created_by_this_frame':True,
                  '_event_id':str(uuid.uuid4())}]}


def publish_service(monkeypatch,svc):
    monkeypatch.setattr(importlib.import_module('backend.core.identity_service'),'identity_service',svc)
    monkeypatch.setattr(settings,'LIVE_ALERTS_ENABLED',False)
    monkeypatch.setattr(settings,'WATCHLIST_ENABLED',False)
    monkeypatch.setattr(settings,'IDENTITY_AUTO_ENRICH_ENABLED',False)
    monkeypatch.setattr(settings,'SIMILARITY_THRESHOLD',.8)


@pytest.mark.parametrize('backend',['pgvector','faiss'])
def test_low_quality_known_capture_is_searchable_with_exact_evidence(isolated,monkeypatch,backend):
    sessions,folder=isolated;svc=service(folder,backend);publish_service(monkeypatch,svc)
    async def exercise():
        async with sessions() as db:
            db.add(Pipeline(pipeline_id='synthetic-camera'));await db.flush()
            person,_,reference=await seed(db,folder,svc)
            observation=await svc.save_embedding(person,vector(20),None,'synthetic-camera',.2,db,defer_commit=True)
            assert observation.faiss_index_type=='known_observation' and observation.quality==.2
            path=folder/'capture.jpg';path.write_bytes(b'synthetic')
            data=frame(person,observation,path)
            result=await persist_detection(db,detection_data=data);await db.commit()
            await db.refresh(observation)
            assert observation.detection_id==result.detection_id
            assert observation.faiss_index_type=='known_observation'
            hits=await search_evidence(db,vector(20),svc.embedding_model_version,'known',10)
            assert len(hits)==1 and hits[0].embedding.id==observation.id
            assert hits[0].snapshot_path==str(path) and hits[0].appearance_id is not None
            assert hits[0].verification=='verified_reference'
            # History may match itself but cannot extend trusted recognition.
            extra=await svc.save_embedding(person,vector(90),None,'synthetic-camera',.2,db,defer_commit=True)
            await db.flush()
            assert await svc.pgvector_index.search_known(vector(90),db,threshold=.8,
                model_version=svc.embedding_model_version)==[]
            replay=await persist_detection(db,detection_data=data);await db.commit()
            assert replay.detection_id==result.detection_id
            assert await db.scalar(select(func.count(IdentityAppearance.id)))==1
            assert await db.scalar(select(func.count(Detection.id)))==1
    run(exercise())


@pytest.mark.parametrize('backend',['pgvector','faiss'])
@pytest.mark.parametrize('quality',[.01,None])
def test_new_low_quality_unknown_is_history_not_reference(isolated,monkeypatch,backend,quality):
    sessions,folder=isolated;svc=service(folder,backend);publish_service(monkeypatch,svc)
    async def exercise():
        async with sessions() as db:
            db.add(Pipeline(pipeline_id='synthetic-camera'));await db.flush()
            person,eid=await svc._create_unknown_identity(vector(0),'synthetic-camera',None,db,quality,'fq1')
            observation=await db.get(IdentityEmbedding,eid)
            assert observation.faiss_index_type=='unknown_observation'
            assert observation.quality_scorer_version=='fq1'
            data=frame(person,observation)
            await persist_detection(db,detection_data=data);await db.commit()
            await db.refresh(observation)
            assert observation.faiss_index_type=='unknown_observation'
            assert await svc.pgvector_index.search_unknown(vector(0),db,threshold=.8,
                model_version=svc.embedding_model_version)==[]
            hits=await search_evidence(db,vector(0),svc.embedding_model_version,'unknown',10)
            assert len(hits)==1 and hits[0].verification=='unknown'
            apps=(await db.execute(select(IdentityAppearance))).scalars().all()
            _,review=await review_unknown_associations(db,person.id,apps)
            assert not review['reference_available']
            assert review['included_count']==0
    run(exercise())


@pytest.mark.parametrize('value',[np.zeros(512),np.full(512,np.nan),np.full(512,np.inf),np.ones(128)])
def test_invalid_observation_is_rejected_before_storage(isolated,value):
    sessions,folder=isolated;svc=service(folder)
    async def exercise():
        async with sessions() as db:
            db.add(Pipeline(pipeline_id='synthetic-camera'))
            person=Identity(type=IdentityType.KNOWN);db.add(person);await db.flush()
            with pytest.raises(ValueError):
                await svc.save_embedding(person,value,None,'synthetic-camera',.1,db,defer_commit=True)
            assert await db.scalar(select(func.count(IdentityEmbedding.id)))==0
    run(exercise())


def test_enrollment_quality_gate_stays_in_force(isolated):
    sessions,folder=isolated;svc=service(folder)
    async def exercise():
        async with sessions() as db:
            person=Identity(type=IdentityType.KNOWN);db.add(person);await db.flush()
            assert await svc.save_embedding(person,vector(0),None,None,.1,db,defer_commit=True) is None
            assert await db.scalar(select(func.count(IdentityEmbedding.id)))==0
    run(exercise())


@pytest.mark.parametrize('problem',['missing','wrong_owner','wrong_camera','invalid'])
def test_broken_required_evidence_rolls_back_entire_sighting(isolated,monkeypatch,problem):
    sessions,folder=isolated;svc=service(folder);publish_service(monkeypatch,svc)
    async def exercise():
        async with sessions() as db:
            db.add_all([Pipeline(pipeline_id='synthetic-camera'),Pipeline(pipeline_id='other-camera')])
            person=Identity(type=IdentityType.KNOWN);other=Identity(type=IdentityType.KNOWN)
            db.add_all([person,other]);await db.flush()
            obs=None
            if problem!='missing':
                obs=await svc.save_embedding(other if problem=='wrong_owner' else person,vector(0),None,
                    'other-camera' if problem=='wrong_camera' else 'synthetic-camera',.1,db,defer_commit=True)
                if problem=='invalid':obs.embedding=np.zeros(512);await db.flush()
            await db.commit()
            data=frame(person,obs)
            with pytest.raises(EmbeddingLinkError):
                async with db.begin():
                    await persist_detection(db,detection_data=data)
            for table in (Detection,Face,IdentityAppearance):
                assert await db.scalar(select(func.count(table.id)))==0
    run(exercise())


@pytest.mark.parametrize('enrichment',[False,True])
def test_capture_ingest_retains_low_quality_signature(isolated,monkeypatch,enrichment):
    sessions,folder=isolated;svc=service(folder);publish_service(monkeypatch,svc)
    monkeypatch.setattr(settings,'IDENTITY_AUTO_ENRICH_ENABLED',enrichment)
    quality=.9 if enrichment else .2
    from backend.services import image_processing as processing
    from backend.core import detection_storage
    image=np.full((300,300,3),140,np.uint8)
    result={'error':None,'stage':'done','kpss':np.zeros((1,5,2)),
        'bboxes':np.array([[20,20,150,180,.99]]),'face_bbox':[20,20,150,180],
        'aligned_face':np.zeros((112,112,3),np.uint8),'embedding':vector(20),
        'quality':quality,'quality_scorer':'fq1','acceptance':{'accepted':True}}
    writer=NS(add_detection=AsyncMock())
    for key,value in [('ensure_pipeline_registered',AsyncMock()),('resolve_display_name',AsyncMock(return_value='Synthetic')),
                      ('_decode_frame_sync',lambda _:image),('_process_crop_sync',lambda _:result),
                      ('batch_writer',writer),('FACE_TRACKING_ENABLED',False)]:
        monkeypatch.setattr(processing,key,value)
    monkeypatch.setattr(settings,'SAVE_CROPPED_IMAGES',False)
    monkeypatch.setattr(settings,'SKIP_UNKNOWN_FACES',False)
    path=folder/'accepted.jpg';path.write_bytes(b'synthetic')
    monkeypatch.setattr(detection_storage,'save_camera_crop',AsyncMock(return_value=str(path)))
    async def exercise():
        async with sessions() as db:
            db.add(Pipeline(pipeline_id='synthetic-camera'));await db.flush()
            person,_,_=await seed(db,folder,svc);await db.commit()
        output=await processing.process_image_async(image_bytes=b'synthetic',predictions=[
            {'bbox':[0,0,250,250],'class_name':'person','confidence':.99}],
            pipeline_id='synthetic-camera',use_batch_write=True,send_realtime_updates=False)
        assert len(output['faces'])==1
        packet=writer.add_detection.call_args.args[0]
        assert packet['faces'][0]['_requires_embedding']
        assert packet['faces'][0]['_embedding_id']
        async with sessions() as db:
            outcome=await persist_detection(db,detection_data=packet);await db.commit()
            hits=await search_evidence(db,vector(20),svc.embedding_model_version,'known',10)
            assert hits[0].appearance_id and hits[0].snapshot_path==str(path)
            assert hits[0].embedding.quality==quality
            assert hits[0].embedding.faiss_index_type==('known_enriched' if enrichment else 'known_observation')
            assert hits[0].embedding.image_id is None
            assert await db.scalar(select(func.count(IdentityEmbedding.id)))==2
    run(exercise())


def test_camera_storage_failure_cannot_silently_skip_evidence(isolated,monkeypatch):
    sessions,folder=isolated;svc=service(folder)
    monkeypatch.setattr(svc.pgvector_index,'add_embedding',AsyncMock(return_value=None))
    async def exercise():
        async with sessions() as db:
            person=Identity(type=IdentityType.KNOWN);db.add(person);await db.flush()
            with pytest.raises(RuntimeError,match='Failed to save camera'):
                await svc.save_embedding(person,vector(0),None,'synthetic-camera',.1,db,defer_commit=True)
    run(exercise())


def test_simultaneous_new_captures_keep_their_own_embeddings(isolated,monkeypatch):
    import asyncio
    sessions,folder=isolated;svc=service(folder);publish_service(monkeypatch,svc)
    async def exercise():
        async with sessions() as db:
            db.add(Pipeline(pipeline_id='synthetic-camera'));await db.flush()
            person,_,_=await seed(db,folder,svc)
            packets=[]
            for angle in (10,20,30):
                obs=await svc.save_embedding(person,vector(angle),None,'synthetic-camera',.2,db,defer_commit=True)
                packets.append(frame(person,obs))
            await db.commit()
        async def write(packet):
            async with sessions() as db:
                async with db.begin():
                    result=await persist_detection(db,detection_data=packet)
                return packet['faces'][0]['_embedding_id'],result.detection_id
        links=await asyncio.wait_for(asyncio.gather(*(write(p) for p in packets)),timeout=15)
        async with sessions() as db:
            for eid,did in links:
                assert (await db.get(IdentityEmbedding,eid)).detection_id==did
            assert await db.scalar(select(func.count(IdentityAppearance.id)))==3
            assert (await db.get(Identity,person.id)).appearances_count==3
    run(exercise())


@pytest.mark.parametrize('source_kind,target_type,target_status,already_reparented,expected_kind',[
    ('known_observation',IdentityType.KNOWN,IdentityStatus.ACTIVE,False,'known_observation'),
    ('known_observation',IdentityType.KNOWN,IdentityStatus.PROMOTED,True,'known_observation'),
    ('known_enriched',IdentityType.KNOWN,IdentityStatus.ACTIVE,False,'known_enriched'),
    ('known_enriched',IdentityType.KNOWN,IdentityStatus.PROMOTED,True,'known_enriched'),
    ('unknown_observation',IdentityType.KNOWN,IdentityStatus.ACTIVE,False,'unknown_observation'),
    ('unknown_observation',IdentityType.UNKNOWN,IdentityStatus.ACTIVE,True,'unknown_observation'),
    ('known_observation',IdentityType.UNKNOWN,IdentityStatus.ACTIVE,True,'unknown_observation'),
    ('known_enriched',IdentityType.UNKNOWN,IdentityStatus.ACTIVE,False,'unknown_observation'),
    ('unknown',IdentityType.KNOWN,IdentityStatus.PROMOTED,False,'known'),
])
def test_queued_survivor_resolution_preserves_observation_and_enrichment_scope(
        isolated,monkeypatch,source_kind,target_type,target_status,already_reparented,expected_kind):
    sessions,folder=isolated;svc=service(folder);publish_service(monkeypatch,svc)
    monkeypatch.setattr(settings,'IDENTITY_AUTO_ENRICH_ENABLED',True)
    monkeypatch.setattr(settings,'IDENTITY_ENRICH_MIN_SIMILARITY',.8)
    monkeypatch.setattr(settings,'IDENTITY_ENRICH_MIN_QUALITY',.6)
    async def exercise():
        async with sessions() as db:
            db.add(Pipeline(pipeline_id='synthetic-camera'));await db.flush()
            if source_kind.startswith('known'):
                source,_,_=await seed(db,folder,svc)
                if source_kind=='known_enriched':
                    eid=await svc._maybe_enrich_identity(source,vector(20),.94,.9,None,'synthetic-camera',db)
                    assert eid is not None
                    observation=await db.get(IdentityEmbedding,eid)
                else:
                    observation=await svc.save_embedding(source,vector(20),None,'synthetic-camera',.2,db,defer_commit=True)
            else:
                quality=.01 if source_kind=='unknown_observation' else .9
                source,eid=await svc._create_unknown_identity(vector(20),'synthetic-camera',None,db,quality,'fq1')
                observation=await db.get(IdentityEmbedding,eid)
            assert observation.faiss_index_type==source_kind
            packet=frame(source,observation)
            original_owner=source.id
            if target_type==IdentityType.KNOWN:
                target,_,_=await seed(db,folder,svc)
                target.status=target_status
            else:
                target=Identity(type=target_type,status=target_status)
                db.add(target);await db.flush()
            # Exercise the committed states seen by queued replay: a survivor
            # may already own the row, or an in-flight row may still name the
            # merged source. This is not a test of merge's asset consolidation.
            source.status=IdentityStatus.MERGED;source.merged_into_id=target.id
            if already_reparented:
                observation.identity_id=target.id
            await db.commit()
            result=await persist_detection(db,detection_data=packet);await db.commit()
            await db.refresh(observation)
            assert observation.identity_id==target.id
            assert observation.detection_id==result.detection_id
            assert observation.faiss_index_type==expected_kind
            assert observation.image_id is None
            # The durable input remains intact for retries and compensation.
            assert packet['faces'][0]['identity_id']==original_owner
            saved_face=await db.scalar(select(Face).where(Face.detection_id==result.detection_id))
            assert saved_face.identity_id==target.id
            if target_type==IdentityType.UNKNOWN:
                assert await svc.pgvector_index.search_unknown(vector(20),db,threshold=.8,
                    model_version=svc.embedding_model_version)==[]
            else:
                # 45 degrees misses the approved 0-degree anchor, but matches
                # the 20-degree capture. Only admitted enrichment may bridge it.
                matches=await svc.pgvector_index.search_known(vector(45),db,threshold=.8,
                    model_version=svc.embedding_model_version,allow_enriched=svc.auto_enrich_enabled)
                assert bool(matches)==(expected_kind=='known_enriched')
                if matches:
                    assert matches[0][0]==str(target.id)
                monkeypatch.setattr(settings,'IDENTITY_AUTO_ENRICH_ENABLED',False)
                assert await svc.pgvector_index.search_known(vector(45),db,threshold=.8,
                    model_version=svc.embedding_model_version,allow_enriched=svc.auto_enrich_enabled)==[]
            replay=await persist_detection(db,detection_data=packet);await db.commit()
            await db.refresh(observation)
            assert replay.detection_id==result.detection_id
            assert observation.faiss_index_type==expected_kind
    run(exercise())

"""Run only against the disposable vehicle_tests database; no production data."""
import asyncio
import base64
from datetime import datetime, timedelta, timezone
from io import BytesIO
import json
import os
from pathlib import Path
import sys
import uuid
from zipfile import ZipFile

import httpx
from fastapi import FastAPI
from sqlalchemy import create_engine, select, text, func
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from sqlalchemy.orm import Session
from db_models import Base, User, Pipeline, Identity, IdentityType, VehicleSighting, VehicleAudit
from db_connection import db_manager
from config import settings
from backend.routes import vehicles
from backend.vehicles import ingest, processing
from backend.vehicles.common import plate_key, bounded_bbox, image_from_bytes, EMBEDDING_VERSION
from PIL import Image, ImageDraw

SYNC='postgresql+psycopg2://postgres:vehicle-tests-only@127.0.0.1:5432/vehicle_tests'
ASYNC=SYNC.replace('+psycopg2','+asyncpg')
settings.STORAGE_DIR='/tmp/vehicle-tests-storage'
settings.VEHICLE_PILOT_PIPELINE_ID='4c79dc55-8ef0-4cbe-bbd6-dcd01d4d755b'
PILOT=settings.VEHICLE_PILOT_PIPELINE_ID


def png(label='TEST VEHICLE'):
    image=Image.new('RGB',(600,300),'#23547c'); ImageDraw.Draw(image).text((30,100),label,fill='white')
    stream=BytesIO(); image.save(stream,'PNG'); return stream.getvalue()


def setup():
    engine=create_engine(SYNC)
    with engine.begin() as conn:
        assert conn.scalar(text('SELECT current_database()'))=='vehicle_tests'
        conn.execute(text('DROP SCHEMA public CASCADE'))
        conn.execute(text('CREATE SCHEMA public'))
        conn.execute(text('CREATE EXTENSION IF NOT EXISTS vector'))
        added={'known_vehicles','vehicle_pipelines','vehicle_sightings','vehicle_person_links','vehicle_audit'}
        Base.metadata.create_all(conn,tables=[t for t in Base.metadata.sorted_tables if t.name not in added])
        import importlib.util
        from alembic.migration import MigrationContext
        from alembic.operations import Operations
        spec=importlib.util.spec_from_file_location('vehicle_migration','alembic/versions/a027b001cars_vehicle_workflow.py')
        migration=importlib.util.module_from_spec(spec);spec.loader.exec_module(migration)
        with Operations.context(MigrationContext.configure(conn)):
            migration.upgrade()
    with Session(engine) as db:
        if not db.get(User,1):
            db.add(User(id=1,username='vehicle_test_admin',email='vehicle-test@example.invalid',password_hash='unused',role='admin'))
            db.add(Pipeline(pipeline_id=PILOT,location_name='IT-DIRECT-CAM-1'))
            db.add(Pipeline(pipeline_id='other-test-camera',location_name='Other test camera'))
            person=Identity(type=IdentityType.KNOWN,display_name='Vehicle Test Person')
            db.add(person); db.commit()
    engine.dispose()


async def main():
    setup()
    db_manager.engine=create_async_engine(ASYNC)
    db_manager.session_maker=async_sessionmaker(db_manager.engine,expire_on_commit=False)
    app=FastAPI(); app.include_router(vehicles.router)
    app.dependency_overrides[vehicles.admin]=lambda: {'id':1,'username':'vehicle_test_admin','role':'admin'}
    transport=httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport,base_url='http://test',headers={'X-Requested-With':'XMLHttpRequest'}) as client:
        async def check(method,path,status=200,**kwargs):
            response=await client.request(method,'/api/vehicles'+path,**kwargs)
            assert response.status_code==status,(method,path,response.status_code,response.text[:1000])
            return response
        assert plate_key(' b-١٢٣ ۴۵۶ ')== 'B123456'
        try: bounded_bbox([float('nan'),0,10,10],100,100); raise AssertionError('NaN box accepted')
        except ValueError: pass
        for payload in (b'bad',b''):
            try:image_from_bytes(payload);raise AssertionError('bad image accepted')
            except (ValueError,OSError):pass
        body={'name':'Test car','plate':'B 123456','country':'LB','make':'Toyota','model':'Corolla','color':'Blue','notes':'=FORMULA must remain text'}
        await check('POST','/known',403,json=body,headers={'X-Requested-With':''})
        car=(await check('POST','/known',201,json=body)).json()
        await check('POST','/known',409,json={**body,'plate':'b-١٢٣٤٥٦'})
        await check('PUT','/known/'+car['id'],409,json={**body,'version':99})
        car=(await check('POST','/known/'+car['id']+'/photo',data={'version':car['version']},files={'photo':('car.png',png(),'image/png')})).json()
        await check('GET','/known/'+car['id']+'/photo')
        person=(await check('GET','/people?q=Vehicle')).json()['items'][0]
        link=(await check('POST','/known/'+car['id']+'/people',201,json={'identity_id':person['id'],'relationship_type':'associated','evidence':'Reviewed test reference'})).json()
        await check('POST','/known/'+car['id']+'/people/'+link['id']+'/revoke')
        await check('PUT','/pipelines/other-test-camera',422,json={'enabled':True})
        await check('PUT','/pipelines/'+PILOT,json={'enabled':True,'ocr_profile':'en','country':'LB'})
        payload={'event_id':uuid.uuid4().hex,'captured_at':datetime.now(timezone.utc).isoformat(),
                 'image':base64.b64encode(png()).decode(),'results':{'predictions':[{'class_name':'car','confidence':.92,'bbox':[0,0,600,300],'track_id':7}]}}
        assert ingest.vehicle_event(payload) and ingest.only_vehicles(payload)
        face={'predictions':[{'class_name':'person'}]}
        assert not ingest.vehicle_event(face) and not ingest.only_vehicles(face)
        outcomes=await asyncio.gather(*(ingest.accept_vehicle_event(PILOT,payload) for _ in range(4)))
        assert sum(r['queued'] for r in outcomes)==1,outcomes
        row=(await check('GET','/sightings')).json()['items'][0]
        await check('GET','/sightings/'+row['id']+'/image/vehicle')
        await check('GET','/sightings?start=2026-01-01T00:00:00',422)
        await check('GET','/sightings?start=2026-12-01T00:00:00Z&end=2026-01-01T00:00:00Z',422)
        assert (await check('GET','/sightings?plate=NOTHERE')).json()['total']==0
        async def fake_vision(operation,payload,profile='en'):
            return {'embedding':[1.0]+[0.0]*255,'embedding_version':EMBEDDING_VERSION,
                    'plates':[{'crop_b64':base64.b64encode(png('B 123456')).decode(),'detection_score':.9,
                               'ocr':{'lines':[{'text':'B 123456','score':.98}]}}]}
        original_processing=processing.vision_request
        original=processing.vision_request; processing.vision_request=fake_vision
        try: await processing.process_one()
        finally: processing.vision_request=original
        row=(await check('GET','/sightings/'+row['id'])).json()
        assert row['processing_state']=='done' and row['plate']=='B 123456' and len(row['reference_candidates'])==1,row
        await check('GET','/sightings/'+row['id']+'/image/plate')
        confirmed=(await check('POST','/sightings/'+row['id']+'/review',json={'version':row['version'],'decision':'confirm','plate':'B123456','country':'LB','known_vehicle_id':car['id']})).json()
        await check('POST','/sightings/'+row['id']+'/review',409,json={'version':row['version'],'decision':'unreadable'})
        await check('POST','/sightings/'+row['id']+'/review',json={'version':confirmed['version'],'decision':'reject','known_vehicle_id':car['id']})
        detail=(await check('GET','/sightings/'+row['id'])).json()
        assert not detail['reference_candidates'] and detail['known_vehicle_id'] is None
        assert len(detail['history'])==2
        original=vehicles.vision_request; vehicles.vision_request=fake_vision
        try:
            search=(await check('POST','/search/image',files={'photo':('car.png',png(),'image/png')})).json()
            assert search['plate_matches'][0]['id']==row['id'],search
        finally: vehicles.vision_request=original
        report=await check('GET','/export.xlsx?ids='+row['id'])
        with ZipFile(BytesIO(report.content)) as z:
            data=z.read('xl/worksheets/sheet1.xml').decode()
            assert row['id'] in data and '<f>' not in data
            assert len(z.namelist())==6
        # Confirm unknown/no-plate evidence is retained and can be found visually.
        async with db_manager.get_session() as db:
            stored=await db.get(VehicleSighting,uuid.UUID(row['id'])); stored.plate=None; stored.plate_key=None; stored.review_state='unreadable'
        vehicles.vision_request=fake_vision
        try:
            search=(await check('POST','/search/image',files={'photo':('car.png',png(),'image/png')})).json()
            assert search['possible_matches'][0]['id']==row['id'] and not search['plate_matches'],search
        finally: vehicles.vision_request=original
        # Worker outages retain images and retry; expired final leases fail visibly.
        async with db_manager.get_session() as db:
            stored=await db.get(VehicleSighting,uuid.UUID(row['id']))
            stored.processing_state='pending'; stored.attempts=0
            stored.next_attempt_at=datetime.utcnow()-timedelta(seconds=1)
        async def unavailable(*args,**kwargs):
            raise RuntimeError('Simulated worker outage')
        processing.vision_request=unavailable
        try: await processing.process_one()
        finally: processing.vision_request=original_processing
        detail=(await check('GET','/sightings/'+row['id'])).json()
        assert detail['processing_state']=='pending' and detail['attempts']==1
        await check('GET','/sightings/'+row['id']+'/image/vehicle')
        async with db_manager.get_session() as db:
            stored=await db.get(VehicleSighting,uuid.UUID(row['id']))
            stored.processing_state='processing'; stored.attempts=5
            stored.lease_until=datetime.utcnow()-timedelta(seconds=1)
        await processing.process_one()
        detail=(await check('GET','/sightings/'+row['id'])).json()
        assert detail['processing_state']=='failed' and detail['error_code']=='WorkerLeaseExpired'
        # API without the admin override must reject anonymous access.
        app.dependency_overrides.clear()
        response=await client.get('/api/vehicles/known')
        assert response.status_code in (401,403),response.status_code
    await db_manager.engine.dispose()
    print('PASS: schema, admin access, CSRF, known-car photo, duplicate plate normalization, edit conflict, person links, pilot scope, concurrent webhook dedup, durable processing, review/rejection, image retrieval, image search including unknown plates, time filters and XLSX export.')


if __name__=='__main__': asyncio.run(main())

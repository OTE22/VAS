"""Quick Search regressions. No fixture changes survive the outer transaction."""
import asyncio
import uuid
from io import BytesIO
from datetime import datetime
from types import SimpleNamespace as NS
from unittest.mock import patch, AsyncMock
import pytest
from PIL import Image
from fastapi import HTTPException, Response, UploadFile
from config import settings
import db_models as m
from test_merge_reliability import sandbox, vector
from backend.core.quick_image_search import rank_people, search_dates, decode_search_image


def test_dates_and_resolution_limits():
    start, end = search_dates('2026-09-13T10:00:00+03:00', None)
    assert start == datetime(2026,9,13,7)
    for dates in [('bad',None), ('2026-09-14','2026-09-13')]:
        with pytest.raises(HTTPException) as error: search_dates(*dates)
        assert error.value.status_code == 422
    payload = BytesIO(); Image.new('RGB',(10001,1)).save(payload,format='PNG')
    with pytest.raises(HTTPException) as error: decode_search_image(payload.getvalue())
    assert error.value.status_code == 413
    with pytest.raises(HTTPException): decode_search_image(b'not an image')


def test_filters_before_limit_duplicates_and_model_isolation():
    async def run():
        async with sandbox() as (db, service, admin):
            model = 'quick-qa-' + uuid.uuid4().hex[:12]
            camera = m.Pipeline(pipeline_id='quick-qa-' + uuid.uuid4().hex)
            people = [m.Identity(type=m.IdentityType.UNKNOWN, status=m.IdentityStatus.ACTIVE) for _ in range(4)]
            db.add_all([camera,*people]); await db.flush()
            for i, person in enumerate(people):
                for _ in range(55 if i == 0 else 1):
                    db.add(m.IdentityEmbedding(identity_id=person.id, embedding=vector(i*5),
                        embedding_model_version=model if i != 3 else 'different-model'))
            for i in [1,2,3]:
                for minute in [1,2]:
                    db.add(m.IdentityAppearance(identity_id=people[i].id, pipeline_id=camera.pipeline_id,
                        start_time=datetime(2026,9,13,10,minute)))
            await db.flush()
            rows = await rank_people(db,vector(0),model,'unknown',2, pipeline_id=camera.pipeline_id)
            assert [p.id for p, _ in rows] == [people[1].id,people[2].id]
            rows = await rank_people(db,vector(0),model,'unknown',3)
            assert len(rows) == 3 and {p.id for p,_ in rows} == {p.id for p in people[:3]}
            people[1].status = m.IdentityStatus.INACTIVE; await db.flush()
            rows = await rank_people(db,vector(0),model,'both',3,pipeline_id=camera.pipeline_id)
            assert [p.id for p,_ in rows] == [people[2].id]
    asyncio.run(run())


@pytest.mark.parametrize('scope,count', [('bad',10),('both',0),('both',101)])
def test_route_rejects_invalid_search_parameters(scope,count):
    async def run():
        from backend.routes.identities import search_by_image
        with pytest.raises(HTTPException) as error:
            await search_by_image(Response(), UploadFile(BytesIO(b'')),scope,count,None,None,None,None,NS(id=1))
        assert error.value.status_code == 422
    asyncio.run(run())


def test_route_bounds_upload_before_decode():
    async def run():
        import backend.routes.identities as route
        upload = NS(read=AsyncMock(return_value=b'12345'))
        with patch.object(settings,'MAX_FILE_SIZE',4), patch.object(route,'get_identity_service',return_value=NS(embedding_model_version='qa')):
            with pytest.raises(HTTPException) as error:
                await route.search_by_image(Response(),upload,'both',10,None,None,None,None,NS(id=1))
        assert error.value.status_code == 413
        upload.read.assert_awaited_once_with(5)
    asyncio.run(run())


def test_group_photo_headers_and_one_audit_without_identity_writes():
    async def run():
        import backend.routes.identities as route
        import backend.core.face_extraction as extraction
        payload=BytesIO(); Image.new('RGB',(64,64)).save(payload,format='PNG'); payload.seek(0)
        faces=[NS(bbox=[0,0,20,20],padded_retry=False), NS(bbox=[0,0,40,40],padded_retry=False)]
        async with sandbox() as (db, service, admin):
            service.embedding_model_version='quick-qa-' + uuid.uuid4().hex[:12]
            response=Response()
            with patch.object(route,'get_identity_service',return_value=service), patch.object(extraction,'extract_faces',return_value=faces), patch.object(extraction,'select_largest',return_value=faces[1]), patch.object(extraction,'embed_face_normalized',return_value=vector(0)) as embed, patch('backend.core.search_audit.record_image_search',AsyncMock()) as audit:
                result=await route.search_by_image(response,UploadFile(payload),'both',10,None,None,None,db,NS(id=admin,username='qa'))
            assert result == [] and response.headers['X-Faces-Detected']=='2'
            assert embed.call_args.args[1] is faces[1]
            audit.assert_awaited_once()
            assert audit.call_args.kwargs['faces_count']==2
    asyncio.run(run())

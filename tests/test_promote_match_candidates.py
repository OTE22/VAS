"""Read-only preview API contracts; isolated PostgreSQL, no live-server cleanup."""
import json
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, func
from conftest import run_on_shared_loop as run
from test_person_history_retention import isolated
from test_promotion_reliability import seed, extraction
from test_known_reference_isolation import service, seed as known_seed, vector
import db_models as m


def test_http_requires_selected_event_and_checksum_before_mutation(isolated):
    sessions,folder=isolated
    async def exercise():
        import backend.routes.identities as route
        async with sessions() as db:
            source,user,rows=await seed(db,folder)
            app=FastAPI();app.include_router(route.router)
            app.dependency_overrides[route.get_db]=lambda:db
            app.dependency_overrides[route.get_current_user]=lambda:user
            async with AsyncClient(transport=ASGITransport(app=app),base_url='http://synthetic.test') as client:
                path=f'/api/admin/unknown/{source.id}'
                assert (await client.get(path+'/match-candidates')).status_code==422
                assert (await client.get(path+'/match-candidates?appearance_id=0')).status_code==422
                assert (await client.post(path+'/promote',json={'display_name':'Synthetic','decision':'create_new'})).status_code==422
                assert (await client.post(path+'/promote',json={'display_name':'Synthetic','decision':'create_new','appearance_id':rows[0].appearance.id,'photo_sha256':'invalid'})).status_code==422
            assert await db.scalar(select(func.count(m.Identity.id)))==1
            assert source.type==m.IdentityType.UNKNOWN
    run(exercise())


def test_preview_uses_saved_event_not_mutable_group_portrait_and_only_known_candidates(isolated,extraction):
    sessions,folder=isolated
    async def exercise():
        import backend.routes.identities as route
        async with sessions() as db:
            source,user,rows=await seed(db,folder)
            known,image,embedding=await known_seed(db,folder,service(),angle=0)
            source.best_snapshot_path=str(folder/'not-a-real-portrait.jpg');await db.commit()
            first=await route.unknown_match_candidates(str(source.id),rows[0].appearance.id,db,user)
            second=await route.unknown_match_candidates(str(source.id),rows[1].appearance.id,db,user)
            assert first['selection']['photo_sha256']==rows[0].checksum
            assert second['selection']['photo_sha256']==rows[1].checksum
            assert [c['identity_id'] for c in first['candidates']]==[str(known.id)]
            assert not db.new and not db.dirty and not db.deleted
            for table in (m.SearchHistory,m.WatchlistAlert,m.IdentityMerge,m.IdentityAuditLog):
                assert await db.scalar(select(func.count()).select_from(table))==0
    run(exercise())


@pytest.mark.parametrize('case',['missing','unusable'])
def test_unusable_preview_blocks_instead_of_offering_blind_promotion(isolated,extraction,monkeypatch,case):
    sessions,folder=isolated
    async def exercise():
        import backend.routes.identities as route
        import backend.core.enrollment_service as enrollment
        async with sessions() as db:
            source,user,rows=await seed(db,folder)
            if case=='missing': rows[0].path.unlink()
            else:
                def bad(*a,**kw): raise ValueError('No single face')
                monkeypatch.setattr(enrollment,'prepare_upload',bad)
            response=await route.unknown_match_candidates(str(source.id),rows[0].appearance.id,db,user)
            assert response.status_code==400 and 'selection' not in json.loads(response.body)
            assert source.type==m.IdentityType.UNKNOWN and not db.new and not db.dirty
    run(exercise())

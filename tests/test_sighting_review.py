"""Durable direct-pair review against a disposable DB; no production data."""
from datetime import datetime, timedelta
import asyncio
from types import SimpleNamespace
import pytest
from sqlalchemy import select, func
from fastapi import HTTPException, FastAPI
import httpx
from conftest import run_on_shared_loop as run
from test_person_history_retention import isolated
from test_unknown_reference_isolation import sightings
from db_models import User, IdentityAuditLog, IdentityEmbedding, IdentityImage, IdentityType, Face, Identity
from backend.core.sighting_review import preview_review, save_review, ACTION
from backend.core.appearance_events import review_unknown_associations
from backend.core.data_retention import DataRetentionManager
from config import settings


async def seed(db, folder):
    person, apps, vectors = await sightings(db, folder)
    user = User(username='synthetic-reviewer', email='review@example.invalid', password_hash='not-a-login', role='admin')
    db.add(user); await db.commit()
    return person, apps, vectors, user


async def decide(db, person, apps, user, decision='confirmed', target=2):
    preview = await preview_review(db, person.id, apps[0].id, apps[target].id, user)
    result = await save_review(db, person.id, apps[target].id, user,
        preview_token=preview['preview_token'], decision=decision, note='Synthetic review')
    await db.commit()
    return result


def test_confirm_persists_is_symmetric_and_does_not_enroll_or_reassign(isolated, monkeypatch):
    monkeypatch.setattr(settings, 'UNKNOWN_SIMILARITY_THRESHOLD', .8)
    sessions, folder = isolated
    async def exercise():
        async with sessions() as db:
            person, apps, vectors, user = await seed(db, folder)
            baseline = [(v.id, v.identity_id, v.image_id, v.embedding.tobytes()) for v in vectors]
            review = await preview_review(db, person.id, apps[0].id, apps[2].id, user)
            assert review['can_decide'] and not review['can_undo']
            assert not db.new and not db.dirty
            await decide(db, person, apps, user)
            iid=person.id;ids=[a.id for a in apps]
            assert baseline == [(v.id, v.identity_id, v.image_id, v.embedding.tobytes()) for v in vectors]
            assert await db.scalar(select(func.count(IdentityImage.id))) == 0
            assert await db.scalar(select(func.count(Identity.id))) == 1
        async with sessions() as db:
            from db_models import IdentityAppearance
            apps=(await db.scalars(select(IdentityAppearance).order_by(IdentityAppearance.id))).all()
            result, meta=await review_unknown_associations(db,iid,apps,ids[0])
            assert result[ids[2]]['association_status']=='operator_confirmed' and meta['included_count']==2
            assert result[ids[1]]['association_status']=='review_required'  # no whole-group approval
            assert result[ids[2]]['manual_review']['reviewer']=='synthetic-reviewer'
            reverse,_=await review_unknown_associations(db,iid,apps,ids[2])
            assert reverse[ids[0]]['association_status']=='operator_confirmed'
            different,_=await review_unknown_associations(db,iid,apps,ids[1])
            assert all(r['association_status']!='operator_confirmed' for r in different.values())
    run(exercise())


def test_reject_overrides_automatic_similarity_and_undo_restores_it(isolated):
    sessions,folder=isolated
    async def exercise():
        async with sessions() as db:
            p,apps,vecs,user=await seed(db,folder)
            result,_=await review_unknown_associations(db,p.id,apps,apps[0].id)
            assert result[apps[1].id]['association_status']=='consistent'
            await decide(db,p,apps,user,'rejected',target=1)
            result,_=await review_unknown_associations(db,p.id,apps,apps[0].id)
            assert result[apps[1].id]['association_status']=='operator_rejected'
            assert _['rejected_count']==1 and _['review_count']==1
            await decide(db,p,apps,user,'clear',target=1)
            result,_=await review_unknown_associations(db,p.id,apps,apps[0].id)
            assert result[apps[1].id]['association_status']=='consistent'
            assert await db.scalar(select(func.count(IdentityAuditLog.id)))==2
    run(exercise())


@pytest.mark.parametrize('change',['photo_bytes','photo_path','ownership','competing_review','expired_token','wrong_actor'])
def test_stale_or_mismatched_previews_cannot_save(isolated,change):
    sessions,folder=isolated
    async def exercise():
        async with sessions() as db:
            p,apps,vecs,user=await seed(db,folder)
            preview=await preview_review(db,p.id,apps[0].id,apps[2].id,user)
            token=preview['preview_token'];actor=user
            if change=='photo_bytes':(folder/'synthetic-2.jpg').write_bytes(b'changed after preview')
            elif change=='photo_path':apps[2].best_snapshot_path=str(folder/'synthetic-1.jpg');await db.flush()
            elif change=='ownership':
                other=Identity(type=IdentityType.UNKNOWN);db.add(other);await db.flush();apps[2].identity_id=other.id;await db.flush()
            elif change=='competing_review':await decide(db,p,apps,user,'rejected')
            elif change=='wrong_actor':actor=SimpleNamespace(id=user.id+99,role='admin',username='different')
            elif change=='expired_token':
                import jwt
                claims=jwt.decode(token,settings.JWT_SECRET_KEY,algorithms=['HS256'],audience='vas:sighting-review')
                claims['exp']=1;token=jwt.encode(claims,settings.JWT_SECRET_KEY,algorithm='HS256')
            with pytest.raises(HTTPException) as error:
                await save_review(db,p.id,apps[2].id,actor,preview_token=token,decision='confirmed')
            assert error.value.status_code in (403,409)
            assert await db.scalar(select(func.count(IdentityAuditLog.id)))==(1 if change=='competing_review' else 0)
    run(exercise())


def test_missing_vectors_can_be_human_reviewed_but_missing_or_ambiguous_photo_cannot(isolated):
    sessions,folder=isolated
    async def exercise():
        async with sessions() as db:
            p,apps,vecs,user=await seed(db,folder)
            await db.delete(vecs[2]);await db.flush()
            await decide(db,p,apps,user)
            result,_=await review_unknown_associations(db,p.id,apps,apps[0].id)
            assert result[apps[2].id]['association_status']=='operator_confirmed'
            (folder/'synthetic-2.jpg').unlink()
            preview=await preview_review(db,p.id,apps[0].id,apps[2].id,user)
            assert not preview['can_decide'] and preview['can_undo']
            with pytest.raises(HTTPException):
                await save_review(db,p.id,apps[2].id,user,preview_token=preview['preview_token'],decision='rejected')
            await save_review(db,p.id,apps[2].id,user,preview_token=preview['preview_token'],decision='clear');await db.commit()
            db.add(Face(identity_id=p.id,detection_id=apps[1].detection_id,name='Synthetic',similarity=.5,face_image_path=apps[1].best_snapshot_path));await db.flush()
            preview=await preview_review(db,p.id,apps[0].id,apps[1].id,user)
            assert not preview['can_decide']
    run(exercise())


def test_review_survives_log_cleanup_and_photo_expiry(isolated,monkeypatch):
    monkeypatch.setattr(settings,'AUDIT_LOG_RETENTION_DAYS',1)
    sessions,folder=isolated
    async def exercise():
        async with sessions() as db:
            p,apps,vecs,user=await seed(db,folder)
            await decide(db,p,apps,user)
            event=await db.scalar(select(IdentityAuditLog));event.created_at=datetime.utcnow()-timedelta(days=500)
            db.add(IdentityAuditLog(user_id=user.id,username=user.username,identity_id=p.id,action_type='view',created_at=event.created_at))
            apps[2].best_snapshot_path=None;await db.commit()
            dry=await DataRetentionManager()._cleanup_auxiliary(db,True)
            assert dry['audit_logs_deleted']==1
            result=await DataRetentionManager()._cleanup_auxiliary(db,False);await db.commit()
            assert result['audit_logs_deleted']==1
            events=(await db.scalars(select(IdentityAuditLog))).all();assert len(events)==1 and events[0].action_type==ACTION
            associations,_=await review_unknown_associations(db,p.id,apps,apps[0].id)
            assert associations[apps[2].id]['association_status']=='operator_confirmed'
    run(exercise())


def test_known_selected_sighting_shows_review_without_becoming_enrollment(isolated):
    sessions,folder=isolated
    async def exercise():
        from backend.routes.identities import get_identity_details
        async with sessions() as db:
            p,apps,vecs,user=await seed(db,folder);p.type=IdentityType.KNOWN;await db.commit()
            await decide(db,p,apps,user)
            payload=await get_identity_details(str(p.id),db,user,appearance_id=apps[0].id)
            assert payload['association_review']['can_review']
            assert next(a for a in payload['appearances'] if a['id']==apps[2].id)['association_status']=='operator_confirmed'
            assert await db.scalar(select(func.count(IdentityImage.id)))==0
    run(exercise())


def test_http_permissions_csrf_and_transactional_save(isolated):
    sessions,folder=isolated
    async def exercise():
        from backend.routes.identities import router
        from backend.auth.auth_service import get_current_user
        from db_connection import get_db
        async with sessions() as db:
            p,apps,vecs,user=await seed(db,folder);identity_id=p.id;first,last=apps[0].id,apps[2].id
            actor={'user':user}
        app=FastAPI();app.include_router(router)
        async def database():
            async with sessions() as db:yield db
        app.dependency_overrides[get_db]=database
        app.dependency_overrides[get_current_user]=lambda:actor['user']
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
            url=f'/api/admin/identity/{identity_id}/sightings/{last}/review'
            actor['user']=SimpleNamespace(role='user',id=user.id,username=user.username)
            assert (await client.get(url,params={'reference_appearance_id':first})).status_code==403
            actor['user']=user
            preview=await client.get(url,params={'reference_appearance_id':first})
            assert preview.status_code==200 and preview.headers['cache-control']=='private, no-store'
            body={'preview_token':preview.json()['preview_token'],'decision':'confirmed'}
            assert (await client.post(url,json=body)).status_code==403
            saved=await client.post(url,json=body,headers={'X-Requested-With':'XMLHttpRequest'})
            assert saved.status_code==200 and saved.json()['success']
            assert (await client.post(url,json=body,headers={'X-Requested-With':'XMLHttpRequest'})).status_code==409
        async with sessions() as db:assert await db.scalar(select(func.count(IdentityAuditLog.id)))==1
    run(exercise())



def test_concurrent_reviews_only_one_commits(isolated):
    sessions, folder = isolated
    async def exercise():
        async with sessions() as db:
            person, apps, vectors, user = await seed(db, folder)
            preview = await preview_review(db, person.id, apps[0].id, apps[2].id, user)
            iid, aid = person.id, apps[2].id
        async def submit(decision):
            async with sessions() as db:
                try:
                    await save_review(db, iid, aid, user, preview_token=preview['preview_token'], decision=decision)
                    await db.commit()
                    return 'saved'
                except HTTPException as error:
                    await db.rollback()
                    return error.status_code
        outcomes = await asyncio.gather(submit('confirmed'), submit('rejected'))
        assert outcomes.count('saved') == 1 and outcomes.count(409) == 1
        async with sessions() as db:
            assert await db.scalar(select(func.count(IdentityAuditLog.id))) == 1
    run(exercise())


def test_changed_sighting_invalidates_persisted_approval(isolated):
    sessions, folder = isolated
    async def exercise():
        async with sessions() as db:
            p, apps, vectors, user = await seed(db, folder)
            await decide(db, p, apps, user)
            apps[2].best_snapshot_path = str(folder/'replacement.jpg')
            await db.commit()
            result, _ = await review_unknown_associations(db, p.id, apps, apps[0].id)
            assert result[apps[2].id]['association_status'] == 'unverified'
            assert result[apps[2].id]['manual_review']['stale']
            assert await db.scalar(select(func.count(IdentityAuditLog.id))) == 1
    run(exercise())


def test_review_photo_cannot_follow_symlinks_outside_storage(isolated, tmp_path_factory):
    sessions, folder = isolated
    outside = tmp_path_factory.mktemp('outside')/'unrelated.jpg'
    outside.write_bytes(b'synthetic file outside permitted media')
    async def exercise():
        async with sessions() as db:
            p, apps, vectors, user = await seed(db, folder)
            image = folder/'synthetic-2.jpg'
            image.unlink(); image.symlink_to(outside)
            preview = await preview_review(db, p.id, apps[0].id, apps[2].id, user)
            assert not preview['can_decide']
            assert preview['sighting']['snapshot_url'] is None
    run(exercise())

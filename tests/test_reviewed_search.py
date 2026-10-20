"""Operator-reviewed Advanced Search, using synthetic data in disposable PG."""
import pytest
from fastapi import HTTPException
from sqlalchemy import select, func

import db_models as m
from config import settings
from conftest import run_on_shared_loop as run
from test_person_history_retention import isolated
from test_search_filters import person, sighting, enrollment, vector, MODEL
from test_search_appearances import context
from backend.core import search_appearances as search
from backend.core.sighting_review import preview_review, save_review


@pytest.fixture(autouse=True)
def configuration(monkeypatch):
    monkeypatch.setattr(settings, 'SIMILARITY_THRESHOLD', .8)
    monkeypatch.setattr(settings, 'RECOGNITION_MODEL', MODEL + '.onnx')


async def seed(db, folder, known=False):
    p = await person(db, m.IdentityType.KNOWN if known else m.IdentityType.UNKNOWN)
    rows = [await sighting(db, p, angle, camera=camera, day=day)
            for angle, camera, day in [(0, 'entrance', 1), (70, 'exit', 2), (100, 'garage', 3)]]
    for face, app, embedding in rows:
        path = folder/app.best_snapshot_path.removeprefix('storage/')
        path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(b'synthetic evidence')
    user = m.User(username='review-tester', email='review@example.invalid', password_hash='not-a-login', role='admin')
    db.add(user); await db.commit()
    return p, rows, user


async def decide(db, p, reference, target, user, decision='confirmed'):
    preview = await preview_review(db, p.id, reference.id, target.id, user)
    await save_review(db, p.id, target.id, user, preview_token=preview['preview_token'], decision=decision)
    await db.commit()


def test_confirmed_angle_enters_search_but_does_not_seed_other_matches(isolated):
    sessions, folder = isolated
    async def exercise():
        async with sessions() as db:
            p, rows, user = await seed(db, folder)
            a, b, c = [row[1] for row in rows]
            assert (await search.search_appearances(db, await context(db), 0))['total'] == 1
            await decide(db, p, a, b, user)
            await decide(db, p, b, c, user)
            page = await search.search_appearances(db, await context(db), 0)
            assert [r['id'] for r in page['appearances']] == [a.id, b.id]
            assert (page['direct_count'], page['operator_confirmed_count']) == (1, 1)
            extra = page['appearances'][1]
            assert extra['similarity'] is None and extra['matched_embedding_id'] is None
            assert extra['match_basis'] == 'operator_confirmed'
            assert extra['manual_review']['reference_appearance_id'] == a.id
            assert extra['manual_review']['reference_similarity'] == pytest.approx(1.)
            assert extra['snapshot_url'].endswith(f'evidence-{b.detection_id}.jpg')
            assert await db.scalar(select(func.count(m.IdentityEmbedding.id))) == 3
            # The relationship works when the query matches the other angle.
            reverse = await search.search_appearances(db, await context(db, vectors={'0':vector(70).tolist()}), 0)
            assert a.id in [r['id'] for r in reverse['appearances']]
    run(exercise())


def test_result_filters_apply_even_when_the_direct_reference_is_outside_them(isolated):
    sessions, folder = isolated
    async def exercise():
        async with sessions() as db:
            p, rows, user = await seed(db, folder)
            a, b, c = [row[1] for row in rows]
            await decide(db, p, a, b, user)
            filters = dict(pipeline_id='exit', date_from='2026-01-02T00:00:00', date_to='2026-01-02T23:59:59.999999')
            page = await search.search_appearances(db, await context(db, filters=filters), 0)
            assert [r['id'] for r in page['appearances']] == [b.id]
            assert page['direct_count'] == 0 and page['operator_confirmed_count'] == 1
            assert (await search.search_appearances(db, await context(db, filters={'pipeline_id':'garage'}), 0))['total'] == 0
            assert (await search.search_appearances(db, await context(db, exclude_ids=[str(p.id)]), 0))['total'] == 0
            assert (await search.search_appearances(db, await context(db, scope='known'), 0))['total'] == 0
            assert (await search.search_appearances(db, await context(db, watermark=a.id), 0))['total'] == 1
    run(exercise())


@pytest.mark.parametrize('change', ['reject', 'undo', 'target_owner', 'reference_owner', 'target_photo', 'reference_photo'])
def test_reversed_or_stale_decisions_do_not_expand_results(isolated, change):
    sessions, folder = isolated
    async def exercise():
        async with sessions() as db:
            p, rows, user = await seed(db, folder)
            a, b, c = [row[1] for row in rows]
            await decide(db, p, a, b, user)
            if change in ('reject','undo'):
                await decide(db, p, a, b, user, 'rejected' if change=='reject' else 'clear')
            elif change.endswith('_owner'):
                other = await person(db)
                (a if change=='reference_owner' else b).identity_id = other.id
            else:
                (a if change=='reference_photo' else b).best_snapshot_path = 'storage/replacement.jpg'
            await db.flush()
            page = await search.search_appearances(db, await context(db), 0)
            assert page['operator_confirmed_count'] == 0
            assert b.id not in [r['id'] for r in page['appearances']]
    run(exercise())


def test_manual_evidence_survives_photo_and_embedding_expiry_without_inheriting_a_known_name(isolated):
    sessions, folder = isolated
    async def exercise():
        async with sessions() as db:
            p, rows, user = await seed(db, folder, known=True)
            await enrollment(db, p, 0)
            a, b, c = [row[1] for row in rows]
            await decide(db, p, a, b, user)
            await db.delete(rows[1][2]); await db.delete(rows[1][0]); b.best_snapshot_path = None
            await db.commit()
            page = await search.search_appearances(db, await context(db), 0)
            assert page['appearances'][0]['type'] == 'known'
            extra = next(r for r in page['appearances'] if r['id']==b.id)
            assert extra['snapshot_url'] is None and extra['similarity'] is None
            assert extra['display_name'] is None and extra['type']=='unverified'
            assert not extra.get('watchlist_match')
    run(exercise())


def test_direct_matches_deduplicate_and_manual_conflicts_do_not_expand(isolated):
    sessions, folder = isolated
    async def exercise():
        async with sessions() as db:
            p, rows, user = await seed(db, folder)
            a, b, c = [row[1] for row in rows]
            rows[2][2].embedding = vector(10); await db.commit()
            await decide(db, p, a, b, user)
            await decide(db, p, c, b, user, 'rejected')
            await decide(db, p, a, c, user)
            page = await search.search_appearances(db, await context(db), 0)
            assert {r['id'] for r in page['appearances']} == {a.id,c.id}
            assert page['direct_count']==2 and page['operator_confirmed_count']==0
            # Rejection is a pair decision, not a veto of an independent image match.
            rows[1][2].embedding=vector(5); await db.commit()
            page = await search.search_appearances(db, await context(db), 0)
            assert page['total']==3 and page['direct_count']==3
    run(exercise())


def test_combined_pages_are_ordered_unique_and_invalidated_after_a_new_decision(isolated):
    sessions, folder = isolated
    async def exercise():
        async with sessions() as db:
            p, rows, user = await seed(db, folder)
            a, b, c = [row[1] for row in rows]
            await decide(db, p, a, b, user); await decide(db, p, a, c, user)
            ctx=await context(db, review_revision=await search.review_revision(db))
            seen=[];after=None
            while True:
                page=await search.search_appearances(db,ctx,0,after_id=after,limit=1)
                seen.extend(r['id'] for r in page['appearances'])
                assert page['total']==3 and page['operator_confirmed_count']==2
                if not page['has_more']:break
                after=page['next_after_id']
            assert seen == [a.id,b.id,c.id]
            await decide(db,p,a,b,user,'clear')
            with pytest.raises(HTTPException) as error:
                await search.search_appearances(db,ctx,0,after_id=a.id)
            assert error.value.status_code==409
            page=await search.search_appearances(db,await context(db),0)
            assert [r['id'] for r in page['appearances']]==[a.id,c.id]
    run(exercise())


def test_no_direct_query_anchor_means_no_manual_expansion(isolated):
    sessions, folder = isolated
    async def exercise():
        async with sessions() as db:
            p, rows, user = await seed(db, folder)
            await decide(db,p,rows[0][1],rows[1][1],user)
            page=await search.search_appearances(db,await context(db,vectors={'0':vector(180).tolist()}),0)
            assert page['total']==0
    run(exercise())


@pytest.mark.parametrize('problem', ['missing', 'old_model', 'ambiguous_vectors', 'ambiguous_faces'])
def test_unusable_reference_cannot_add_a_confirmed_sighting(isolated, problem):
    sessions, folder = isolated
    async def exercise():
        async with sessions() as db:
            p, rows, user = await seed(db, folder)
            face, a, embedding = rows[0]
            b = rows[1][1]
            await decide(db, p, a, b, user)
            if problem == 'missing': await db.delete(embedding)
            elif problem == 'old_model': embedding.embedding_model_version = 'previous-model'
            elif problem == 'ambiguous_vectors':
                db.add(m.IdentityEmbedding(identity_id=p.id, detection_id=a.detection_id,
                    pipeline_id=a.pipeline_id, embedding=vector(0), embedding_model_version=MODEL))
            else:
                db.add(m.Face(identity_id=p.id, detection_id=a.detection_id, name='Synthetic other', similarity=.9))
            await db.flush()
            page = await search.search_appearances(db, await context(db), 0)
            assert page['total'] == 0
    run(exercise())


def test_multi_face_queries_do_not_share_confirmation_seeds(isolated):
    sessions, folder = isolated
    async def exercise():
        async with sessions() as db:
            p, rows, user = await seed(db, folder)
            await decide(db, p, rows[0][1], rows[1][1], user)
            ctx = await context(db, vectors={'0': vector(0).tolist(), '1': vector(180).tolist()})
            assert (await search.search_appearances(db, ctx, 0))['operator_confirmed_count'] == 1
            assert (await search.search_appearances(db, ctx, 1))['total'] == 0
    run(exercise())

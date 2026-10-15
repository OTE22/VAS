"""All-appearance retrieval with isolated synthetic data, never production rows."""
from datetime import datetime
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select

from config import settings
import db_models as m
from conftest import run_on_shared_loop as run
from test_person_history_retention import isolated
from test_search_filters import vector, person, sighting, enrollment, MODEL
from backend.core import search_appearances as module
from backend.core.quick_image_search import search_dates


@pytest.fixture(autouse=True)
def configuration(monkeypatch):
    monkeypatch.setattr(settings, 'SIMILARITY_THRESHOLD', .8)
    monkeypatch.setattr(settings, 'RECOGNITION_MODEL', MODEL + '.onnx')


async def context(db, **updates):
    result = dict(vectors={'0': vector(0).tolist()}, model=MODEL, scope='both',
        threshold=.8, filters={}, exclude_ids=[],
        watermark=await db.scalar(select(func.max(m.IdentityAppearance.id))))
    result.update(updates)
    return result


def test_all_pages_across_groups_and_cameras_without_top_k_or_similarity_chaining(isolated):
    sessions, _ = isolated
    async def check():
        async with sessions() as db:
            people = [await person(db) for _ in range(12)]
            expected = set()
            for i in range(125):
                _, app, _ = await sighting(db, people[i % 12], i % 30,
                    camera=f'camera-{i % 3}', day=i % 10 + 1)
                expected.add(app.id)
            # This image agrees with a 30-degree match but not the original query.
            _, unrelated, _ = await sighting(db, people[0], 60)
            original = await context(db)
            # New events are reached by rerunning search, not inserted between pages.
            _, new_app, _ = await sighting(db, people[0], 0)
            seen, after = [], None
            while True:
                page = await module.search_appearances(db, original, 0, after_id=after, limit=17)
                assert page['total'] == 125 and page['cameras'] == 3
                seen.extend(row['id'] for row in page['appearances'])
                assert len(page['appearances']) <= 17
                after = page['next_after_id']
                if not page['has_more']: break
            assert len(seen) == len(set(seen)) == 125
            assert set(seen) == expected
            assert unrelated.id not in seen and new_app.id not in seen
            assert (await module.search_appearances(db, await context(db), 0))['total'] == 126
    run(check())


def test_known_unknown_names_filters_and_exact_query_are_preserved(isolated):
    sessions, _ = isolated
    async def check():
        async with sessions() as db:
            known, unknown = await person(db, m.IdentityType.KNOWN), await person(db)
            await enrollment(db, known, 90)
            _, app, _ = await sighting(db, known, 0, camera='north', day=2)
            await sighting(db, unknown, 0, camera='south', day=3)
            original = await context(db)
            page = await module.search_appearances(db, original, 0)
            assert page['total'] == 2
            hit = next(row for row in page['appearances'] if row['id'] == app.id)
            assert hit['type'] == 'unverified' and hit['display_name'] is None
            assert hit['identity_verification'] == 'unverified'
            assert not hit.get('watchlist_match')
            original['filters'] = {'pipeline_id': 'north', 'date_from': '2026-01-02T00:00:00', 'date_to': '2026-01-02T23:59:59.999999'}
            assert (await module.search_appearances(db, original, 0))['total'] == 1
            original['exclude_ids'] = [str(known.id)]
            assert (await module.search_appearances(db, original, 0))['total'] == 0
            original['exclude_ids'] = []; original['scope'] = 'unknown'
            assert (await module.search_appearances(db, original, 0))['total'] == 0
    run(check())


def test_known_reference_verification_uses_the_query_threshold_even_during_a_setting_change(isolated, monkeypatch):
    sessions, _ = isolated
    async def check():
        async with sessions() as db:
            p = await person(db, m.IdentityType.KNOWN)
            await enrollment(db, p, 50)
            await sighting(db, p, 0)
            original = await context(db)
            monkeypatch.setattr(settings, 'SIMILARITY_THRESHOLD', .6)
            row = (await module.search_appearances(db, original, 0))['appearances'][0]
            assert row['type'] == 'unverified' and row['display_name'] is None
            # An approved reference that agrees with both is allowed to label it.
            await enrollment(db, p, 0)
            row = (await module.search_appearances(db, original, 0))['appearances'][0]
            assert row['type'] == 'known' and row['display_name'] == p.display_name
    run(check())


@pytest.mark.parametrize('case', ['no_vector', 'two_vectors', 'two_faces', 'two_appearances', 'old_model', 'no_photo', 'deleted_face'])
def test_missing_ambiguous_and_retained_evidence(isolated, case):
    sessions, folder = isolated
    async def check():
        async with sessions() as db:
            p = await person(db)
            f, a, e = await sighting(db, p, 0)
            if case == 'no_vector': await db.delete(e)
            elif case == 'old_model': e.embedding_model_version = 'other-model'
            elif case == 'two_vectors':
                db.add(m.IdentityEmbedding(identity_id=p.id, detection_id=e.detection_id,
                    pipeline_id=e.pipeline_id, embedding=vector(90), embedding_model_version=MODEL))
            elif case == 'two_faces':
                db.add(m.Face(identity_id=p.id, detection_id=e.detection_id, name='Synthetic other', similarity=.9))
            elif case == 'two_appearances':
                db.add(m.IdentityAppearance(identity_id=p.id, detection_id=e.detection_id,
                    pipeline_id=e.pipeline_id, start_time=a.start_time))
            elif case == 'no_photo': f.face_image_path = None; a.best_snapshot_path = None
            elif case == 'deleted_face':
                path = folder / 'retained.jpg'; path.write_bytes(b'synthetic')
                a.best_snapshot_path = str(path); await db.delete(f)
            await db.flush()
            page = await module.search_appearances(db, await context(db), 0)
            assert page['total'] == (1 if case in ('no_photo', 'deleted_face') else 0)
            if case == 'no_photo':
                assert page['appearances'][0]['snapshot_url'] is None
            if case == 'deleted_face':
                assert page['appearances'][0]['snapshot_url'].endswith('/retained.jpg')
    run(check())


def test_date_to_includes_last_microsecond_and_timezone_range(isolated):
    start, end = search_dates('2026-01-02', '2026-01-02')
    assert start == datetime(2026, 1, 2) and end == datetime(2026, 1, 2, 23, 59, 59, 999999)
    assert search_dates(None, '2026-01-02T23:59:59.999999+03:00')[1] == datetime(2026, 1, 2, 20, 59, 59, 999999)
    sessions, _ = isolated
    async def check():
        async with sessions() as db:
            p = await person(db)
            _, last, _ = await sighting(db, p, 0, day=2)
            last.start_time = end
            await sighting(db, p, 0, day=3)
            await db.flush()
            page = await module.search_appearances(db, await context(db,
                filters={'date_from': start.isoformat(), 'date_to': end.isoformat()}), 0)
            assert [row['id'] for row in page['appearances']] == [last.id]
    run(check())


def test_context_is_owner_bound_expires_and_never_serializes_vectors_to_client(isolated, monkeypatch):
    sessions, _ = isolated
    cache = NS(set=AsyncMock(return_value=True), get=AsyncMock())
    monkeypatch.setattr(module, 'redis_cache_service', cache)
    async def check():
        async with sessions() as db:
            token = await module.create_context(db, [NS(face_index=2, skipped=False, embedding=vector(0))],
                user_id=7, scope='both', filters={})
            assert len(token) == 36
            args = cache.set.call_args
            assert args.kwargs['ttl'] == 1800
            cache.get.return_value = args.args[1]
            assert (await module.load_context(token, 7, 2))['vectors']['2'] == vector(0).tolist()
            for owner, face, status in [(8, 2, 410), (7, 0, 404)]:
                with pytest.raises(HTTPException) as error: await module.load_context(token, owner, face)
                assert error.value.status_code == status
            monkeypatch.setattr(settings, 'SIMILARITY_THRESHOLD', .6)
            with pytest.raises(HTTPException) as error: await module.load_context(token, 7, 2)
            assert error.value.status_code == 409
            cache.get.return_value = None
            with pytest.raises(HTTPException) as error: await module.load_context(token, 7, 2)
            assert error.value.status_code == 410
            cache.set.return_value = False
            assert await module.create_context(db, [NS(face_index=0, skipped=False, embedding=vector(0))],
                user_id=7, scope='both', filters={}) is None
    run(check())


def test_http_appearance_route_requires_admin_and_preserves_paging_contract(isolated, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from backend.routes import advanced_search as routes
    from db_connection import get_db
    from uuid import uuid4
    app = FastAPI()
    app.include_router(routes.router)
    route = next(r for r in routes.router.routes if r.path == '/api/search/appearances/{context_id}')
    admin_dependency = next(d.call for d in route.dependant.dependencies if d.name == 'current_user')
    async def user(): return {'id': 7, 'role': 'admin'}
    async def session(): yield object()
    app.dependency_overrides[admin_dependency] = user
    app.dependency_overrides[get_db] = session
    monkeypatch.setattr(module, 'load_context', AsyncMock(return_value={'owned': True}))
    monkeypatch.setattr(module, 'search_appearances', AsyncMock(return_value={
        'appearances': [{'id': 101, 'snapshot_url': None}], 'total': 102,
        'has_more': True, 'next_after_id': 101, 'threshold': .8}))
    with TestClient(app) as client:
        response = client.get(f'/api/search/appearances/{uuid4()}?face_index=2&after_id=100&limit=1')
        assert response.status_code == 200
        assert response.headers['cache-control'] == 'private, no-store'
        assert response.json()['next_after_id'] == 101
        assert module.search_appearances.call_args.kwargs == {'after_id': 100, 'limit': 1}
        assert module.load_context.call_args.args[1:] == (7, 2)
        assert client.get(f'/api/search/appearances/{uuid4()}?face_index=0&limit=101').status_code == 422
        async def denied(): raise HTTPException(403, 'Admin required')
        app.dependency_overrides[admin_dependency] = denied
        assert client.get(f'/api/search/appearances/{uuid4()}?face_index=0').status_code == 403


def test_advanced_http_normalizes_date_range_and_exposes_only_opaque_context(monkeypatch):
    import cv2
    import numpy as np
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from backend.routes import advanced_search as routes
    from db_connection import get_db
    from uuid import uuid4
    app = FastAPI(); app.include_router(routes.router)
    route = next(r for r in routes.router.routes if r.path == '/api/search/advanced')
    dependency = next(d.call for d in route.dependant.dependencies if d.name == 'current_user')
    async def user(): return {'id': 7, 'role': 'admin'}
    async def session(): yield object()
    app.dependency_overrides[dependency] = user; app.dependency_overrides[get_db] = session
    token = str(uuid4())
    fake_result = NS(faces=[NS(skipped=False)])
    payload = {'search_id': 'test', 'image_info': {}, 'faces': [], 'watchlist_alerts': [],
        'processing_time_ms': 1, 'summary': dict.fromkeys(['total_faces_detected',
        'faces_searchable', 'faces_skipped', 'total_matches', 'unique_identities_found',
        'known_matches', 'unknown_matches', 'watchlist_alerts'], 0)}
    service = NS(is_initialized=True, search_multi_face=AsyncMock(return_value=fake_result), to_dict=lambda result: dict(payload))
    monkeypatch.setattr(routes, 'get_advanced_search_service', lambda: service)
    monkeypatch.setattr(module, 'create_context', AsyncMock(return_value=token))
    png = cv2.imencode('.png', np.zeros((16, 16, 3), dtype=np.uint8))[1].tobytes()
    with TestClient(app) as client:
        response = client.post('/api/search/advanced', headers={'X-Requested-With': 'XMLHttpRequest'},
            data={'date_from': '2026-10-15T00:00:00+03:00', 'date_to': '2026-10-15T23:59:59.999999+03:00'},
            files={'image': ('synthetic.png', png, 'image/png')})
        assert response.status_code == 200, response.text
        assert response.json()['appearance_context'] == token
        filters = service.search_multi_face.call_args.kwargs['filters']
        assert filters['date_from'] == datetime(2026, 10, 14, 21)
        assert filters['date_to'] == datetime(2026, 10, 15, 20, 59, 59, 999999)
        assert module.create_context.call_args.kwargs['filters'] == filters
        response = client.post('/api/search/advanced', headers={'X-Requested-With': 'XMLHttpRequest'},
            data={'date_from': '2026-10-16', 'date_to': '2026-10-15'},
            files={'image': ('synthetic.png', png, 'image/png')})
        assert response.status_code == 422

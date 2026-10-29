"""Unknown listing regressions on an explicitly disposable PostgreSQL database."""
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import event, select, text
from sqlalchemy.dialects import postgresql

from conftest import run_on_shared_loop as run
from test_person_history_retention import isolated
from db_models import (Identity, IdentityAppearance, IdentityEmbedding, IdentityStatus,
                       IdentityType, Pipeline, Detection, Face)
from backend.core.identity_pipelines import effective_pipelines, pipeline_scope_predicate


async def seed(db):
    day = datetime(2026, 10, 25, 12)
    db.add_all([Pipeline(pipeline_id=camera) for camera in ('a', 'b', 'c', 'hidden')])
    people = {}
    for name, count in [('appearance', 10), ('embedding', 20), ('face', 30),
                        ('orphan', 40), ('old', 50), ('inactive', 60),
                        ('known', 70), ('null_embedding', 5), ('lower', 1), ('upper', 2)]:
        person = Identity(type=IdentityType.KNOWN if name == 'known' else IdentityType.UNKNOWN,
                          status=IdentityStatus.INACTIVE if name == 'inactive' else IdentityStatus.ACTIVE,
                          display_name=name, first_seen_at=day, last_seen_at=day,
                          appearances_count=count)
        db.add(person)
        people[name] = person
    await db.flush()
    for name, camera, when in [
        ('appearance', 'a', day), ('appearance', 'a', day),
        ('appearance', 'hidden', datetime(2026, 10, 1)),
        ('old', 'a', datetime(2026, 10, 1)), ('inactive', 'a', day), ('known', 'a', day),
        ('lower', 'a', datetime(2026, 10, 25)), ('upper', 'a', datetime(2026, 10, 26)),
    ]:
        db.add(IdentityAppearance(identity_id=people[name].id, pipeline_id=camera, start_time=when))
    for name, camera in [('appearance', 'b'), ('embedding', 'b'), ('embedding', 'b'),
                         ('embedding', 'hidden'), ('orphan', None), ('null_embedding', None)]:
        db.add(IdentityEmbedding(identity_id=people[name].id, pipeline_id=camera,
                                 faiss_index_type='unknown'))
    for name in ('embedding', 'face', 'face', 'null_embedding'):
        detection = Detection(pipeline_id='c', timestamp=day)
        db.add(detection)
        await db.flush()
        db.add(Face(detection_id=detection.id, identity_id=people[name].id,
                    name='Unknown', similarity=0))
    await db.commit()
    return people


def prepare_route(monkeypatch):
    from backend.core.redis_cache import redis_cache_service
    from backend.routes.identities import list_unknown_identities
    monkeypatch.setattr(redis_cache_service, 'get_unknown_cache_key', AsyncMock(return_value='isolated-unknown'))
    monkeypatch.setattr(redis_cache_service, 'get', AsyncMock(return_value=None))
    monkeypatch.setattr(redis_cache_service, 'set', AsyncMock(return_value=True))

    async def query(db, **overrides):
        values = dict(page=1, page_size=2, date_from='2026-10-25T00:00:00Z',
                      date_to='2026-10-26T00:00:00Z', pipeline_id=None, status_filter=None,
                      min_appearances=None, show_all=True, db=db,
                      current_user=SimpleNamespace(id=1, role='admin'))
        values.update(overrides)
        return await list_unknown_identities(**values)
    return query


def test_membership_matches_priority_relation_even_with_outer_appearance_join(isolated):
    sessions, _ = isolated

    async def exercise():
        async with sessions() as db:
            await seed(db)
            relation = effective_pipelines().subquery()
            for allowed in (None, [], ['a'], ['b'], ['c'], ['b', 'hidden']):
                for camera in (None, 'a', 'b', 'c', 'hidden', 'denied'):
                    expected = select(relation.c.identity_id)
                    if allowed is not None:
                        expected = expected.where(relation.c.pipeline_id.in_(allowed))
                    if camera:
                        expected = expected.where(relation.c.pipeline_id == camera)
                    predicate = pipeline_scope_predicate(camera, allowed)
                    actual = select(Identity.id).where(predicate)
                    assert set((await db.scalars(actual)).all()) == set((await db.scalars(expected)).all())
                    joined = select(Identity.id).join(IdentityAppearance).where(predicate).distinct()
                    joined_expected = select(Identity.id).join(IdentityAppearance).where(
                        Identity.id.in_(expected)).distinct()
                    assert set((await db.scalars(joined)).all()) == set((await db.scalars(joined_expected)).all())
                    assert 'UNION' not in str(actual.compile(dialect=postgresql.dialect()))
    run(exercise())


def test_route_counts_filters_scope_and_transaction_local_guard(isolated, monkeypatch):
    from backend.auth.auth_service import AuthService
    sessions, _ = isolated
    query = prepare_route(monkeypatch)

    async def exercise():
        async with sessions() as db:
            await seed(db)
            await db.execute(text('SET max_parallel_workers_per_gather = 2'))
            await db.commit()
            statements = []

            def capture(conn, cursor, statement, parameters, context, executemany):
                statements.append(statement)

            engine = db.get_bind()
            event.listen(engine, 'before_cursor_execute', capture)
            try:
                body = await query(db)
                assert body['total'] == 5 and len(body['identities']) == 2
                assert body['total_pages'] == 3
                assert body['stats'] == {'total_unknown': 5, 'total_appearances': 66, 'active_cameras': 4}
                assert body['pipeline_totals'] == {'a': 2, 'b': 1, 'c': 2, 'hidden': 1}
                ids = {row['id'] for row in body['identities']}
                next_page = await query(db, page=2)
                assert ids.isdisjoint(row['id'] for row in next_page['identities'])
                assert next_page['pipeline_totals'] == body['pipeline_totals']
                assert await db.scalar(text('SHOW max_parallel_workers_per_gather')) == '0'
                stats_queries = [sql for sql in statements
                                 if 'WITH matching_unknown_identities AS MATERIALIZED' in sql
                                 and 'count(distinct' in sql.lower()]
                assert stats_queries and all(sql.count('UNION') == 2 for sql in stats_queries)
                await db.rollback()
                assert await db.scalar(text('SHOW max_parallel_workers_per_gather')) == '2'

                filtered = await query(db, min_appearances=15)
                assert filtered['stats'] == {'total_unknown': 2, 'total_appearances': 50, 'active_cameras': 3}
                assert filtered['pipeline_totals'] == {'b': 1, 'c': 1, 'hidden': 1}

                monkeypatch.setattr(AuthService, 'get_user_pipelines', AsyncMock(return_value=['b']))
                user = SimpleNamespace(id=2, role='user')
                for camera in (None, 'b'):
                    scoped = await query(db, current_user=user, pipeline_id=camera)
                    assert scoped['total'] == 1
                    assert scoped['identities'][0]['display_name'] == 'embedding'
                    assert scoped['identities'][0]['pipeline_ids'] == ['b']
                    assert scoped['pipeline_totals'] == {'b': 1}
                    # Preserve existing identity-wide stats, while camera totals/cards obey scope.
                    assert scoped['stats']['active_cameras'] == 2
                denied = await query(db, current_user=user, pipeline_id='a')
                assert denied['total'] == 0 and denied['pipeline_totals'] == {}
                monkeypatch.setattr(AuthService, 'get_user_pipelines', AsyncMock(return_value=[]))
                await db.rollback()
                empty = await query(db, current_user=user)
                assert empty['total'] == 0
                assert await db.scalar(text('SHOW max_parallel_workers_per_gather')) == '2'
            finally:
                event.remove(engine, 'before_cursor_execute', capture)
    run(exercise())


def test_cached_response_skips_database_and_errors_hide_sql(monkeypatch):
    from fastapi import HTTPException
    from backend.core.redis_cache import redis_cache_service
    query = prepare_route(monkeypatch)

    async def exercise():
        monkeypatch.setattr(redis_cache_service, 'get', AsyncMock(return_value={'cached': True}))
        assert await query(None) == {'cached': True}
        monkeypatch.setattr(redis_cache_service, 'get', AsyncMock(side_effect=RuntimeError('SELECT sensitive SQL')))
        with pytest.raises(HTTPException) as failure:
            await query(None)
        assert failure.value.status_code == 500
        assert failure.value.detail == 'Failed to list unknown identities. Please try again.'
        monkeypatch.setattr(redis_cache_service, 'get', AsyncMock(side_effect=HTTPException(403, 'Denied')))
        with pytest.raises(HTTPException) as denied:
            await query(None)
        assert denied.value.status_code == 403
    run(exercise())

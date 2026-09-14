"""Exercise the real pgvector query in isolated, transaction-local tables."""
import asyncio
from uuid import UUID

import numpy as np
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from config import settings
from backend.core.identity_index_pgvector import IdentityIndexPgVector


def test_known_search_ranks_distinct_people_by_best_photo():
    asyncio.run(_exercise())


async def _exercise():
    engine = create_async_engine(settings.DATABASE_URL)
    try:
        async with engine.connect() as connection:
            transaction = await connection.begin()
            try:
                # PostgreSQL resolves these private temporary tables before
                # public tables. No real people are read or modified.
                await connection.execute(text('CREATE TEMP TABLE identities '
                    '(id uuid PRIMARY KEY, type text, status text, display_name text) ON COMMIT DROP'))
                await connection.execute(text('CREATE TEMP TABLE identity_embeddings '
                    '(id bigserial PRIMARY KEY, identity_id uuid, embedding vector(512), quality real) ON COMMIT DROP'))
                people = [str(UUID(int=i)) for i in range(1, 8)]
                for i, person in enumerate(people):
                    await connection.execute(text('INSERT INTO identities VALUES (:id, :type, :status, :name)'),
                        {'id': person, 'type': 'UNKNOWN' if i == 4 else 'KNOWN',
                         'status': ['ACTIVE', 'PROMOTED', 'ACTIVE', 'INACTIVE', 'ACTIVE', 'MERGED', 'ACTIVE'][i],
                         'name': f'Person {i}'})
                # Person 1 has more photos than a typical over-fetch pool;
                # the runner-up must still get a candidate slot.
                samples = [(people[0], .99)] * 30 + [(people[0], .70),
                    (people[1], .90), (people[2], .60), (people[3], 1.0),
                    (people[4], 1.0), (people[5], 1.0), (people[6], .90)]
                for person, similarity in samples:
                    vector = np.zeros(512)
                    vector[:2] = [similarity, np.sqrt(1 - similarity ** 2)]
                    await connection.execute(text('INSERT INTO identity_embeddings '
                        '(identity_id, embedding, quality) VALUES (:id, CAST(:v AS vector), .8)'),
                        {'id': person, 'v': '[' + ','.join(map(str, vector)) + ']'})
                query = np.zeros(512, dtype=np.float32)
                query[0] = 1
                index = IdentityIndexPgVector()
                async with AsyncSession(bind=connection, join_transaction_mode='create_savepoint') as db:
                    hits = await index.search_known(query, db, top_k=2, threshold=.5)
                    assert [p for p, _ in hits] == people[:2]
                    assert [s for _, s in hits] == pytest.approx([.99, .90], abs=1e-6)
                    hits = await index.search_known(query, db, top_k=10, threshold=.5)
                    assert [p for p, _ in hits] == [people[0], people[1], people[6], people[2]]
                    assert len({p for p, _ in hits}) == len(hits)
                    hits = await index.search_known(query, db, top_k=10, threshold=.95)
                    assert [p for p, _ in hits] == [people[0]]
                    assert await index.search_known(query, db, top_k=10, threshold=.999) == []
            finally:
                await transaction.rollback()
    finally:
        await engine.dispose()

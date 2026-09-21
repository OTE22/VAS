"""Exercise the route's camera-count query against an isolated SQLite database."""
import asyncio
from pathlib import Path
from types import SimpleNamespace
import textwrap
import unittest

from sqlalchemy import Column, Integer, String, MetaData, Table, create_engine, select, func


class PipelineCountsTest(unittest.TestCase):
    def test_counts_include_cameras_beyond_first_page_and_respect_scope(self):
        metadata = MetaData()
        identities = Table('identities', metadata, Column('id', Integer, primary_key=True), Column('type', String))
        appearances = Table('appearances', metadata, Column('id', Integer, primary_key=True),
                            Column('identity_id', Integer), Column('pipeline_id', String), Column('start_time', Integer))
        Identity = SimpleNamespace(**{c.name: c for c in identities.c})
        Appearance = SimpleNamespace(**{c.name: c for c in appearances.c})
        engine = create_engine('sqlite://')
        metadata.create_all(engine)
        source = Path('backend/routes/identities.py').read_text()
        start = source.index('        pipeline_counts = await db.execute(')
        end = source.index('\n        # ORDER BY', start)
        query_code = textwrap.dedent(source[start:end]).replace('.select_from(IdentityAppearance)', '.select_from(appearances)').replace('.join(Identity,', '.join(identities,')
        with engine.begin() as connection:
            connection.execute(identities.insert(), [{'id': i, 'type': 'unknown' if i < 24 else 'known'} for i in range(1, 25)])
            connection.execute(appearances.insert(), [
                {'identity_id': i, 'pipeline_id': 'busy' if i <= 21 else 'quiet', 'start_time': 100}
                for i in range(1, 25)
            ] + [{'identity_id': 22, 'pipeline_id': 'quiet', 'start_time': 100},
                 {'identity_id': 22, 'pipeline_id': 'old', 'start_time': 1}])

            class DB:
                async def execute(self, query):
                    return connection.execute(query)

            def counts(allowed=None, pipeline=None):
                event_conditions = [Appearance.identity_id == Identity.id, Appearance.start_time >= 50]
                if allowed is not None:
                    event_conditions.append(Appearance.pipeline_id.in_(allowed))
                if pipeline:
                    event_conditions.append(Appearance.pipeline_id == pipeline)
                conditions = [Identity.type == 'unknown', select(Appearance.id).where(*event_conditions).correlate(identities).exists()]
                scope = dict(select=select, func=func, Identity=Identity, IdentityAppearance=Appearance,
                             identities=identities, appearances=appearances, db=DB(),
                             conditions=conditions, event_conditions=event_conditions)
                exec('async def run():\n' + textwrap.indent(query_code, '    ') + '\n    return pipeline_totals', scope)
                return asyncio.run(scope['run']())

            self.assertEqual(counts(), {'busy': 21, 'quiet': 2})
            self.assertEqual(counts(['quiet']), {'quiet': 2})
            self.assertEqual(counts([]), {})
            self.assertEqual(counts(pipeline='quiet'), {'quiet': 2})
        engine.dispose()


if __name__ == '__main__':
    unittest.main()

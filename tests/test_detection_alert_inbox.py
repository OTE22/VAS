"""Integration tests on an explicitly disposable PostgreSQL database only.

Run with ALERT_INBOX_TEST_URL pointing to vas-alert-inbox-test-db/postgres.
"""
import importlib.util
import os
import unittest
import uuid
from datetime import datetime, timedelta
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from alembic.migration import MigrationContext
from alembic.operations import Operations
from backend.core import detection_alert_inbox as inbox


class InboxTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        url = os.environ.get('ALERT_INBOX_TEST_URL', '')
        if '@vas-alert-inbox-test-db/postgres' not in url:
            self.skipTest('Requires the disposable inbox test database')
        self.engine = create_async_engine(url)
        self.db = AsyncSession(self.engine)
        self.schema = 'test_' + uuid.uuid4().hex
        await self.db.execute(text(f'CREATE SCHEMA {self.schema}'))
        await self.db.execute(text(f'SET search_path TO {self.schema}'))
        ddl = [
            'CREATE TABLE identities(id uuid PRIMARY KEY, display_name text)',
            'CREATE TABLE pipelines(pipeline_id text PRIMARY KEY, location_name text)',
            'CREATE TABLE watchlists(id uuid PRIMARY KEY, name text, alert_level text, notify_dashboard boolean)',
            'CREATE TABLE watchlist_entries(id uuid PRIMARY KEY, watchlist_id uuid, identity_id uuid, action_instructions text)',
            'CREATE TABLE live_search_alerts(id uuid PRIMARY KEY, identity_id uuid, name text, notify_dashboard boolean, sound_alert boolean)',
        ]
        for table, rule in [('watchlist_alerts', 'watchlist_entry_id'), ('live_alert_triggers', 'alert_id')]:
            ddl.append(f'''CREATE TABLE {table}(id uuid PRIMARY KEY, {rule} uuid, pipeline_id text,
                detection_id integer, snapshot_path text, similarity_score float,
                created_at timestamp, acknowledged boolean DEFAULT false,
                acknowledged_by integer, acknowledged_at timestamp, triggered_by text DEFAULT 'detection')''')
        for statement in ddl:
            await self.db.execute(text(statement))
        self.identity, self.watchlist, self.entry, self.live = [uuid.uuid4() for _ in range(4)]
        await self.db.execute(text("INSERT INTO identities VALUES (:id,'Alice')"), {'id': self.identity})
        await self.db.execute(text("INSERT INTO pipelines VALUES ('cam1','Entrance'),('cam2','Exit')"))
        await self.db.execute(text("INSERT INTO watchlists VALUES (:id,'Visitors','info',true)"), {'id': self.watchlist})
        await self.db.execute(text("INSERT INTO watchlist_entries VALUES (:id,:wl,:person,'Notify reception')"), {'id': self.entry, 'wl': self.watchlist, 'person': self.identity})
        await self.db.execute(text("INSERT INTO live_search_alerts VALUES (:id,:person,'Entrance rule',true,true)"), {'id': self.live, 'person': self.identity})
        spec = importlib.util.spec_from_file_location('severity_migration', '/app/alembic/versions/ff06a7b8c9d0_live_alert_severity.py')
        migration = importlib.util.module_from_spec(spec); spec.loader.exec_module(migration)
        def upgrade(conn):
            with Operations.context(MigrationContext.configure(conn)):
                migration.upgrade()
        await (await self.db.connection()).run_sync(upgrade)
        await self.db.commit()
        self.base = datetime(2026, 9, 16, 10)

    async def asyncTearDown(self):
        if hasattr(self, 'db'):
            await self.db.rollback()
            await self.db.execute(text(f'DROP SCHEMA {self.schema} CASCADE'))
            await self.db.commit()
            await self.db.close()
            await self.engine.dispose()

    async def event(self, source='watchlist', seconds=0, pipeline='cam1'):
        table, rule, rule_id = ('watchlist_alerts', 'watchlist_entry_id', self.entry) if source == 'watchlist' else ('live_alert_triggers', 'alert_id', self.live)
        event_id = uuid.uuid4()
        await self.db.execute(text(f'''INSERT INTO {table}(id,{rule},pipeline_id,similarity_score,created_at)
            VALUES (:id,:rule,:pipeline,0.91,:created)'''), {'id': event_id, 'rule': rule_id, 'pipeline': pipeline, 'created': self.base + timedelta(seconds=seconds)})
        await self.db.commit()
        return str(event_id)

    async def test_migration_preserves_rules_and_sets_warning(self):
        self.assertEqual((await self.db.execute(text('SELECT alert_level FROM live_search_alerts'))).scalar_one(), 'warning')
        with self.assertRaises(Exception):
            await self.db.execute(text("UPDATE live_search_alerts SET alert_level='invalid'"))
        await self.db.rollback()
        self.assertEqual((await self.db.execute(text('SELECT count(*) FROM live_search_alerts'))).scalar_one(), 1)

    async def test_grouping_source_camera_gap_and_priority(self):
        first = await self.event(); await self.event(seconds=20)
        await self.event(seconds=25, pipeline='cam2'); await self.event(seconds=700)
        await self.event('live', seconds=5)
        data = await inbox.list_inbox(self.db)
        self.assertEqual(data['total'], 4)
        self.assertEqual(data['items'][0]['source'], 'live')
        group = next(i for i in data['items'] if i['first_id'] == first)
        self.assertEqual(group['sightings'], 2)
        self.assertEqual(group['location_name'], 'Entrance')
        self.assertEqual(group['action_instructions'], 'Notify reception')
        page = await inbox.list_inbox(self.db, limit=1, offset=1)
        self.assertEqual((len(page['items']), page['total']), (1, 4))
        empty = await inbox.list_inbox(self.db, offset=99)
        self.assertEqual((empty['items'], empty['total']), ([], 4))

    async def test_ack_boundary_preserves_new_sightings_history_and_actor(self):
        first = await self.event(); latest = await self.event(seconds=20)
        newer = await self.event(seconds=30)
        self.assertEqual(await inbox.acknowledge_episode(self.db, 'watchlist', first, latest, 7), 2)
        data = await inbox.list_inbox(self.db)
        self.assertEqual(data['items'][0]['first_id'], newer)
        self.assertEqual(await inbox.acknowledge_episode(self.db, 'watchlist', first, latest, 8), 0)
        rows = (await self.db.execute(text('SELECT acknowledged,acknowledged_by FROM watchlist_alerts'))).all()
        self.assertEqual(len(rows), 3)
        self.assertEqual(sorted(actor for ack, actor in rows if ack), [7, 7])

    async def test_ack_rejects_boundary_from_other_source_or_camera(self):
        first = await self.event(); live = await self.event('live')
        other_camera = await self.event(pipeline='cam2')
        self.assertEqual(await inbox.acknowledge_episode(self.db, 'watchlist', first, live, 7), 0)
        self.assertEqual(await inbox.acknowledge_episode(self.db, 'watchlist', first, other_camera, 7), 0)
        self.assertEqual(await inbox.acknowledge_episode(self.db, 'live', live, live, 7), 1)
        self.assertEqual((await inbox.list_inbox(self.db))['total'], 2)

    async def test_sound_candidates_ignore_pagination_and_honor_muting(self):
        await self.event(); live = await self.event('live', seconds=10)
        data = await inbox.list_inbox(self.db, limit=1, sounds_since=self.base + timedelta(seconds=1))
        self.assertEqual([s['first_id'] for s in data['sound_candidates']], [live])
        await self.db.execute(text('UPDATE live_search_alerts SET sound_alert=false'))
        await self.db.commit()
        data = await inbox.list_inbox(self.db, sounds_since=self.base + timedelta(seconds=1))
        self.assertEqual(data['sound_candidates'], [])
        self.assertEqual(data['total'], 2)

    async def test_disabled_dashboard_and_search_alerts_excluded(self):
        await self.event(); await self.event('live')
        await self.db.execute(text("UPDATE watchlist_alerts SET triggered_by='search'"))
        await self.db.execute(text('UPDATE live_search_alerts SET notify_dashboard=false'))
        await self.db.commit()
        self.assertEqual((await inbox.list_inbox(self.db))['total'], 0)

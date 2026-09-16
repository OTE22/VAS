"""Regression checks for read-only map boot and offline Redis shutdown.

Run without external services: python -m unittest discover -s tests -p test_map_and_redis_cleanup.py
"""
import asyncio
import errno
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from backend.core import map_content_ledger as ledger
from backend.core.websocket_manager import WebSocketManager


class MapLedgerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = str(Path(self.temp.name) / 'content_verdicts.json')
        self.settings = patch.object(ledger, 'settings', SimpleNamespace(
            MAP_CONTENT_LEDGER_PATH=self.path, MAP_PRODUCTION_DIR=self.temp.name))
        self.settings.start()
        self.addCleanup(self.settings.stop)
        self.entry = {'source_id': 'map', 'pass': True, 'code': None,
                      'message': None, 'kind': 'vector', 'archive_sha256': 'abc'}
        ledger.save({'map': self.entry}, {})

    def test_verified_boot_does_not_write_read_only_ledger(self):
        with patch.object(ledger, 'installed_archives', return_value={'map': '/map.mbtiles'}), \
             patch.object(ledger, 'verdict_for', return_value=(True, None, None)), \
             patch.object(ledger, 'save', side_effect=OSError(errno.EROFS, 'read-only')) as save:
            result = ledger.verify_installed(only_unverified=True)
        self.assertEqual(result['map']['skipped'], 'already verified')
        save.assert_not_called()

    def test_new_verdict_is_persisted_and_pending_entries_preserved(self):
        ledger.save({}, {'staged': {'state': 'pending'}})
        with patch.object(ledger, 'installed_archives', return_value={'map': '/map.mbtiles'}), \
             patch.object(ledger, 'build_entry', return_value=self.entry):
            ledger.verify_installed(only_unverified=True)
        self.assertEqual(ledger.load(), {'map': self.entry})
        self.assertEqual(ledger.load_pending(), {'staged': {'state': 'pending'}})

    def test_removed_archives_are_persistently_pruned(self):
        with patch.object(ledger, 'installed_archives', return_value={}):
            ledger.verify_installed(only_unverified=True)
        self.assertEqual(ledger.load(), {})

    def test_required_write_failure_is_not_hidden(self):
        with patch.object(ledger, 'installed_archives', return_value={'map': '/map.mbtiles'}), \
             patch.object(ledger, 'build_entry', return_value=self.entry), \
             patch.object(ledger, 'save', side_effect=OSError(errno.EROFS, 'read-only')):
            with self.assertRaises(OSError):
                ledger.verify_installed()


class RedisShutdownTests(unittest.IsolatedAsyncioTestCase):
    async def test_listener_cancelled_and_resources_closed_without_unsubscribe(self):
        manager = WebSocketManager()
        manager._redis_enabled = manager._redis_initialized = True
        pubsub = manager.redis_pubsub = SimpleNamespace(aclose=AsyncMock(), unsubscribe=AsyncMock(side_effect=AssertionError('must not contact Redis')))
        client = manager.redis_client = SimpleNamespace(aclose=AsyncMock())
        task = manager._redis_listener_task = asyncio.create_task(asyncio.sleep(3600))
        await asyncio.sleep(0)
        await manager.close_redis()
        self.assertTrue(task.cancelled())
        pubsub.aclose.assert_awaited_once()
        client.aclose.assert_awaited_once()
        pubsub.unsubscribe.assert_not_awaited()
        self.assertFalse(manager._redis_enabled)
        self.assertFalse(manager._redis_initialized)
        self.assertIsNone(manager.redis_pubsub)
        self.assertIsNone(manager.redis_client)
        await manager.close_redis()  # idempotent
        client.aclose.assert_awaited_once()

    async def test_client_is_closed_even_if_pubsub_cleanup_fails(self):
        manager = WebSocketManager()
        manager.redis_pubsub = SimpleNamespace(aclose=AsyncMock(side_effect=RuntimeError('cleanup failed')))
        client = manager.redis_client = SimpleNamespace(aclose=AsyncMock())
        with self.assertRaisesRegex(RuntimeError, 'cleanup failed'):
            await manager.close_redis()
        client.aclose.assert_awaited_once()

    async def test_failed_listener_does_not_leak_connections(self):
        manager = WebSocketManager()
        async def failed():
            raise RuntimeError('listener failed')
        manager._redis_listener_task = asyncio.create_task(failed())
        await asyncio.sleep(0)
        pubsub = manager.redis_pubsub = SimpleNamespace(aclose=AsyncMock())
        client = manager.redis_client = SimpleNamespace(aclose=AsyncMock())
        with self.assertRaisesRegex(RuntimeError, 'listener failed'):
            await manager.close_redis()
        pubsub.aclose.assert_awaited_once()
        client.aclose.assert_awaited_once()

    async def test_real_redis_client_can_close_without_server(self):
        from redis.asyncio import Redis
        manager = WebSocketManager()
        manager.redis_client = Redis(host='127.0.0.1', port=1, socket_connect_timeout=0.1)
        manager.redis_pubsub = manager.redis_client.pubsub()
        await asyncio.wait_for(manager.close_redis(), timeout=1)


if __name__ == '__main__':
    unittest.main()

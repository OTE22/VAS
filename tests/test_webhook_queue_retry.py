"""Exercise the real handler with an isolated queue; no model, DB or live HTTP.

AST loading avoids importing the receiver's model/bootstrap dependencies.
Run directly with Python's standard library unittest runner.
"""
import ast
import asyncio
from datetime import datetime
import hashlib
import importlib.util
import logging
from pathlib import Path
from types import SimpleNamespace
import time
import unittest
from unittest.mock import Mock, patch
import uuid


class Response:
    def __init__(self, status_code, content, headers=None):
        self.status_code, self.content, self.headers = status_code, content, headers


class HTTPException(Exception):
    def __init__(self, status_code, detail):
        self.status_code, self.detail = status_code, detail


class QueueRetryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        path = Path(__file__).resolve().parents[1] / 'backend/routes/webhook.py'
        tree = ast.parse(path.read_text())
        names = {'_dedup_is_duplicate', '_job_key', 'webhook_handler'}
        tree.body = [node for node in tree.body
                     if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in names]
        self.outcomes = [True]
        self.calls = 0

        async def add(item):
            self.calls += 1
            result = self.outcomes.pop(0)
            if isinstance(result, Exception):
                raise result
            return result

        async def alias(value):
            return value

        self.ns = dict(time=time, uuid=uuid, datetime=datetime, hashlib=hashlib,
                       settings=SimpleNamespace(WEBHOOK_DEDUP_TTL_SECONDS=600),
                       _dedup_seen={}, _dedup_pending=set(), _DEDUP_MAX_KEYS=5000,
                       _logged_payload_structures=set(), BackgroundTasks=object,
                       JSONResponse=Response, HTTPException=HTTPException,
                       logger=logging.getLogger('queue-retry-test'),
                       validate_pipeline_id=lambda v: v, _resolve_alias=alias,
                       _extract_location_name=lambda p: None,
                       _extract_pipeline_name=lambda p: None,
                       _payload_structure=lambda p: (list(p), [], [], None),
                       extract_images_from_payload=lambda p: p.get('images', []),
                       _metric_pipeline_label=lambda p: p, metrics_requests_total=Mock(),
                       processing_queue=SimpleNamespace(add=add))
        exec(compile(tree, str(path), 'exec'), self.ns)
        self.payload = {'event_id': 'event-1', 'images': ['fake-image'], 'predictions': []}
        spec = importlib.util.spec_from_file_location('isolated_feedback', path.parents[1] / 'core/processing_feedback.py')
        self.feedback = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.feedback)
        event_time = SimpleNamespace(observation_time=lambda *a: (datetime.now(), 'test'))
        modules = patch.dict('sys.modules', {'backend.core.event_time': event_time, 'backend.core': SimpleNamespace(processing_feedback=self.feedback)})
        modules.start()
        self.addCleanup(modules.stop)

    async def send(self, payload=None):
        return await self.ns['webhook_handler']('camera-1', self.payload if payload is None else payload, None)

    async def test_feedback_pending_then_committed_without_requeue(self):
        self.payload['processing_feedback'] = True
        self.assertEqual((await self.send()).content['processing_status'], 'pending')
        self.assertEqual((await self.send()).content['processing_status'], 'pending')
        self.feedback.complete('camera-1:event_id:event-1', 'saved')
        self.assertEqual((await self.send()).content['processing_status'], 'saved')
        self.assertEqual(self.calls, 1)

    async def test_feedback_queue_rejection_is_retryable(self):
        self.payload['processing_feedback'] = True
        self.outcomes = [False, True]
        self.assertEqual((await self.send()).status_code, 503)
        self.assertIsNone(self.feedback.lookup('camera-1:event_id:event-1'))
        self.assertEqual((await self.send()).content['processing_status'], 'pending')
        self.assertEqual(self.calls, 2)

    async def test_feedback_cache_has_a_hard_capacity(self):
        self.payload['processing_feedback'] = True
        self.feedback.MAX_EVENTS = 0
        self.assertEqual((await self.send()).status_code, 429)
        self.assertFalse(self.ns['_dedup_seen'])
        self.assertEqual(self.calls, 0)

    async def test_queue_full_then_same_event_is_accepted_then_deduplicated(self):
        self.outcomes = [False, True]
        self.assertEqual((await self.send()).status_code, 503)
        self.assertFalse(self.ns['_dedup_seen'])
        self.assertEqual((await self.send()).status_code, 202)
        self.assertEqual((await self.send()).content['status'], 'duplicate')
        self.assertEqual(self.calls, 2)

    async def test_exception_before_acceptance_does_not_poison_retry(self):
        self.outcomes = [RuntimeError('queue unavailable'), True]
        with self.assertRaises(HTTPException):
            await self.send()
        self.assertFalse(self.ns['_dedup_seen'])
        self.assertFalse(self.ns['_dedup_pending'])
        self.assertEqual((await self.send()).status_code, 202)

    async def test_concurrent_retry_waits_for_acceptance(self):
        started, release = asyncio.Event(), asyncio.Event()

        async def blocked_add(item):
            started.set()
            await release.wait()
            return False

        original_add = self.ns['processing_queue'].add
        self.ns['processing_queue'].add = blocked_add
        first = asyncio.create_task(self.send())
        await started.wait()
        try:
            second = await self.send()
            self.assertEqual(second.status_code, 429)
            self.assertEqual(second.content['status'], 'pending')
        finally:
            release.set()
            await first
        self.ns['processing_queue'].add = original_add
        self.assertEqual((await self.send()).status_code, 202)

    async def test_cancelled_enqueue_releases_reservation(self):
        started = asyncio.Event()

        async def blocked_add(item):
            started.set()
            await asyncio.Event().wait()

        self.ns['processing_queue'].add = blocked_add
        task = asyncio.create_task(self.send())
        await started.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertFalse(self.ns['_dedup_seen'])
        self.assertFalse(self.ns['_dedup_pending'])

    async def test_image_free_event_still_deduplicates(self):
        payload = dict(self.payload, images=[])
        self.assertEqual((await self.send(payload))['message'], 'No images')
        self.assertEqual((await self.send(payload)).content['status'], 'duplicate')
        self.assertEqual(self.calls, 0)

    async def test_partial_acceptance_keeps_existing_dedup_contract(self):
        self.outcomes = [True, False]
        payload = dict(self.payload, images=['image-1', 'image-2'])
        result = await self.send(payload)
        self.assertEqual(result.content['queued'], 1)
        self.assertEqual((await self.send(payload)).content['status'], 'duplicate')
        self.assertEqual(self.calls, 2)


if __name__ == '__main__':
    unittest.main()

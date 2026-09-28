"""Isolated feedback endpoint contract; no production config, models or DB."""
import ast
import importlib.util
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from test_webhook_queue_retry import Response

ROOT = Path(__file__).resolve().parents[1]


class StatusTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location('feedback_test', ROOT / 'backend/core/processing_feedback.py')
        self.feedback = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.feedback)
        tree = ast.parse((ROOT / 'backend/routes/webhook.py').read_text())
        tree.body = [n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == 'webhook_status']
        self.assertEqual(len(tree.body[0].decorator_list), 2)
        for decorator in tree.body[0].decorator_list:
            self.assertIn('Depends(require_webhook_key)', ast.unparse(decorator))
        tree.body[0].decorator_list = []
        ns = {'JSONResponse': Response}
        exec(compile(tree, 'status_route', 'exec'), ns)
        self.endpoint = ns['webhook_status']
        modules = patch.dict('sys.modules', {'backend.core': SimpleNamespace(processing_feedback=self.feedback)})
        modules.start()
        self.addCleanup(modules.stop)

    async def test_pending_complete_and_unknown(self):
        key = 'camera:event_id:one'
        self.assertTrue(self.feedback.reserve(key))
        token = self.feedback.token_for(key)
        self.assertNotIn('camera', token)
        self.assertEqual((await self.endpoint(token)).status_code, 202)
        self.feedback.complete(key, 'saved')
        result = await self.endpoint(token)
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.content, {'processing_status': 'saved'})
        self.feedback.discard(key)
        self.assertEqual((await self.endpoint(token)).status_code, 410)

    async def test_expiry_and_invalid_handle(self):
        self.feedback.reserve('event')
        token = self.feedback.token_for('event')
        with patch.object(self.feedback.time, 'monotonic', return_value=10**15):
            self.assertEqual((await self.endpoint(token)).status_code, 410)
        self.assertEqual((await self.endpoint('../invalid')).status_code, 410)

    def test_nginx_budgets_and_backpressure(self):
        text = (ROOT / 'nginx.prod.conf').read_text()
        location = text.split('location ~ ^/(api/)?webhook/ {', 1)[1].split('}', 1)[0]
        self.assertIn('limit_req zone=webhook_ingest', location)
        self.assertIn('limit_req zone=webhook_status', location)
        self.assertIn('limit_req_status 429;', location)
        self.assertNotIn('zone=api_rate', location)
        self.assertIn('zone=api_rate:10m rate=20r/s;', text)
        self.assertIn('zone=auth_rate:10m rate=10r/m;', text)

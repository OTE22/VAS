"""Exercise worker feedback without importing models, DB clients or live config."""
import ast
import asyncio
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch


class WorkerFeedbackTests(unittest.IsolatedAsyncioTestCase):
    async def run_worker(self, final='saved', error=False, invalid=False):
        path = Path(__file__).resolve().parents[1] / 'backend/services/queue_worker.py'
        tree = ast.parse(path.read_text())
        tree.body = [n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == '_process_queued_item']
        complete = Mock()

        async def process(*args, **kwargs):
            kwargs['feedback']['status'] = final
            if error:
                raise RuntimeError('test processing failure')
            return {'pipeline_id': 'offline'}

        process = AsyncMock(side_effect=process)
        ns = dict(asyncio=asyncio, datetime=datetime, INFERENCE_POOL=None,
                  _decode_validate_sync=lambda item: None if invalid else b'image',
                  process_image_async=process, logger=Mock())
        with patch.dict('sys.modules', {'backend.core': SimpleNamespace(
                processing_feedback=SimpleNamespace(complete=complete))}):
            exec(compile(tree, str(path), 'exec'), ns)
            try:
                await ns['_process_queued_item']({'pipeline_id': 'offline', 'feedback_key': 'event'}, 1)
            except RuntimeError:
                if not error:
                    raise
        return complete, process

    async def test_feedback_forces_direct_write(self):
        complete, process = await self.run_worker()
        self.assertFalse(process.await_args.kwargs['use_batch_write'])
        complete.assert_called_once_with('event', 'saved')

    async def test_unexpected_precommit_error_is_failed(self):
        complete, _ = await self.run_worker(final='no_face', error=True)
        complete.assert_called_once_with('event', 'failed')

    async def test_postcommit_notification_error_preserves_saved_status(self):
        complete, _ = await self.run_worker(error=True)
        complete.assert_called_once_with('event', 'saved')

    async def test_invalid_image_never_enters_processing(self):
        complete, process = await self.run_worker(invalid=True)
        complete.assert_called_once_with('event', 'invalid_image')
        process.assert_not_awaited()


if __name__ == '__main__':
    unittest.main()

"""Run the production direct-write block against a controlled transaction."""
import ast
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch


class CommitFeedbackTests(unittest.IsolatedAsyncioTestCase):
    async def run_commit(self, fail_commit=False, rejected=False):
        source = Path(__file__).resolve().parents[1] / 'backend/services/image_processing.py'
        tree = ast.parse(source.read_text())
        function = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == 'process_image_async')
        block = next(n for n in function.body if isinstance(n, ast.If) and ast.unparse(n.test) == 'not use_batch_write')
        wrapper = ast.parse('async def scenario():\n    pass').body[0]
        wrapper.body = [block]
        module = ast.fix_missing_locations(ast.Module(body=[wrapper], type_ignores=[]))
        feedback = {}
        persisted = SimpleNamespace(unknown_events=[], bundles=[])
        alerts, compensate = AsyncMock(), AsyncMock()

        class Session:
            async def __aenter__(self):
                return self
            async def __aexit__(self, *args):
                if fail_commit:
                    raise RuntimeError('simulated commit failure')

        ns = dict(use_batch_write=False, db_manager=SimpleNamespace(get_session=Session),
                  ensure_pipeline_registered=AsyncMock(), pipeline_id='offline-test',
                  detection_data={'faces': [{}]}, logger=Mock(), feedback=feedback,
                  report=lambda status: feedback.update(status=status),
                  detected_faces=[] if rejected else [{}], FACE_TRACKING_ENABLED=False,
                  display_name=None)
        modules = {
            'backend.core.detection_evidence': SimpleNamespace(
                persist_detection=AsyncMock(return_value=persisted),
                broadcast_detection_alerts=alerts, compensate_failed_detection=compensate),
            'backend.core.metrics': SimpleNamespace(metrics_db_operation_failures=None),
            'backend.core.appearance_events': SimpleNamespace(publish_unknown_events=AsyncMock()),
        }
        with patch.dict('sys.modules', modules):
            exec(compile(module, str(source), 'exec'), ns)
            await ns['scenario']()
        return feedback, alerts, compensate

    async def test_saved_only_after_commit(self):
        feedback, alerts, compensate = await self.run_commit()
        self.assertEqual(feedback['status'], 'saved')
        alerts.assert_awaited_once()
        compensate.assert_not_awaited()

    async def test_commit_failure_never_reports_saved_or_sends_alerts(self):
        feedback, alerts, compensate = await self.run_commit(fail_commit=True)
        self.assertEqual(feedback['status'], 'failed')
        alerts.assert_not_awaited()
        compensate.assert_awaited_once()

    async def test_rejected_evidence_is_not_a_usable_saved_face(self):
        feedback, _, _ = await self.run_commit(rejected=True)
        self.assertEqual(feedback['status'], 'quality_rejected')


if __name__ == '__main__':
    unittest.main()

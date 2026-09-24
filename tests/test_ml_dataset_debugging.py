"""Dataset diagnostics and notebook tests; no production DB, kernels or files."""
import asyncio
import contextlib
import hashlib
import io
import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from backend.ml.dataset_diagnostics import DatasetDiagnostics, trace_dataset_build
from backend.ml.debug_notebook import build_debug_notebook, helper_hashes
from backend.ml.dataset_steps import dataset_fingerprint, split_rows
from backend.ml.data_validator import validate_rows


class DiagnosticTests(unittest.IsolatedAsyncioTestCase):
    async def test_success_records_counts_and_preserves_prior_snapshots(self):
        sink = AsyncMock()
        diagnostics = DatasetDiagnostics('fixture', sink)
        await diagnostics.stage('configuration')
        before = diagnostics.snapshot()
        await diagnostics.stage('extracting_rows', candidate_rows=12)
        await diagnostics.finish({'status': 'built', 'dataset_id': 'fixture'})
        self.assertEqual(before['stage_history'][0]['status'], 'running')
        self.assertEqual(diagnostics.snapshot()['stage_history'][0]['candidate_rows'], 12)
        self.assertTrue(all(e['status'] == 'completed' for e in diagnostics.events))
        self.assertEqual(sink.await_count, 3)
        json.dumps(diagnostics.snapshot(), allow_nan=False)

    async def test_failure_preserves_exception_and_does_not_export_secrets(self):
        diag = DatasetDiagnostics('fixture', AsyncMock())
        @trace_dataset_build
        async def operation(*, diagnostics):
            await diagnostics.stage('writing_artifact')
            raise ValueError('postgresql://username:secret@private-host/db')
        with self.assertRaisesRegex(ValueError, 'secret'):
            await operation(diagnostics=diag)
        self.assertEqual(diag.events[-1]['status'], 'failed')
        self.assertEqual(diag.failure['code'], 'ValueError')
        self.assertNotIn('secret', json.dumps(diag.snapshot()))
        self.assertNotIn(str(Path(__file__).parent), json.dumps(diag.snapshot()))

    async def test_quality_refusal_is_failed_not_completed(self):
        diag = DatasetDiagnostics()
        @trace_dataset_build
        async def operation(*, diagnostics):
            await diagnostics.stage('validation')
            return {'status': 'failed', 'quality_report': {'failed_checks': ['minimum_rows']}}
        result = await operation(diagnostics=diag)
        self.assertEqual(result['diagnostics']['failure']['failed_checks'], ['minimum_rows'])
        self.assertEqual(diag.events[-1]['status'], 'failed')

    async def test_failed_telemetry_does_not_fail_dataset(self):
        diag = DatasetDiagnostics('fixture', AsyncMock(side_effect=RuntimeError('offline')))
        @trace_dataset_build
        async def operation(*, diagnostics):
            return {'status': 'built', 'checksum': 'same-output'}
        result = await operation(diagnostics=diag)
        self.assertEqual(result['checksum'], 'same-output')


class NotebookTests(unittest.TestCase):
    def test_failed_build_notebook_executes_without_artifact_or_database(self):
        notebook = build_debug_notebook({'job': {'status': 'failed'}, 'diagnostics': {'failure': {'code': 'ValueError'}}, 'artifact_available': False})
        scope = {}
        with contextlib.redirect_stdout(io.StringIO()):
            for cell in notebook['cells']:
                if cell['cell_type'] == 'code': exec(''.join(cell['source']), scope)
        self.assertEqual(notebook['nbformat'], 4)
        self.assertIn('Replay unavailable', json.dumps(notebook))
        self.assertNotIn('pyarrow', ''.join(''.join(c['source']) for c in notebook['cells'] if c['cell_type'] == 'code'))

    def fixture(self, root, strategy='temporal_group'):
        import pyarrow as pa
        import pyarrow.parquet as pq
        rows = [{'entity_id': str(i // 2), 'as_of': datetime(2025, 1, 1) + timedelta(days=i),
                 'features': {'count': float(i)}, 'snapshot_id': i + 1, 'label': None} for i in range(30)]
        defs = [{'name': 'count', 'leakage_class': 'safe', 'version': 1}]
        report = validate_rows(rows, kind='unsupervised', definitions=defs, now=datetime(2026, 1, 1))
        report['debug_contract'] = {'helper_sha256': helper_hashes(), 'definitions': defs,
                                   'split': {'strategy': strategy, 'val_fraction': .2, 'holdout_fraction': .2}}
        train, val, test, split = split_rows(rows, strategy)
        for name, part in [('train', train), ('val', val), ('test', test)]:
            for row in part: row['split'] = name
        path = root / 'datasets' / 'fixture.parquet'; path.parent.mkdir(exist_ok=True)
        pq.write_table(pa.Table.from_pylist([dict(entity_id=r['entity_id'], as_of=r['as_of'].isoformat(),
            features_json=json.dumps(r['features']), label=r['label'], snapshot_id=r['snapshot_id'], split=r.get('split')) for r in rows]), path)
        evidence = {'artifact_available': True, 'artifact_relative': 'datasets/fixture.parquet', 'dataset': {
            'kind': 'unsupervised', 'quality_report': report, 'split_config': split, 'checksum': dataset_fingerprint(rows),
            'parquet_sha256': hashlib.sha256(path.read_bytes()).hexdigest()}}
        return evidence, path

    def execute(self, notebook, root):
        scope = {}
        with contextlib.redirect_stdout(io.StringIO()):
            for i, cell in enumerate(notebook['cells']):
                if cell['cell_type'] == 'code':
                    exec(compile(''.join(cell['source']), f'notebook-cell-{i}', 'exec'), scope)
                    if 'ARTIFACT_ROOT' in scope: scope['ARTIFACT_ROOT'] = root
        return scope

    def test_full_notebook_rechecks_real_parquet_for_both_split_strategies(self):
        with tempfile.TemporaryDirectory() as folder:
            for strategy in ['temporal', 'temporal_group']:
                root = Path(folder); evidence, path = self.fixture(root, strategy)
                before = path.read_bytes()
                scope = self.execute(build_debug_notebook(evidence), root)
                self.assertTrue(scope['rechecked']['passed'])
                self.assertEqual(path.read_bytes(), before)
                self.assertEqual(scope['split_report']['method'], strategy)

    def test_snapshot_tampering_stops_before_loading(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); evidence, path = self.fixture(root)
            path.write_bytes(path.read_bytes() + b'changed')
            with self.assertRaisesRegex(AssertionError, 'checksum mismatch'):
                self.execute(build_debug_notebook(evidence), root)

    def test_code_drift_and_missing_legacy_definitions_are_explicit(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); evidence, _ = self.fixture(root)
            contract = evidence['dataset']['quality_report']['debug_contract']
            contract['helper_sha256'] = {'wrong': 'version'}
            with self.assertRaisesRegex(AssertionError, 'Helper code changed'):
                self.execute(build_debug_notebook(evidence), root)
            contract.pop('helper_sha256'); contract.pop('definitions')
            with self.assertRaisesRegex(AssertionError, 'definitions were not recorded'):
                self.execute(build_debug_notebook(evidence), root)

    def test_metadata_is_data_not_executable_source(self):
        evidence = {'job': {'job_id': "'); raise RuntimeError('injection'); #"}, 'artifact_available': False}
        scope = self.execute(build_debug_notebook(evidence), Path('/tmp'))
        self.assertEqual(scope['evidence'], evidence)


class NotebookRouteTests(unittest.TestCase):
    def test_exports_require_permission_and_never_include_queue_payload(self):
        from fastapi import FastAPI, HTTPException
        from fastapi.testclient import TestClient
        from backend.routes.ml_ops import router, ML_MANAGE, get_db
        from backend.core.task_history import task_history_manager
        task = {'job_id': 'fixture', 'task_type': 'ml_dataset_build', 'status': 'failed',
                'payload': {'database_url': 'secret'}, 'error_message': 'sensitive exception',
                'details': {'diagnostics_version': 1, 'stage_history': [], 'failure': {'code': 'ValueError'}}}
        app = FastAPI(); app.include_router(router)
        app.dependency_overrides[get_db] = lambda: SimpleNamespace()
        app.dependency_overrides[ML_MANAGE] = lambda: SimpleNamespace(username='fixture')
        with patch.object(task_history_manager, 'get_task_by_job_id', AsyncMock(return_value=task)), TestClient(app) as client:
            r = client.get('/api/ml/jobs/fixture/debug-notebook')
            self.assertEqual(r.status_code, 200)
            self.assertIn('attachment;', r.headers['content-disposition'])
            self.assertEqual(r.headers['cache-control'], 'no-store')
            self.assertNotIn('secret', r.text)
            self.assertNotIn('sensitive exception', r.text)
            task['task_type'] = 'unrelated_job'
            self.assertEqual(client.get('/api/ml/jobs/fixture/debug-notebook').status_code, 404)
            async def forbidden(): raise HTTPException(403, 'Denied')
            app.dependency_overrides[ML_MANAGE] = forbidden
            self.assertEqual(client.get('/api/ml/jobs/fixture/debug-notebook').status_code, 403)
            self.assertEqual(client.get('/api/ml/datasets/00000000-0000-0000-0000-000000000000/debug-notebook').status_code, 403)

    def test_workspace_url_rejects_credentials_tokens_and_invalid_hosts(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from backend.routes.ml_ops import router, ML_MANAGE
        from config import settings
        app = FastAPI(); app.include_router(router)
        app.dependency_overrides[ML_MANAGE] = lambda: SimpleNamespace(username='fixture')
        with TestClient(app) as client:
            for url in ['https://user:secret@example.com', 'https://example.com/?token=secret', 'javascript:bad', 'http://remote.example', 'https://[invalid']:
                with patch.object(settings, 'ML_NOTEBOOK_URL', url):
                    self.assertEqual(client.get('/api/ml/debug-workspace').json(), {'url': None})
            with patch.object(settings, 'ML_NOTEBOOK_URL', 'https://notebooks.example/lab'):
                self.assertEqual(client.get('/api/ml/debug-workspace').json()['url'], 'https://notebooks.example/lab')



class DatasetBuildTests(unittest.IsolatedAsyncioTestCase):
    async def run_build(self, root, *, count=30, fail_write=False, kind="unsupervised"):
        from backend.ml import dataset_builder
        from backend.ml.feature_store import feature_store
        from config import settings
        snapshots = [SimpleNamespace(id=i+1, entity_id=str(i),
            as_of_timestamp=datetime(2025, 1, 1) + timedelta(days=i),
            features={'count': float(i)}, unavailable_features={}) for i in range(count)]
        responses = [
            SimpleNamespace(scalar=lambda: count),
            SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: snapshots)),
            SimpleNamespace(scalar=lambda: None),
        ]
        if kind == 'supervised':
            labels = [SimpleNamespace(subject_id=str(i), event_time=snapshots[i].as_of_timestamp,
                label='positive' if i % 2 else 'negative', id=str(i)) for i in range(count)]
            responses.insert(2, SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: labels)))
        db = SimpleNamespace(execute=AsyncMock(side_effect=responses), add=lambda row: None, commit=AsyncMock())
        diag = DatasetDiagnostics('fixture', AsyncMock())
        with patch.object(feature_store, 'get_definitions_for_feature_set', AsyncMock(return_value=[{'name': 'count', 'version': 1, 'leakage_class': 'safe'}])), \
             patch('backend.ml.readiness.entity_history_statistics', AsyncMock(return_value={})), \
             patch.object(settings, 'ML_ARTIFACT_DIR', str(root)):
            if fail_write:
                with patch.object(dataset_builder, '_atomic_parquet_write', side_effect=OSError('synthetic disk fault')):
                    with self.assertRaises(OSError):
                        await dataset_builder.build_dataset(db, name='debug-fixture', kind=kind, diagnostics=diag)
                return None, diag, db
            result = await dataset_builder.build_dataset(db, name='debug-fixture', kind=kind, diagnostics=diag)
            return result, diag, db

    async def test_real_builder_records_stages_without_changing_snapshot(self):
        import pyarrow.parquet as pq
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); result, diag, db = await self.run_build(root)
            self.assertEqual(result['status'], 'built')
            self.assertEqual(result['row_count'], 30)
            self.assertEqual(diag.events[-1]['stage'], 'registering_dataset')
            self.assertTrue(all(event['status'] == 'completed' for event in diag.events))
            self.assertIn('definitions', result['quality_report']['debug_contract'])
            self.assertEqual(pq.read_table(root / 'datasets/debug-fixture-v1.parquet').num_rows, 30)
            self.assertEqual(db.commit.await_count, 1)

    async def test_supervised_build_keeps_requested_name_and_records_labels(self):
        with tempfile.TemporaryDirectory() as folder:
            result, diag, _ = await self.run_build(Path(folder), count=60, kind='supervised')
            self.assertEqual(result['status'], 'built')
            self.assertEqual(result['name'], 'debug-fixture')
            matched = next(e for e in diag.events if e['stage'] == 'matching_labels')
            self.assertEqual(matched['reviewed_labels'], 60)
            self.assertEqual(diag.configuration['kind'], 'supervised')

    async def test_real_validation_failure_is_located_and_writes_no_artifact(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); result, diag, db = await self.run_build(root, count=2)
            self.assertEqual(result['status'], 'failed')
            self.assertEqual(diag.events[-1]['stage'], 'validation')
            self.assertIn('minimum_rows', diag.failure['failed_checks'])
            self.assertEqual(list(root.rglob('*.parquet')), [])

    async def test_real_artifact_error_records_code_location_without_registering(self):
        with tempfile.TemporaryDirectory() as folder:
            _, diag, db = await self.run_build(Path(folder), fail_write=True)
            self.assertEqual(diag.events[-1]['stage'], 'writing_artifact')
            self.assertEqual(diag.failure['code'], 'OSError')
            self.assertTrue(any(frame['module'] == 'backend/ml/dataset_builder.py' for frame in diag.failure['frames']))
            self.assertEqual(db.commit.await_count, 0)


if __name__ == '__main__':
    unittest.main()

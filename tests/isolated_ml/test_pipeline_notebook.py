"""Execute the real export cells offline; no app startup, DB or production files."""
import ast
import contextlib
import hashlib
import importlib.util
import io
import json
import socket
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]


def load(name):
    spec = importlib.util.spec_from_file_location('notebook_test_' + name, ROOT / 'backend/ml' / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


notebooks, steps, validation = (load(name) for name in ('debug_notebook', 'dataset_steps', 'data_validator'))


class PipelineNotebookTests(unittest.TestCase):
    def execute(self, notebook, root=Path('/tmp')):
        namespace = {}
        with patch.object(socket, 'socket', side_effect=AssertionError('Notebook attempted network access')), contextlib.redirect_stdout(io.StringIO()):
            for index, cell in enumerate(notebook['cells']):
                if cell['cell_type'] == 'code':
                    source = ''.join(cell['source']).replace('Path("/artifacts")', 'Path(' + repr(str(root)) + ')')
                    exec(compile(source, f'notebook-{index}', 'exec'), namespace)
                    if 'ARTIFACT_ROOT' in namespace:
                        namespace['ARTIFACT_ROOT'] = root
        return namespace

    def fixture(self, root, model_type='behavior_anomaly_model'):
        import pyarrow as pa
        import pyarrow.parquet as pq
        rows = [{'entity_id': str(index), 'as_of': datetime(2025, 1, 1) + timedelta(days=index),
                 'features': {'count': float(index), 'target': float(index * 2)}, 'snapshot_id': index + 1,
                 'label': 'positive' if index % 2 else 'negative'} for index in range(40)]
        definitions = [{'name': name, 'version': 1, 'leakage_class': 'safe'} for name in ('count', 'target')]
        quality = validation.validate_rows(rows, kind='unsupervised', definitions=definitions, now=datetime(2026, 1, 1))
        quality['debug_contract'] = {'helper_sha256': notebooks.helper_hashes(), 'definitions': definitions,
                                    'split': {'strategy': 'temporal', 'val_fraction': .2, 'holdout_fraction': .2}}
        train, val, test, report = steps.split_rows(rows, 'temporal')
        for name, part in [('train', train), ('val', val), ('test', test)]:
            for row in part:
                row['split'] = name
        artifact = root / 'datasets/test.parquet'
        artifact.parent.mkdir()
        pq.write_table(pa.Table.from_pylist([dict(entity_id=row['entity_id'], as_of=row['as_of'].isoformat(),
            features_json=json.dumps(row['features']), label=row['label'], snapshot_id=row['snapshot_id'], split=row['split']) for row in rows]), artifact)
        median = (train[len(train) // 2 - 1]['features']['count'] + train[len(train) // 2]['features']['count']) / 2
        evidence = {'artifact_available': True, 'artifact_relative': 'datasets/test.parquet',
            'dataset': {'kind': 'unsupervised', 'quality_report': quality, 'split_config': report,
                        'checksum': steps.dataset_fingerprint(rows), 'parquet_sha256': hashlib.sha256(artifact.read_bytes()).hexdigest()},
            'pipeline': {'model_type': model_type, 'training': {'configuration': {'feature_names': ['count'],
                'imputation_medians': {'count': median}, 'feature_coverage_floor': .7,
                'pipeline': {'features': ['count'], 'target': 'target'} if model_type == 'tabular_regression_model' else {'features': ['count']}}},
                'feature_lineage': {'samples': [{'id': 1, 'features': {'count': 0}}], 'definitions': definitions}}}
        return evidence, artifact

    def test_first_cell_displays_verified_database_extraction(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            evidence, artifact = self.fixture(root)
            book = notebooks.build_pipeline_debug_notebook(evidence)
            self.assertEqual(book['cells'][0]['cell_type'], 'code')
            scope = self.execute(book, root)
            self.assertEqual(len(scope['data_used_rows']), 20)
            self.assertIn('feature.count', scope['preview_records'][0])
            self.assertIn('split', scope['preview_records'][0])
            self.assertIn('THIS IS THE DATA USED', ''.join(book['cells'][0]['source']))

    def test_first_cell_refuses_changed_data_before_preview(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            evidence, artifact = self.fixture(root)
            artifact.write_bytes(artifact.read_bytes() + b'changed')
            book = notebooks.build_pipeline_debug_notebook(evidence)
            with self.assertRaisesRegex(AssertionError, 'checksum mismatch'):
                self.execute(book, root)

    def test_starter_without_data_executes_and_lists_all_steps(self):
        evidence = {'artifact_available': False, 'pipeline': {'model_type': 'behavior_anomaly_model'}}
        book = notebooks.build_pipeline_debug_notebook(evidence)
        scope = self.execute(book)
        self.assertEqual(scope['pipeline']['model_type'], 'behavior_anomaly_model')
        text = json.dumps(book)
        for phase in ('Collection', 'Feature meaning', 'Extraction', 'Split', 'Training recipe', 'Evaluation', 'Reproducibility', 'actual service usage'):
            self.assertIn(phase, text)
        self.assertIn('not automatically the historical code', text)
        self.assertEqual(book['metadata']['vas_debug_export'], 2)
        self.assertEqual(len({cell['id'] for cell in book['cells']}), len(book['cells']))

    def test_failed_run_keeps_failure_and_preparation_without_fake_training(self):
        evidence = {'artifact_available': False, 'job': {'status': 'failed', 'error_code': 'PREPARATION_CANCELLED'},
                    'pipeline': {'preparation': {'status': 'cancelled', 'rows_scanned': 4},
                                 'training': {'failure': {'code': 'PREPARATION_CANCELLED'}}}}
        scope = self.execute(notebooks.build_pipeline_debug_notebook(evidence))
        self.assertEqual(scope['pipeline']['preparation']['rows_scanned'], 4)
        self.assertEqual(scope['training']['failure']['code'], 'PREPARATION_CANCELLED')
        self.assertEqual(scope['training_config'], {})

    def test_metadata_cannot_execute_code(self):
        marker = "'); raise RuntimeError('untrusted'); #"
        evidence = {'artifact_available': False, 'pipeline': {'model_type': marker, 'service': {'state': marker}}}
        scope = self.execute(notebooks.build_pipeline_debug_notebook(evidence))
        self.assertEqual(scope['evidence'], evidence)

    def test_source_references_are_current_repository_functions(self):
        references = notebooks.pipeline_source_references()
        for key, item in references.items():
            self.assertEqual(hashlib.sha256(item['source'].encode()).hexdigest(), item['sha256'], key)
            self.assertTrue(item['module'].startswith('backend/ml/'))
            self.assertFalse(item['module'].startswith('/'))
            ast.parse(item['source'])
        self.assertIn('start_time < as_of', references['person_context']['source'])
        self.assertIn('MLLabel.review_status == "reviewed"', references['dataset']['source'])

    def test_database_and_fit_source_stays_nonexecuting_data(self):
        book = notebooks.build_pipeline_debug_notebook({'artifact_available': False})
        for cell in book['cells']:
            if cell['cell_type'] != 'code':
                continue
            tree = ast.parse(''.join(cell['source']))
            imports = [node.module or '' for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)]
            self.assertFalse(any(name.startswith(('backend', 'config', 'db_connection')) for name in imports))
            call_names = [node.func.id for node in ast.walk(tree) if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)]
            self.assertNotIn('exec', call_names)
            self.assertNotIn('eval', call_names)

    def test_verified_snapshot_matches_medians_and_vectors_for_each_family(self):
        for family in ('behavior_anomaly_model', 'coappearance_anomaly_model', 'social_graph_anomaly_model', 'threat_ranking_model', 'tabular_regression_model'):
            with self.subTest(family=family), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                evidence, artifact = self.fixture(root, family)
                before = artifact.read_bytes()
                scope = self.execute(notebooks.build_pipeline_debug_notebook(evidence), root)
                self.assertEqual(scope['matrix_recheck']['status'], 'matched')
                self.assertEqual(scope['matrix_recheck']['feature_names'], ['count'])
                self.assertEqual(scope['matrices']['train'].shape[1], 1)
                self.assertEqual(artifact.read_bytes(), before)

    def test_saved_matrix_contract_drift_stops(self):
        for change in ('feature_names', 'imputation_medians', 'feature_coverage_floor'):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                evidence, _ = self.fixture(root)
                evidence['pipeline']['training']['configuration'][change] = {
                    'feature_names': ['target'], 'imputation_medians': {'count': -500}, 'feature_coverage_floor': .5}[change]
                with self.assertRaises(AssertionError):
                    self.execute(notebooks.build_pipeline_debug_notebook(evidence), root)

    def test_corrupted_snapshot_stops_before_matrix_or_record_loading(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            evidence, artifact = self.fixture(root)
            artifact.write_bytes(artifact.read_bytes() + b'changed')
            with self.assertRaisesRegex(AssertionError, 'checksum mismatch'):
                self.execute(notebooks.build_pipeline_debug_notebook(evidence), root)

    def test_missing_model_contract_keeps_dataset_recheck_useful(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            evidence, _ = self.fixture(root)
            evidence['pipeline']['training'] = {}
            scope = self.execute(notebooks.build_pipeline_debug_notebook(evidence), root)
            self.assertTrue(scope['rechecked']['passed'])
            self.assertEqual(scope['matrix_recheck']['status'], 'unavailable')

    def test_feature_samples_are_bounded_in_display(self):
        book = notebooks.build_pipeline_debug_notebook({'artifact_available': False,
            'pipeline': {'feature_lineage': {'samples': [{'id': number} for number in range(5)]}}})
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            namespace = {}
            for cell in book['cells']:
                if cell['cell_type'] == 'code':
                    exec(''.join(cell['source']), namespace)
        self.assertIn("Snapshot 2", output.getvalue())
        self.assertNotIn("Snapshot 3", output.getvalue())
        self.assertEqual(len(namespace['feature_samples']), 3)


    def test_timeline_tables_do_not_invent_completion_and_keep_full_metadata(self):
        evidence = {'artifact_available': False, 'pipeline': {'preparation': {'status': 'prepared', 'rows_scanned': 7},
            'training': {'stage_history': [{'stage': 'training', 'started_at': '2026-01-01T00:00:00Z', 'duration_seconds': 3.5}, {'stage': 'evaluating'}]},
            }, 'diagnostics': {'stage_history': [{'stage': 'extracting_rows', 'status': 'completed', 'output_rows': 5}]}}
        scope = self.execute(notebooks.build_pipeline_debug_notebook(evidence))
        self.assertEqual(scope['timeline_rows']['Preparation'][0]['status'], 'prepared')
        self.assertEqual(scope['timeline_rows']['Dataset'][0]['status'], 'completed')
        self.assertEqual(scope['timeline_rows']['Training'][0]['status'], 'boundary recorded')
        self.assertEqual(scope['timeline_rows']['Training'][0]['duration_seconds'], 3.5)
        self.assertEqual(scope['timeline_details']['Training']['stage_history'][0]['started_at'], '2026-01-01T00:00:00Z')
        self.assertEqual(scope['timeline_details']['Dataset']['stage_history'][0]['output_rows'], 5)

    def test_feature_value_table_distinguishes_zero_missing_and_unavailable_reason(self):
        evidence = {'artifact_available': False, 'pipeline': {'feature_lineage': {'samples': [
            {'id': 77, 'features': {'zero': 0, 'present': 3.4, 'empty': None}, 'missingness': {'history': 'insufficient_history'}, 'source_row_counts': {'appearances': 2}}]}}}
        scope = self.execute(notebooks.build_pipeline_debug_notebook(evidence))
        values = {row['feature']: row for row in scope['feature_value_rows'][0]}
        self.assertEqual(values['zero']['availability'], 'available')
        self.assertEqual(values['zero']['value'], 0)
        self.assertEqual(values['empty']['availability'], 'unavailable')
        self.assertEqual(values['history']['reason'], 'insufficient_history')
        self.assertIsNone(values['history']['value'])
        self.assertEqual(scope['feature_samples'][0]['source_row_counts'], {'appearances': 2})


if __name__ == '__main__':
    unittest.main()

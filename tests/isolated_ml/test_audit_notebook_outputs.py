"""Negative checks against a real exported bundle: run this file with bundle path."""
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from audit_notebook_outputs import audit_bundle, audit_family

BUNDLE = None


class OutputAuditTests(unittest.TestCase):
    def setUp(self):
        if BUNDLE is None:
            self.skipTest('Supply an executed validation bundle to this standalone test')
        self.evidence = json.loads((BUNDLE / 'behavior_anomaly_model-evidence.json').read_text())
        self.result = json.loads((BUNDLE / 'behavior_anomaly_model-result.json').read_text())

    def assert_rejected(self, check):
        result = audit_family(self.evidence, self.result)
        self.assertFalse(result['passed'])
        self.assertIn(check, [c['check'] for c in result['checks'] if not c['passed']])

    def test_real_bundle_passes(self):
        self.assertTrue(audit_bundle(BUNDLE)['passed'])

    def test_wrong_serving_model_rejected(self):
        self.result['consumer']['model_id'] = 'wrong-model'
        self.assert_rejected('selected_and_consumed_model_match')

    def test_wrong_threshold_band_rejected(self):
        self.result['consumer']['ml_anomaly_band'] = 'incorrect-band'
        self.assert_rejected('consumer_band_matches_thresholds')

    def test_probability_misrepresentation_rejected(self):
        self.result['consumer']['is_probability'] = True
        self.assert_rejected('consumer_0_score_contract')

    def test_unaccounted_dataset_rows_rejected(self):
        self.evidence['dataset']['row_count'] += 1
        self.assert_rejected('row_accounting')

    def test_evaluation_population_mismatch_rejected(self):
        self.result['evaluation']['splits']['test']['rows'] += 1
        self.assert_rejected('test_evaluation_population')

    def test_nan_prediction_rejected(self):
        self.result['consumer']['behavioral_anomaly_score'] = float('nan')
        self.assert_rejected('consumer_0_score_contract')

    def test_different_training_release_rejected(self):
        self.evidence['dataset']['code_version'] = 'another-release'
        self.assert_rejected('release_identity')

    def test_changed_saved_output_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / 'notebooks').mkdir()
            for file in BUNDLE.glob('*.json'):
                shutil.copyfile(file, root / file.name)
            for file in (BUNDLE / 'notebooks').glob('*-executed.ipynb'):
                shutil.copyfile(file, root / 'notebooks' / file.name)
            target = root / 'notebooks' / 'behavior_anomaly_model-executed.ipynb'
            book = json.loads(target.read_text())
            cell = next(c for c in book['cells'] if c['cell_type'] == 'code' and c.get('outputs'))
            cell['outputs'][0]['text'] = 'Fabricated successful output'
            target.write_text(json.dumps(book))
            report = audit_bundle(root)
            self.assertFalse(report['passed'])
            failed = next(f for f in report['families'] if f['family'] == 'behavior_anomaly_model')
            self.assertIn('saved_cell_outputs_match_execution_report', [c['check'] for c in failed['checks'] if not c['passed']])


if __name__ == '__main__':
    if len(sys.argv) < 2:
        raise SystemExit('Usage: test_audit_notebook_outputs.py EXECUTED_BUNDLE')
    BUNDLE = Path(sys.argv.pop(1))
    unittest.main()

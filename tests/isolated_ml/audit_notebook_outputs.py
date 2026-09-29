"""Read-only semantic audit of the synthetic service-validation bundle.

Unlike cell success alone, these checks compare evidence across stage boundaries.
No application imports, database access, model loading or deployment occurs.
"""
import hashlib
import json
import math
from pathlib import Path

FAMILIES = ('behavior_anomaly_model', 'coappearance_anomaly_model',
            'social_graph_anomaly_model', 'threat_ranking_model', 'tabular_regression_model')


def audit_family(evidence, result):
    family = result['family']
    model, dataset = evidence['pipeline']['model'], evidence['dataset']
    service = evidence['pipeline']['service']
    checks, observations = [], []

    def check(name, condition):
        checks.append({'check': name, 'passed': bool(condition)})

    check('worker_completed', result['status'] == 'passed' and result['training_status'] == 'completed')
    check('model_job_dataset_lineage', model['id'] == result['model_id']
          and model['model_type'] == family == evidence['pipeline']['model_type']
          and model['dataset_id'] == dataset['id']
          and model['training_job_id'] == result['job_id'] == evidence['job']['job_id'])
    check('release_identity', bool(model['code_version']) and
          model['code_version'] == result['code_version'] == dataset['code_version'])
    split = dataset['split_config']
    counts = split['counts']
    check('row_accounting', sum(counts.values()) + split.get('dropped_for_group_integrity', 0) == dataset['row_count'])
    for name in ('train', 'val', 'test'):
        metrics = result['evaluation']['splits'][name]
        check(name + '_evaluation_population', counts[name] > 0 and metrics['rows'] == counts[name])
        if 'band_counts' in metrics:
            check(name + '_band_population', sum(metrics['band_counts'].values()) == metrics['rows'])
        quantiles = [metrics[k] for k in ('score_p50', 'score_p90', 'score_p99') if k in metrics]
        check(name + '_score_quantiles', all(math.isfinite(x) and 0 <= x <= 1 for x in quantiles)
              and quantiles == sorted(quantiles))
    # Notebook evidence intentionally removes free-text notes and some internal
    # checks. Compare quantitative results and score semantics, not redacted text.
    keys = ('splits', 'baseline', 'band_cutpoints', 'score_type', 'is_probability', 'calibration_status')
    check('evaluation_matches_registered_model', all(result['evaluation'].get(k) == model['evaluation_report'].get(k) for k in keys))
    check('engineering_consistency', result['engineering'] == model['training_config']['engineering_gate'] == 'PASS')
    check('scientific_status_preserved', result['scientific'] == model['training_config']['scientific_gate'])
    observations.append('Scientific status: ' + result['scientific'] + '; engineering success is not accuracy approval.')
    if family == 'tabular_regression_model':
        check('regression_cannot_connect', result['expected_offline_refusal'] == 'OFFLINE_ONLY'
              and service['selected_model'] is None and service['destination']['mode'] == 'offline_only')
        if result['evaluation']['splits']['test'].get('r2', 0) > .99:
            observations.append('Near-perfect regression fit on synthetic data: inspect target/predictor relationships; this is not evidence of generalization.')
    else:
        consumer = result['consumer']
        check('selected_and_consumed_model_match', service['selected_model']['id'] == consumer['model_id'] == model['id'])
        check('stop_archives_same_model', result['stop']['success'] and
              result['stop']['archived_model']['id'] == model['id'] and result['stop']['archived_model']['stage'] == 'archived')
        if family == 'threat_ranking_model':
            items = consumer['items']
            check('ranking_population', consumer['failed'] == 0 and consumer['scored'] == consumer['total'] == len(items))
            check('ranking_order', [x['score'] for x in items] == sorted((x['score'] for x in items), reverse=True))
            observations.append('Ranking test ROC AUC: ' + str(result['evaluation']['splits']['test'].get('roc_auc')) + '; synthetic labels do not establish useful discrimination.')
        else:
            items = [consumer]
        for index, item in enumerate(items):
            score = item.get('score', item.get('behavioral_anomaly_score'))
            check(f'consumer_{index}_score_contract', isinstance(score, (float, int)) and math.isfinite(score) and 0 <= score <= 1
                  and item['is_probability'] is False and item['model_id'] == model['id']
                  and item['feature_set_version'] == model['feature_set_version'])
            if family != 'behavior_anomaly_model':
                check(f'consumer_{index}_observational_only', item['applied_to_live_result'] is False)
            if 'anomaly' in family:
                cutpoints = consumer.get('band_cutpoints') or result['evaluation']['band_cutpoints']
                ordered = [cutpoints[k] for k in ('elevated', 'unusual', 'highly_unusual')]
                check('ordered_thresholds', all(math.isfinite(x) and 0 <= x <= 1 for x in ordered) and ordered == sorted(ordered))
                band = item.get('band', item.get('ml_anomaly_band'))
                # API scores are rounded to six decimals; accept either side only
                # inside that rounding interval, never an unrelated band.
                def band_at(value):
                    return next((k for k in ('highly_unusual', 'unusual', 'elevated') if value >= cutpoints[k]), 'normal')
                check('consumer_band_matches_thresholds', band in {band_at(score - .0000005), band_at(score + .0000005)})
            if item.get('unavailable_features'):
                observations.append('Consumer reported unavailable features: ' + ', '.join(item['unavailable_features']) + '. A score is not complete feature coverage.')
        usage = service['consumption']
        if family == 'behavior_anomaly_model':
            observations.append('Behavioral inference was tested directly; assessment-linked service usage was not exercised.'
                                if not usage.get('selected_model_used') else 'Assessment-linked usage is recorded.')
        else:
            check('usage_matches_selection', usage['selected_model_used'] and usage['model_id'] == model['id'] and bool(usage['last_success_at']))
    return {'family': family, 'passed': all(c['passed'] for c in checks), 'checks': checks, 'observations': observations}


def audit_bundle(root):
    root = Path(root)
    cell_report = json.loads((root / 'cell-execution-report.json').read_text())
    if len(cell_report) != len(FAMILIES) or {r['family'] for r in cell_report} != set(FAMILIES):
        raise ValueError('Incomplete or duplicate notebook family reports')
    results = []
    for family in FAMILIES:
        evidence = json.loads((root / (family + '-evidence.json')).read_text())
        worker = json.loads((root / (family + '-result.json')).read_text())
        result = audit_family(evidence, worker)
        book = json.loads((root / 'notebooks' / (family + '-executed.ipynb')).read_text())
        recorded = next(r for r in cell_report if r['family'] == family)
        cells = [(i, c) for i, c in enumerate(book['cells']) if c['cell_type'] == 'code']
        output_ok = recorded['status'] == 'passed' and len(cells) == len(recorded['cells']) == recorded['executed_code_cells'] == recorded['total_code_cells']
        for order, ((index, cell), entry) in enumerate(zip(cells, recorded['cells']), 1):
            outputs = cell.get('outputs', [])
            # nbformat serializes multiline text as arrays on disk; the execution
            # callback hashes its in-memory string representation.
            for output in outputs:
                if isinstance(output.get('text'), list):
                    output['text'] = ''.join(output['text'])
                for mime, value in output.get('data', {}).items():
                    if isinstance(value, list) and mime != 'application/json':
                        output['data'][mime] = ''.join(value)
            digest = hashlib.sha256(json.dumps(outputs, sort_keys=True).encode()).hexdigest()
            output_ok &= (index == entry['cell_index'] and cell['execution_count'] == entry['execution_count'] == order
                          and entry['status'] == 'passed' and digest == entry['outputs_sha256']
                          and not any(o['output_type'] == 'error' for o in outputs))
        result['checks'].append({'check': 'saved_cell_outputs_match_execution_report', 'passed': bool(output_ok)})
        result['passed'] &= bool(output_ok)
        results.append(result)
    return {'passed': all(r['passed'] for r in results), 'families': results,
            'scope': 'Synthetic recorded-output consistency; not live-traffic or scientific validation.'}


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('bundle', type=Path)
    args = parser.parse_args()
    report = audit_bundle(args.bundle)
    print(json.dumps(report, indent=2))
    raise SystemExit(0 if report['passed'] else 1)

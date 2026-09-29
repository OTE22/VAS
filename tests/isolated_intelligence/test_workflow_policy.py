"""Guidance and every registry entry point share connection prerequisites."""
import asyncio
from copy import deepcopy
from types import SimpleNamespace as Row
from unittest.mock import AsyncMock, patch
import pytest
from conftest import settings
from test_service_deployment import model
from backend.ml.workflow_policy import connection_blockers, training_readiness
from backend.ml.registry_service import RegistryError, registry_service

run = asyncio.run

@pytest.mark.parametrize('family', ['behavior_anomaly_model', 'threat_ranking_model'])
@pytest.mark.parametrize('split,count', [('val', 0), ('test', None), ('test', True), ('train', -1)])
def test_missing_evaluation_cannot_connect(family, split, count):
    row = model(family)
    row.evaluation_report['splits'][split]['rows'] = count
    assert 'EVALUATION_COVERAGE_REQUIRED' in [b['code'] for b in connection_blockers(row, family)]


def test_evaluation_requires_both_ranking_classes():
    row = model('threat_ranking_model')
    row.evaluation_report['splits']['test']['positive'] = 0
    assert 'EVALUATION_CLASSES_REQUIRED' in [b['code'] for b in connection_blockers(row, row.model_type)]


@pytest.mark.parametrize('family,target', [('behavior_anomaly_model', 'shadow'), ('threat_ranking_model', 'approved')])
def test_direct_registry_api_cannot_bypass_held_out_gate(family, target):
    row = model(family)
    row.evaluation_report['splits']['test']['rows'] = 0
    db = Row(execute=AsyncMock(return_value=Row(scalar_one_or_none=lambda: row)))
    with patch('backend.ml.registry_service.validate_artifact') as artifact:
        with pytest.raises(RegistryError) as exc:
            run(registry_service.transition(db, str(row.id), to_stage=target, actor='fixture', reason='reviewed'))
        assert exc.value.code == 'EVALUATION_COVERAGE_REQUIRED'
        artifact.assert_not_called()


def test_engineering_and_evaluation_do_not_claim_scientific_approval():
    row = model()
    row.training_config['scientific_gate'] = 'INSUFFICIENT_EVIDENCE'
    assert connection_blockers(row, row.model_type) == []
    # This is only an observational connection; existing live mode gates remain.


def test_no_label_data_has_actionable_requirements():
    r = run(training_readiness(None, 'threat_ranking_model', labels={
        'supervised_gate_open': False, 'counted_reviewed_manual': {'total': 3, 'positive': 2, 'negative': 1},
        'required_total': 100, 'required_per_class': 25}))
    assert not r['ready'] and '3/100' in r['blockers'][0]['message']


def test_graph_history_shortfall_is_explained(monkeypatch):
    for name, value in [('ML_GRAPH_MIN_NODES',25), ('ML_GRAPH_MIN_EDGES',50), ('ML_GRAPH_MIN_OBSERVATION_DAYS',14)]:
        monkeypatch.setattr(settings, name, value, raising=False)
    db = Row(execute=AsyncMock(return_value=Row(mappings=lambda: Row(one=lambda: {'edges': 186, 'nodes': 26, 'span_days': 1.2}))))
    r = run(training_readiness(db, 'social_graph_anomaly_model'))
    assert not r['ready'] and len(r['blockers']) == 1 and '1.2/14' in r['blockers'][0]['message']


def test_saved_dataset_avoids_current_source_history_gate(monkeypatch):
    from backend.ml import guided_training, job_service
    monkeypatch.setattr(job_service, 'ml_worker_health', AsyncMock(return_value={'status': 'healthy'}))
    row = Row(status='built', kind='unsupervised', feature_set_version='secintel-features-v2', definition_name='behavior_anomaly_person')
    db = Row(execute=AsyncMock(return_value=Row(scalar_one_or_none=lambda: row)))
    with patch('backend.ml.workflow_policy.training_readiness', AsyncMock()) as readiness:
        run(guided_training.preflight_guided_training(db, model_type='behavior_anomaly_model', dataset_id='11111111-1111-4111-8111-111111111111'))
        readiness.assert_not_awaited()

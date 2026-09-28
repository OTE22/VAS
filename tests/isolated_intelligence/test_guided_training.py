import asyncio
import sys
from contextlib import asynccontextmanager
from types import ModuleType, SimpleNamespace as Row
from unittest.mock import AsyncMock
import pytest
from conftest import manager
from backend.ml import guided_training as guided, job_service

run=asyncio.run

@pytest.fixture
def healthy(monkeypatch):
    monkeypatch.setattr(job_service,'ml_worker_health',AsyncMock(return_value={'status':'healthy'}))


def test_guided_worker_preflight_rejects_unavailable(monkeypatch):
    monkeypatch.setattr(job_service,'ml_worker_health',AsyncMock(return_value={'status':'stale'}))
    with pytest.raises(guided.GuidedTrainingRefusal) as exc:
        run(guided.preflight_guided_training(None,model_type='behavior_anomaly_model'))
    assert exc.value.code=='ML_WORKER_UNAVAILABLE' and exc.value.details['status_code']==503


def test_guided_regression_requires_explicit_target(healthy):
    with pytest.raises(guided.GuidedTrainingRefusal) as exc:
        run(guided.preflight_guided_training(None,model_type='tabular_regression_model'))
    assert exc.value.code=='ADVANCED_CONFIGURATION_REQUIRED'


def test_guided_ranking_explains_missing_reviewed_labels(monkeypatch,healthy):
    module=ModuleType('backend.ml.labeling_service')
    module.labeling_service=Row(label_stats=AsyncMock(return_value={'supervised_gate_open':False}),supervised_refusal=lambda s:{'required_total':100})
    monkeypatch.setitem(sys.modules,'backend.ml.labeling_service',module)
    with pytest.raises(guided.GuidedTrainingRefusal) as exc:
        run(guided.preflight_guided_training(None,model_type='threat_ranking_model'))
    assert exc.value.code=='INSUFFICIENT_REVIEWED_LABELS'
    assert exc.value.details['label_readiness']['required_total']==100


@pytest.mark.parametrize('row,code',[(None,'DATASET_NOT_FOUND'),(Row(status='failed'),'DATASET_NOT_READY'),(Row(status='built',kind='unsupervised',feature_set_version='coappearance-features-v1'),'DATASET_SERVICE_MISMATCH'),(Row(status='built',kind='unsupervised',feature_set_version='secintel-features-v2',definition_name='another_task'),'DATASET_SERVICE_MISMATCH')])
def test_guided_dataset_must_belong_to_selected_service(healthy,row,code):
    db=Row(execute=AsyncMock(return_value=Row(scalar_one_or_none=lambda:row)))
    with pytest.raises(guided.GuidedTrainingRefusal) as exc:
        run(guided.preflight_guided_training(db,model_type='behavior_anomaly_model',dataset_id='11111111-1111-4111-8111-111111111111'))
    assert exc.value.code==code


def test_guided_compatible_dataset_passes(healthy):
    row=Row(status='built',kind='unsupervised',feature_set_version='secintel-features-v2',definition_name='behavior_anomaly_person')
    db=Row(execute=AsyncMock(return_value=Row(scalar_one_or_none=lambda:row)))
    run(guided.preflight_guided_training(db,model_type='behavior_anomaly_model',dataset_id='11111111-1111-4111-8111-111111111111'))


def test_guided_saved_dataset_is_reused_without_new_feature_collection():
    result=run(guided.prepare_training_inputs('job',{'prepare_features':True,'dataset_id':'existing'}))
    assert result['status']=='reused'


def preparation_harness(monkeypatch,result):
    from backend.core.task_history import task_history_manager
    @asynccontextmanager
    async def session(): yield object()
    monkeypatch.setattr(manager,'get_session',session)
    module=ModuleType('backend.ml.collector')
    module.run_collection=AsyncMock(return_value=result)
    monkeypatch.setitem(sys.modules,'backend.ml.collector',module)
    progress=AsyncMock();monkeypatch.setattr(task_history_manager,'update_progress',progress)
    return module,progress


def test_guided_feature_preparation_is_incremental_and_reports_service(monkeypatch):
    collector,progress=preparation_harness(monkeypatch,{'snapshots_written':20,'snapshots_deduplicated':2})
    result=run(guided.prepare_training_inputs('job',{'prepare_features':True,'model_type':'behavior_anomaly_model'}))
    assert result=={'status':'prepared','snapshots_written':20,'snapshots_reused':2}
    assert collector.run_collection.await_args.kwargs['full_rebuild'] is False
    assert progress.await_args.kwargs['details']['model_type']=='behavior_anomaly_model'


@pytest.mark.parametrize('result,code',[({'cancelled':True},'PREPARATION_CANCELLED'),({'current_state_pending':2},'FEATURE_PREPARATION_INCOMPLETE'),({'status':'failed'},'FEATURE_PREPARATION_INCOMPLETE'),({'status':'busy'},'FEATURE_PREPARATION_INCOMPLETE')])
def test_guided_incomplete_preparation_cannot_continue_to_training(monkeypatch,result,code):
    preparation_harness(monkeypatch,result)
    with pytest.raises(guided.GuidedTrainingRefusal) as exc:
        run(guided.prepare_training_inputs('job',{'prepare_features':True,'model_type':'behavior_anomaly_model'}))
    assert exc.value.code==code

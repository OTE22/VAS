import asyncio
import ast
import importlib
from datetime import datetime, timedelta
from types import SimpleNamespace as Row
from unittest.mock import AsyncMock
import pytest
from conftest import ROOT, settings
from backend.core.trajectory_predictor import TrajectoryPredictor
from backend.core.threshold_learner import ThresholdLearner
from backend.core.activity_correlation import ActivityCorrelationAnalyzer
from backend.ml import threshold_learning

run = asyncio.run
NOW = datetime(2026, 9, 28, 12)

def trajectory(cameras):
    return {'cameras': cameras, 'time_diffs': [2] * (len(cameras)-1)}

def test_one_relevant_transition_is_not_high_confidence():
    predictor = TrajectoryPredictor()
    predictor._get_historical_trajectories = AsyncMock(return_value=[trajectory(['A','B']), trajectory(['C','D']), trajectory(['C','D'])])
    result = run(predictor.predict_with_evidence(None,'id','A',NOW))
    assert result['predictions'] == []
    assert result['supporting_sessions'] == result['total_transitions'] == 1


def test_three_independent_outgoing_sessions_report_counts_not_confidence():
    predictor = TrajectoryPredictor()
    predictor._get_historical_trajectories = AsyncMock(return_value=[trajectory(['A','B']), trajectory(['A','B']), trajectory(['A','C'])])
    result = run(predictor.predict_with_evidence(None,'id','A',NOW))
    assert result['total_transitions'] == result['supporting_sessions'] == 3
    assert result['calibration_status'] == 'uncalibrated'
    assert result['predictions'][0]['probability'] == pytest.approx(2/3)
    assert result['predictions'][0]['transition_count'] == 2
    assert result['predictions'][0]['estimated_time'] == NOW + timedelta(minutes=2)


def test_one_session_with_many_transitions_is_insufficient():
    predictor = TrajectoryPredictor()
    predictor._get_historical_trajectories = AsyncMock(return_value=[trajectory(['A','B'] * 10)])
    result = run(predictor.predict_with_evidence(None,'id','A',NOW))
    assert result['predictions'] == [] and result['total_transitions'] == 10


def test_simultaneous_observations_do_not_establish_direction():
    predictor = TrajectoryPredictor()
    predictor._get_historical_trajectories = AsyncMock(return_value=[dict(cameras=['A','B'],time_diffs=[0])] * 3)
    assert run(predictor.predict_with_evidence(None,'id','A',NOW))['total_transitions'] == 0


def test_trajectory_database_errors_propagate():
    predictor = TrajectoryPredictor()
    predictor._get_historical_trajectories = AsyncMock(side_effect=RuntimeError('database unavailable'))
    with pytest.raises(RuntimeError):
        run(predictor.predict_with_evidence(None,'id','A',NOW))


def test_threshold_database_errors_are_not_no_evidence():
    with pytest.raises(RuntimeError):
        run(ThresholdLearner().learn_thresholds_for_pair(Row(execute=AsyncMock(side_effect=RuntimeError('db'))),'A','B'))


def test_zero_coordinates_are_valid():
    learner=ThresholdLearner()
    learner._pipeline_cache = {'A': Row(latitude=0,longitude=0), 'B': Row(latitude=0,longitude=.001)}
    learner._get_cross_camera_movements = AsyncMock(return_value=[dict(time_diff_minutes=2)]*10)
    result=run(learner.learn_thresholds_for_pair(None,'A','B'))
    assert result['sample_count']==10 and result['actual_distance_meters'] > 100


def test_movements_are_bidirectional_without_redetection_inflation():
    learner=ThresholdLearner()
    learner._appearance_cache = {'A': [Row(identity_id='id',pipeline_id='A',start_time=NOW+timedelta(minutes=m)) for m in (0,1,4)], 'B': [Row(identity_id='id',pipeline_id='B',start_time=NOW+timedelta(minutes=m)) for m in (2,3)]}
    result=run(learner._get_cross_camera_movements(None,'A','B'))
    assert [(r['from_camera'],r['to_camera']) for r in result] == [('A','B'),('B','A')]
    assert len(result)==2


def test_dense_correlation_keeps_exact_score_with_twenty_examples():
    analyzer=ActivityCorrelationAnalyzer()
    a=[Row(pipeline_id='A',start_time=NOW)]*500
    b=[Row(pipeline_id='B',start_time=NOW+timedelta(minutes=1))]*500
    score, examples, meta=analyzer._match_sequences(a,b,{'A':{'B'}},10,{})
    assert score==1.0 and len(examples)==20 and meta['sequence_count']==250000
    assert meta['examples_truncated'] is True


def test_streamed_correlation_matches_reference_scoring():
    analyzer=ActivityCorrelationAnalyzer()
    a=[Row(pipeline_id='A',start_time=NOW+timedelta(seconds=n*10)) for n in range(3)]
    b=[Row(pipeline_id='B' if n%2 else 'C',start_time=NOW+timedelta(minutes=n+1)) for n in range(6)]
    score, seq, meta=analyzer._match_sequences(a,b,{'A':{'B','C'}},10,{})
    assert meta['sequence_count']==18
    assert score==pytest.approx(.7+.3*analyzer._calculate_pattern_consistency(seq))


def test_correlation_direction_and_window_boundaries():
    analyzer=ActivityCorrelationAnalyzer()
    a=[Row(pipeline_id='A',start_time=NOW)]
    b=[Row(pipeline_id='B',start_time=NOW+timedelta(minutes=m)) for m in (0,1,10)]
    _, _, meta=analyzer._match_sequences(a,b,{'A':{'B'}},10,{})
    assert meta['sequence_count']==1
    _, _, reverse=analyzer._match_sequences(b,a,{'B':{'A'}},10,{})
    assert reverse['sequence_count']==0


def test_threshold_persistence_failure_propagates(monkeypatch):
    monkeypatch.setattr(ThresholdLearner,'learn_all_camera_pairs',AsyncMock(return_value={('A','B'): {}}))
    monkeypatch.setattr(threshold_learning,'persist_threshold_candidates',AsyncMock(side_effect=RuntimeError('write failed')))
    with pytest.raises(RuntimeError, match='write failed'):
        run(threshold_learning.run_threshold_learning(None,['A','B']))


def test_empty_learning_has_explicit_outcome(monkeypatch):
    monkeypatch.setattr(ThresholdLearner,'learn_all_camera_pairs',AsyncMock(return_value={}))
    result=run(threshold_learning.run_threshold_learning(None,['A','B']))
    assert result['outcome']=='insufficient_evidence'
    assert result['candidates_written']==0 and result['activation_required'] is False


def endpoint(name, **namespace):
    """Execute actual endpoint function with injected dependencies, no app startup."""
    from fastapi import HTTPException
    from fastapi.responses import JSONResponse
    from sqlalchemy import select
    from db_models import Pipeline
    tree=ast.parse((ROOT/'backend/routes/intelligence.py').read_text())
    node=next(n for n in tree.body if isinstance(n,ast.AsyncFunctionDef) and n.name==name)
    node.decorator_list=[]
    node.args.defaults=[ast.Constant(None)]*len(node.args.args)
    env=dict(settings=settings, HTTPException=HTTPException, JSONResponse=JSONResponse,
        select=select, Pipeline=Pipeline,
        _feature_disabled=lambda *args: HTTPException(status_code=403),
        _safe_500=lambda *args: HTTPException(status_code=500), _audit=lambda *a,**kw:None)
    env.update(namespace)
    compile_tree=ast.Module(body=[ast.ImportFrom(module='__future__',names=[ast.alias(name='annotations')],level=0),node],type_ignores=[])
    exec(compile(ast.fix_missing_locations(compile_tree),str(ROOT/'backend/routes/intelligence.py'),'exec'),env)
    return env[name]

@pytest.mark.parametrize('name,flag', [('create_threshold_job','AUTO_THRESHOLD_LEARNING_ENABLED'), ('calculate_activity_correlation','ACTIVITY_CORRELATION_ENABLED'), ('predict_next_camera','TRAJECTORY_PREDICTION_ENABLED')])
def test_disabled_routes_reject_before_doing_work(name,flag):
    from fastapi import HTTPException
    setattr(settings,flag,False)
    with pytest.raises(HTTPException) as exc:
        run(endpoint(name)())
    assert exc.value.status_code==403


def test_old_learning_endpoint_cannot_bypass_worker():
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc:
        run(endpoint('learn_thresholds')())
    assert exc.value.status_code==410


def test_learning_fetches_camera_history_once_not_once_per_pair():
    learner=ThresholdLearner()
    cameras=[Row(pipeline_id=str(i),latitude=0,longitude=i*.001) for i in range(30)]
    class Result:
        def __init__(self,rows): self.rows=rows
        def scalars(self): return self
        def all(self): return self.rows
    calls=[]
    async def execute(query):
        calls.append(query)
        return Result(cameras if len(calls)==1 else [])
    progress=AsyncMock()
    learned=run(learner.learn_all_camera_pairs(Row(execute=execute),[p.pipeline_id for p in cameras],progress=progress))
    assert learned=={} and len(calls)==31
    assert progress.await_args.args==(95,)
    assert learner._appearance_cache is None and learner._pipeline_cache is None


def test_capabilities_separates_disabled_and_available(monkeypatch):
    import sys, threading, json
    from types import ModuleType
    import backend.ml.job_service as jobs
    from datetime import datetime
    monkeypatch.setattr(jobs,'_active_job',AsyncMock(return_value=None))
    monkeypatch.setattr(jobs,'ml_worker_health',AsyncMock(return_value={'status':'offline'}))
    maps=ModuleType('backend.core.map_availability'); maps.cached=lambda:None
    monkeypatch.setitem(sys.modules,'backend.core.map_availability',maps)
    decision=ModuleType('backend.ml.decision_service'); decision.decision_service=Row(decision_engine_state=AsyncMock(return_value={}))
    monkeypatch.setitem(sys.modules,'backend.ml.decision_service',decision)
    from fastapi.encoders import jsonable_encoder
    env=dict(peek_holder=AsyncMock(return_value=None), _relationship_job_lock=threading.Lock(),
        _RELATIONSHIP_JOB={'job_id':None}, datetime=datetime, jsonable_encoder=jsonable_encoder,
        _iso_z=lambda dt:dt.isoformat(), rules_engine_block=lambda *a:{},
        **{name:'test' for name in ('NETWORK_RISK_VERSION','PATTERN_ALGORITHM_VERSION','ANOMALY_ALGORITHM_VERSION','THREAT_ALGORITHM_VERSION','THRESHOLD_ALGORITHM_VERSION','TRAJECTORY_MODEL_VERSION','CORRELATION_ALGORITHM_VERSION')})
    response=run(endpoint('get_security_capabilities',**env)())
    caps=json.loads(response.body)['capabilities']
    assert caps['threshold_learning']['status']=='worker_unavailable'
    assert caps['trajectory_prediction']['status']=='available'
    for key in ('AUTO_THRESHOLD_LEARNING_ENABLED','TRAJECTORY_PREDICTION_ENABLED','ACTIVITY_CORRELATION_ENABLED'):
        setattr(settings,key,False)
    caps=json.loads(run(endpoint('get_security_capabilities',**env)()).body)['capabilities']
    for key in ('threshold_learning','trajectory_prediction','activity_correlation'):
        assert caps[key]['enabled'] is False and caps[key]['status']=='disabled'


def test_threshold_api_rejects_offline_worker(monkeypatch):
    from contextlib import asynccontextmanager
    from fastapi import HTTPException
    from conftest import manager
    import backend.ml.job_service as jobs
    @asynccontextmanager
    async def context():
        yield Row()
    monkeypatch.setattr(manager,'get_session',context)
    monkeypatch.setattr(jobs,'ml_worker_health',AsyncMock(return_value={'status':'offline'}))
    enqueue=AsyncMock();monkeypatch.setattr(jobs,'enqueue_ml_job',enqueue)
    with pytest.raises(HTTPException) as exc:
        run(endpoint('create_threshold_job')())
    assert exc.value.status_code==503
    enqueue.assert_not_awaited()


def test_threshold_api_does_not_acknowledge_failed_commit(monkeypatch):
    from contextlib import asynccontextmanager
    from fastapi import HTTPException
    from conftest import manager
    import backend.ml.job_service as jobs
    @asynccontextmanager
    async def context():
        yield Row(commit=AsyncMock(side_effect=RuntimeError('commit failed')))
    monkeypatch.setattr(manager,'get_session',context)
    monkeypatch.setattr(jobs,'ml_worker_health',AsyncMock(return_value={'status':'healthy'}))
    monkeypatch.setattr(jobs,'enqueue_ml_job',AsyncMock(return_value={'job_id':'test-job'}))
    with pytest.raises(HTTPException) as exc:
        run(endpoint('create_threshold_job')())
    assert exc.value.status_code==500

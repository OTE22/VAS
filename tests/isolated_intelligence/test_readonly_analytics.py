"""Regression tests for existing-data inspection. Production is never a target."""
import asyncio
import hashlib
import json
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace as Row
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import text, event
from conftest import engine, sessions, settings
from db_models import (Base, Pipeline, Identity, IdentityType, Detection, Face,
                       IdentityAppearance as A, ThreatAssessmentRecord as T, MLLabel)
from backend.core.trajectory_predictor import (TrajectoryPredictor, camera_sessions,
                                               observed_camera_examples)
from backend.ml.dataset_explorer import explore_existing_data, explore_dataset, analytics_scope
from backend.ml.debug_notebook import build_analytics_notebook

run = asyncio.run
START = datetime(2026, 9, 20)
END = START + timedelta(hours=1)
ID1, ID2 = uuid.UUID(int=1), uuid.UUID(int=2)


def appearance(id, camera, seconds, created=None):
    at = START + timedelta(seconds=seconds)
    return Row(id=id, pipeline_id=camera, start_time=at, created_at=created or at)


def test_ties_break_both_sides_and_duplicates_are_stable():
    rows = [appearance(1,'A',0), appearance(2,'B',10), appearance(3,'C',10),
            appearance(4,'D',20), appearance(5,'E',30), appearance(6,'E',30)]
    for ordered in (rows, list(reversed(rows))):
        history = camera_sessions(ordered)
        assert len(history) == 1
        assert history[0]['cameras'] == ['D','E']
        assert history[0]['appearance_ids'] == [4,5]


def test_examples_separate_future_target_and_reject_late_inputs():
    examples = observed_camera_examples([appearance(1,'A',0), appearance(2,'B',10), appearance(3,'C',20)])
    assert len(examples) == 2
    for row in examples:
        assert row['target_appearance_id'] not in row['input_appearance_ids']
        assert row['target_time'] > row['anchor_time']
    assert observed_camera_examples([appearance(1,'A',0,START+timedelta(seconds=20)),appearance(2,'B',10)]) == []
    assert observed_camera_examples([appearance(1,'A',0),appearance(2,'B',3*3600)]) == []


def test_prediction_passes_historical_anchor_in_utc():
    predictor = TrajectoryPredictor()
    predictor._get_historical_trajectories = AsyncMock(return_value=[])
    as_of = END.replace(tzinfo=timezone(timedelta(hours=3)))
    result = run(predictor.predict_with_evidence(None,str(ID1),'A',as_of))
    assert predictor._get_historical_trajectories.await_args.kwargs['as_of'] == END-timedelta(hours=3)
    assert result['predictions'] == []


@pytest.mark.parametrize('kwargs', [dict(kind='speed'),dict(pipeline_ids=[]),dict(limit=0),
    dict(window_seconds=0),dict(end=START),dict(end=START+timedelta(days=8))])
def test_scope_rejects_unbounded_or_unsupported_requests(kwargs):
    args=dict(kind='sightings',start=START,end=END,pipeline_ids=['A'])
    args.update(kwargs)
    with pytest.raises(ValueError): analytics_scope(**args)


def test_inspection_notebook_executes_actual_rows_and_rejects_false_direction():
    payload={'metadata':dict(kind='next_camera',grain='transition',captured_at='2026-09-20',
        returned_rows=1,read_only=True,truncated=False,unavailable={'speed':'missing positions'}),
        'items':[dict(anchor_time='2026-09-20T00:00:00',target_time='2026-09-20T00:00:10',
            target_appearance_id=2,input_appearance_ids=[1],horizon_seconds=10)]}
    namespace={}
    for cell in build_analytics_notebook(payload)['cells']:
        exec(''.join(cell['source']),namespace)
    assert namespace['dataset'][0]['target_appearance_id'] == 2
    payload['items'][0]['target_appearance_id']=1
    with pytest.raises(AssertionError):
        namespace={}
        for cell in build_analytics_notebook(payload)['cells']:
            exec(''.join(cell['source']),namespace)


def test_artifact_checksum_verified_before_preview(tmp_path):
    import pyarrow as pa
    import pyarrow.parquet as pq
    path=tmp_path/'data.parquet'
    pq.write_table(pa.Table.from_pylist([dict(entity_id='person',as_of='2026-09-20',features_json='{"count":1}')]),path)
    row=Row(storage_path=str(path),id=ID1,version=1,definition_name='test',checksum='logical',
        parquet_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),quality_report={},missing_value_report={})
    assert explore_dataset(row,tmp_path)['integrity_status']=='verified'
    row.parquet_sha256='0'*64
    with pytest.raises(ValueError,match='checksum'): explore_dataset(row,tmp_path)


@pytest.fixture
def database():
    if engine is None: pytest.skip('Disposable PostgreSQL required')
    async def prepare():
        async with engine.begin() as db:
            await db.execute(text('CREATE EXTENSION IF NOT EXISTS vector'))
            await db.run_sync(Base.metadata.create_all)
            await db.execute(text('TRUNCATE pipelines, identities, detections, threat_assessments, ml_labels CASCADE'))
        async with sessions() as db:
            db.add_all([Pipeline(pipeline_id=c,latitude=0,longitude=i*.0001) for i,c in enumerate(('A','B','C'))])
            db.add_all([Identity(id=id,type=IdentityType.UNKNOWN) for id in (ID1,ID2)])
            await db.flush()
            d=Detection(pipeline_id='A',timestamp=START); db.add(d); await db.flush()
            db.add_all([Face(detection_id=d.id,name='test',similarity=.8,identity_id=id,
                            bbox_x1=10,bbox_y1=20,bbox_x2=30,bbox_y2=50) for id in (ID1,ID2)])
            for id,person,camera,seconds in [(1,ID1,'A',0),(2,ID2,'A',1),(3,ID1,'B',10),
                    (4,ID2,'A',-1),(5,ID2,'A',3600),(6,ID2,'C',1)]:
                at=START+timedelta(seconds=seconds)
                db.add(A(id=id,identity_id=person,pipeline_id=camera,start_time=at,created_at=at))
            t=T(id=uuid.UUID(int=100),subject_type='identity',subject_id=str(ID1),person_id=ID1,
                pipeline_id='A',source_timestamp=START,total_risk_score=77,confidence=.6,
                severity='high',model_version='rules',idempotency_key='assessment-fixture')
            db.add(t);await db.flush()
            db.add(MLLabel(assessment_id=t.id,subject_id=str(ID1),label='negative',label_kind='manual',
                label_definition_version='test-v1',source='analyst_review',event_time=START,
                review_status='reviewed',reviewed_by='fixture-reviewer',created_by='fixture-reviewer',
                idempotency_key='label-fixture'))
            await db.commit()
    run(prepare())
    settings.MULTI_CAMERA_CO_APPEARANCE_ENABLED=False
    settings.MULTI_CAMERA_MIN_CO_APPEARANCES=1
    settings.RELATED_IDENTITY_TIME_WINDOW_MINUTES=1
    settings.RELATED_IDENTITY_MIN_CO_APPEARANCES=1
    settings.ACTIVITY_CORRELATION_ENABLED=False


async def inspect(kind, **kwargs):
    args=dict(kind=kind,start=START,end=END,pipeline_ids=['A','B'])
    args.update(kwargs)
    async with sessions() as db:
        await db.execute(text('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY'))
        result=await explore_existing_data(db,**args)
        assert not db.new and not db.dirty and not db.deleted
        return result


def test_camera_windows_no_fanout_half_open_and_scoped(database):
    result=run(inspect('camera_windows'))
    assert len(result['items'])==2
    a=result['items'][0]
    assert a['counts']==dict(detection_events=1,recognition_results=2,saved_sightings=2,
                            distinct_observed_identities=2,alert_triggers=0,assessments=1)
    assert result['items'][1]['counts']['saved_sightings']==1
    assert result['metadata']['unavailable']['occupancy']
    partial=run(inspect('camera_windows',limit=1))
    assert partial['metadata']['truncated'] is True
    assert partial['items'][0]['counts']==a['counts']


def test_all_read_projections_work_and_do_not_issue_mutations(database):
    statements=[]
    def record(conn,cursor,statement,params,context,many): statements.append(statement.strip().split()[0].upper())
    event.listen(engine.sync_engine,'before_cursor_execute',record)
    try:
        for kind in ('observations','sightings','events','reviews','cached_edges','next_camera'):
            result=run(inspect(kind,identity_id=str(ID1)))
            assert result['metadata']['read_only']
        assert set(statements) <= {'SET','SHOW','SELECT'}
    finally: event.remove(engine.sync_engine,'before_cursor_execute',record)
    review=run(inspect('reviews'))['items'][0]
    assert review['total_risk_score']==77 and review['prediction'] is None
    assert review['labels'][0]['label']=='negative' and review['labels'][0]['reviewed_by']=='fixture-reviewer'
    observation=run(inspect('observations'))['items'][0]
    assert observation['roi_width']==20 and observation['frame_dimensions_available'] is False


def test_pairs_share_source_evidence_and_exclude_out_of_period_rows(database):
    matches=run(inspect('co_occurrences',identity_id=str(ID1)))
    assert [r['evidence_key'] for r in matches['items']]==[[1,2]]
    edges=run(inspect('period_edges',identity_id=str(ID1)))
    assert edges['items'][0]['source_pair_count']==len(matches['items'])
    assert edges['items'][0]['source_appearance_pairs']==[[1,2]]
    assert edges['items'][0]['anchor_appearance_count']==2


def test_historical_prediction_excludes_future_and_late_arrivals(database):
    async def check():
        async with sessions() as db:
            db.add(A(id=7,identity_id=ID1,pipeline_id='C',start_time=START+timedelta(seconds=5),created_at=END))
            await db.commit()
        async with sessions() as db:
            history=await TrajectoryPredictor()._get_historical_trajectories(db,str(ID1),as_of=START+timedelta(seconds=9))
            assert history==[]
            history=await TrajectoryPredictor()._get_historical_trajectories(db,str(ID1),as_of=START+timedelta(seconds=11))
            assert history[0]['appearance_ids']==[1,3]
    run(check())


def test_empty_scope_is_not_zero_occupancy_and_readwrite_session_rejected(database):
    assert run(inspect('camera_windows',pipeline_ids=['C'],start=START+timedelta(minutes=1)))['metadata']['status']=='no_saved_observations'
    async def check():
        async with sessions() as db:
            with pytest.raises(ValueError,match='READ ONLY'):
                await explore_existing_data(db,kind='sightings',start=START,end=END,pipeline_ids=['A'])
    run(check())


def test_cross_camera_policy_preserves_camera_scope_and_source_counts(database):
    settings.MULTI_CAMERA_CO_APPEARANCE_ENABLED=True
    matches=run(inspect('co_occurrences',identity_id=str(ID1)))
    assert sorted(r['evidence_key'] for r in matches['items'])==[[1,2],[2,3]]
    assert {r['classification'] for r in matches['items']}=={'same_camera','cross_camera'}
    assert matches['metadata']['threshold_provenance']
    edge=run(inspect('period_edges',identity_id=str(ID1)))['items'][0]
    assert edge['source_pair_count']==2 and edge['cross_camera_count']==1


def test_unknown_camera_is_refused_and_source_limits_are_explicit(database):
    with pytest.raises(ValueError,match='do not exist'): run(inspect('sightings',pipeline_ids=['missing']))
    result=run(inspect('sightings',limit=1))
    assert result['metadata']['truncated'] and result['metadata']['status']=='partial'


def test_map_helper_cannot_manufacture_zones(monkeypatch):
    import sys
    from types import ModuleType
    from test_hardening import endpoint
    watchlists=ModuleType('backend.core.watchlist_service')
    watchlists.watchlist_service=Row(get_identity_watchlists=AsyncMock(return_value=[]))
    monkeypatch.setitem(sys.modules,'backend.core.watchlist_service',watchlists)
    query=AsyncMock(side_effect=AssertionError('Map must not query camera names to synthesize zones'))
    result=run(endpoint('_security_inputs')(Row(execute=query),str(ID1),[{'movements':[{'pipeline_id':'restricted-entrance'}]}]))
    assert result==([],None)
    query.assert_not_awaited()


def test_api_serializes_real_uuid_rows_and_downloads_notebook(database):
    import ast
    from fastapi import HTTPException
    from fastapi.responses import JSONResponse
    from backend.utils.time_utils import iso_utc
    from conftest import ROOT
    tree=ast.parse((ROOT/'backend/routes/ml_ops.py').read_text())
    node=next(n for n in tree.body if isinstance(n,ast.AsyncFunctionDef) and n.name=='existing_analytics')
    # Keep authorization policy under test before stripping FastAPI wrappers.
    defaults=' '.join(ast.unparse(n) for n in node.args.defaults)
    assert 'Depends(ML_MANAGE)' in defaults and 'rate_limited' in defaults
    node.decorator_list=[];node.args.defaults=[ast.Constant(None)]*len(node.args.args)
    namespace={'datetime':datetime,'JSONResponse':JSONResponse,'iso_utc':iso_utc,
               '_error':lambda status,code,msg:HTTPException(status,detail=msg),
               '_safe_500':lambda label,exc:exc}
    compiled=ast.Module(body=[ast.ImportFrom(module='__future__',names=[ast.alias(name='annotations')],level=0),node],type_ignores=[])
    exec(compile(ast.fix_missing_locations(compiled),'actual-analytics-endpoint','exec'),namespace)
    args=dict(kind='sightings',start=START,end=END,pipeline_ids=['A','B'],identity_id=ID1,
              limit=500,window_seconds=10,notebook=False)
    payload=json.loads(run(namespace['existing_analytics'](**args)).body)
    assert payload['items'][0]['identity_id']==str(ID1)
    assert payload['items'][0]['start_time'].endswith('Z')
    args['notebook']=True
    notebook=json.loads(run(namespace['existing_analytics'](**args)).body)
    assert len(notebook['cells'])==3
    assert notebook['metadata']['vas_debug_scope']=='existing_database_read_only'

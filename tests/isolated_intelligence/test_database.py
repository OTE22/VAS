"""Exercises the real queue/store against a disposable PostgreSQL instance."""
import asyncio
from datetime import datetime, timedelta
from unittest.mock import AsyncMock
import pytest
from sqlalchemy import text, select, update
from conftest import engine, session, settings
from db_models import Base, BackgroundTaskHistory, LearnedThreshold
from backend.ml.job_service import enqueue_ml_job, MLJobConflict
from backend.core.task_history import task_history_manager
from backend.ml.threshold_learning import persist_threshold_candidates
from backend.core.threshold_store import threshold_store, SIGNAL_TIME_WINDOW

pytestmark=pytest.mark.skipif(engine is None, reason='Disposable PostgreSQL not supplied')
run=asyncio.run

@pytest.fixture(scope='module',autouse=True)
def schema():
    if engine is None:
        return
    async def setup():
        async with engine.begin() as db:
            await db.execute(text('CREATE EXTENSION IF NOT EXISTS vector'))
            await db.run_sync(Base.metadata.create_all)
    run(setup())

@pytest.fixture(autouse=True)
def clean():
    if engine is None:
        return
    async def clear():
        async with engine.begin() as db:
            await db.execute(text('TRUNCATE background_task_history, learned_thresholds CASCADE'))
        threshold_store.invalidate()
    run(clear())


def test_queue_survives_session_closure_and_only_one_worker_claims():
    async def check():
        async with session() as db:
            job=await enqueue_ml_job(db,kind='threshold',payload={'pipeline_ids':['A','B']},description='isolated')
        claims=await asyncio.gather(*[task_history_manager.claim_next_queued_job(queue_name='ml',lease_owner=owner,lease_seconds=60) for owner in ('one','two')])
        assert sum(claim is not None for claim in claims)==1
        record=await task_history_manager.get_queued_job_payload(job['job_id'],queue_name='ml')
        assert record['status']=='running' and record['payload']['pipeline_ids']==['A','B']
    run(check())


def test_concurrent_submissions_have_database_single_flight():
    async def submit():
        try:
            async with session() as db:
                return await enqueue_ml_job(db,kind='threshold',payload={},description='isolated')
        except MLJobConflict:
            return None
    async def check():
        jobs=await asyncio.gather(submit(),submit())
        assert sum(j is not None for j in jobs)==1
    run(check())


def test_cancelled_scheduled_job_cannot_be_claimed():
    async def check():
        async with session() as db:
            job=await enqueue_ml_job(db,kind='threshold',payload={},description='isolated')
        ok,outcome=await task_history_manager.request_cancel(job['task_id'])
        assert ok and outcome=='cancelled'
        assert await task_history_manager.claim_next_queued_job(queue_name='ml',lease_owner='worker',lease_seconds=60) is None
    run(check())


def test_crashed_worker_lease_is_failed_not_completed():
    async def check():
        async with session() as db:
            job=await enqueue_ml_job(db,kind='threshold',payload={},description='isolated')
        await task_history_manager.claim_next_queued_job(queue_name='ml',lease_owner='worker',lease_seconds=60)
        async with session() as db:
            await db.execute(update(BackgroundTaskHistory).where(BackgroundTaskHistory.job_id==job['job_id']).values(lease_expires_at=datetime.utcnow()-timedelta(seconds=1)))
        assert await task_history_manager.fail_expired_queue_leases(queue_name='ml')==1
        record=await task_history_manager.get_task_by_job_id(job['job_id'])
        assert record['status']=='failed' and record['error_code']=='WORKER_LEASE_EXPIRED'
    run(check())


def learned():
    return {('A','B'):dict(optimal_time_window_minutes=5,optimal_distance_meters=120,sample_count=12)}


def test_candidates_are_persisted_but_not_consumed_until_activation():
    async def check():
        async with session() as db:
            assert await persist_threshold_candidates(db,learned())==4
        async with session() as db:
            resolved=await threshold_store.resolve(db,SIGNAL_TIME_WINDOW,static_default=10)
            assert resolved.value==10 and resolved.source=='static_default'
            row=(await db.execute(select(LearnedThreshold).where(LearnedThreshold.scope_type=='global',LearnedThreshold.signal_name==SIGNAL_TIME_WINDOW))).scalar_one()
            await threshold_store.activate(db,str(row.id),activated_by='isolated',min_samples=10)
        async with session() as db:
            resolved=await threshold_store.resolve(db,SIGNAL_TIME_WINDOW,static_default=10)
            assert resolved.value==5 and resolved.source=='global'
    run(check())


def test_candidate_transaction_rolls_back_on_write_failure(monkeypatch):
    original=threshold_store.record_candidate
    calls=0
    async def fail_on_second(*args,**kwargs):
        nonlocal calls
        calls+=1
        if calls==2:
            raise RuntimeError('injected write failure')
        return await original(*args,**kwargs)
    monkeypatch.setattr(threshold_store,'record_candidate',fail_on_second)
    async def check():
        with pytest.raises(RuntimeError,match='injected'):
            async with session() as db:
                await persist_threshold_candidates(db,learned())
        async with session() as db:
            assert list((await db.execute(select(LearnedThreshold))).scalars())==[]
    run(check())


def test_concurrent_activation_leaves_only_one_active_value():
    async def check():
        async with session() as db:
            ids=[]
            for value in (4,6):
                row=await threshold_store.record_candidate(db,scope_type='global',scope_id='',signal_name=SIGNAL_TIME_WINDOW,value=value,sample_count=12)
                ids.append(str(row.id))
        async def activate(identifier):
            async with session() as db:
                await threshold_store.activate(db,identifier,activated_by='isolated',min_samples=10)
        await asyncio.gather(*(activate(identifier) for identifier in ids))
        async with session() as db:
            rows=list((await db.execute(select(LearnedThreshold).where(LearnedThreshold.status=='active'))).scalars())
            assert len(rows)==1
    run(check())


def test_worker_dispatch_marks_persistence_failure_failed(monkeypatch):
    import sys
    from types import ModuleType
    from backend.ml import worker, threshold_learning
    runtime=ModuleType('backend.core.runtime_settings'); runtime.hydrate_from_db=AsyncMock()
    monkeypatch.setitem(sys.modules,'backend.core.runtime_settings',runtime)
    monkeypatch.setattr(threshold_learning,'run_threshold_learning',AsyncMock(side_effect=RuntimeError('injected write failure')))
    async def check():
        async with session() as db:
            job=await enqueue_ml_job(db,kind='threshold',payload={'pipeline_ids':['A','B']},description='isolated')
        await task_history_manager.claim_next_queued_job(queue_name='ml',lease_owner='worker',lease_seconds=60)
        assert await worker.execute_job(job['job_id'])==2
        record=await task_history_manager.get_task_by_job_id(job['job_id'])
        assert record['status']=='failed' and record['success'] is False
    run(check())


def test_worker_dispatch_persists_empty_evidence_result(monkeypatch):
    import sys
    from types import ModuleType
    from backend.ml import worker
    runtime=ModuleType('backend.core.runtime_settings'); runtime.hydrate_from_db=AsyncMock()
    monkeypatch.setitem(sys.modules,'backend.core.runtime_settings',runtime)
    async def check():
        async with session() as db:
            job=await enqueue_ml_job(db,kind='threshold',payload={},description='isolated')
        await task_history_manager.claim_next_queued_job(queue_name='ml',lease_owner='worker',lease_seconds=60)
        assert await worker.execute_job(job['job_id'])==0
        record=await task_history_manager.get_task_by_job_id(job['job_id'])
        assert record['status']=='completed'
        assert record['result']['outcome']=='insufficient_evidence'
    run(check())

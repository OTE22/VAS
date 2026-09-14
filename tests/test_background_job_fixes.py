"""Failure injection uses temporary files/mocks; the live database is not modified."""
import asyncio
import importlib
import os
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from conftest import run_on_shared_loop as run
from config import settings


def test_first_run_deadline_respects_startup_delay():
    from backend.core import service_supervisor as sup
    sup.reset_registry_for_tests()
    async def exercise():
        entered = asyncio.Event()
        async def sleep(delay):
            entered.set()
            await asyncio.Event().wait()
        task = asyncio.create_task(sup.supervised_loop('delayed', 86400, AsyncMock(),
            initial_delay=25200, first_run_timeout=300, now=lambda: 10, sleep=sleep))
        await entered.wait()
        assert 'delayed' not in sup.stale_services(_now=lambda: 25509)
        assert 'delayed' in sup.stale_services(_now=lambda: 25511)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    run(exercise())
    sup.reset_registry_for_tests()


def test_failed_return_marks_supervisor_degraded():
    from backend.core import service_supervisor as sup
    sup.reset_registry_for_tests()
    async def exercise():
        async def work():
            return {'status': 'failed', 'failures': ['partial cleanup']}
        async def sleep(delay):
            assert sup.get_service_health()['partial']['consecutive_failures'] == 1
            raise asyncio.CancelledError
        with pytest.raises(asyncio.CancelledError):
            await sup.supervised_loop('partial', 60, work, sleep=sleep)
    run(exercise())
    sup.reset_registry_for_tests()


@pytest.fixture
def spool(tmp_path, monkeypatch):
    from backend.core import detection_spool
    monkeypatch.setattr(detection_spool, 'root', lambda: tmp_path / 'queue')
    return detection_spool


def frame():
    return {'pipeline_id': 'qa-retry', 'detection': {
        'pipeline_id': 'qa-retry', 'uuid': str(uuid.uuid4()), 'timestamp': datetime.utcnow()},
        'faces': [{'_embedding_id': 17, '_secondary_embedding_ids': [18]}]}


def test_spool_roundtrip_and_protected_evidence(spool):
    data = frame()
    spool.enqueue(data)
    spool.enqueue(data)
    rows = spool.pending()
    assert len(rows) == 1 and rows[0][1] == data
    assert spool.protected_embedding_ids() == {17, 18}
    spool.acknowledge(rows[0][0])
    assert spool.pending() == []


def test_spool_atomic_write_failure_leaves_no_partial_work(spool, monkeypatch):
    def fail(*args):
        raise OSError('disk full')
    monkeypatch.setattr(os, 'replace', fail)
    with pytest.raises(OSError):
        spool.enqueue(frame())
    assert list(spool.root().iterdir()) == []


def test_spool_quarantines_invalid_evidence(spool):
    spool.enqueue(frame())
    path, _ = spool.pending()[0]
    spool.quarantine(path, 'invalid link')
    assert spool.pending() == []
    assert path.with_suffix('.failed.json').exists()


def test_writer_keeps_work_when_circuit_open(spool, monkeypatch):
    bw = importlib.import_module('backend.core.batch_writer')
    monkeypatch.setattr(bw.db_circuit_breaker, 'can_execute', AsyncMock(return_value=False))
    async def exercise():
        writer = bw.BatchDatabaseWriter()
        await writer.add_detection(frame())
        with pytest.raises(RuntimeError, match='retained on disk'):
            await writer.flush()
        assert len(spool.pending()) == 1
    run(exercise())


def test_writer_recovers_after_database_failure(spool, monkeypatch):
    bw = importlib.import_module('backend.core.batch_writer')
    calls = []
    @asynccontextmanager
    async def session():
        yield SimpleNamespace(execute=AsyncMock())
    monkeypatch.setattr(bw.db_manager, 'get_session', session)
    monkeypatch.setattr(bw.db_circuit_breaker, 'can_execute', AsyncMock(return_value=True))
    monkeypatch.setattr(bw.db_circuit_breaker, 'call_failed', AsyncMock())
    monkeypatch.setattr(bw.db_circuit_breaker, 'call_succeeded', AsyncMock())
    async def persist(db, *, detection_data):
        calls.append(detection_data['detection']['uuid'])
        if len(calls) == 1:
            raise OSError('connection lost')
        return SimpleNamespace(bundles=[])
    monkeypatch.setattr(bw, 'persist_detection', persist)
    async def exercise():
        writer = bw.BatchDatabaseWriter()
        await writer.add_detection(frame())
        with pytest.raises(RuntimeError):
            await writer.flush()
        # Simulate a process restart: new writer reads the existing spool.
        await bw.BatchDatabaseWriter().flush()
        assert spool.pending() == [] and calls[0] == calls[1]
    run(exercise())


def test_idempotent_replay_has_no_evidence_or_alert_side_effects(monkeypatch):
    from backend.core.detection_evidence import persist_detection
    db = SimpleNamespace(execute=AsyncMock(side_effect=[None, None,
        SimpleNamespace(scalar_one_or_none=lambda: 123)]), add=AsyncMock(), flush=AsyncMock())
    result = run(persist_detection(db, detection_data=frame()))
    assert result.detection_id == 123 and result.bundles == []
    db.add.assert_not_called()
    db.flush.assert_not_called()


def old_file(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('old evidence', encoding='utf-8')
    os.utime(path, (time.time() - 90 * 86400,) * 2)


def test_log_preview_and_cleanup_preserve_active_audit_and_new_files(tmp_path, monkeypatch):
    from backend.core import log_retention as lr
    import utils.logging as ul
    monkeypatch.setattr(ul, 'active_log_path', lambda: str(tmp_path / 'app.log'))
    monkeypatch.setattr(lr, '_open_paths', lambda: ({tmp_path / 'error.log'}, True))
    for name in ('app.log', 'app.log.1', 'app.log.9', 'access.log', 'error.log',
                 'audit/report.json', 'audit/model.parquet', 'audit/run.log', 'smoke/old.png'):
        old_file(tmp_path / name)
    (tmp_path / 'app.log.2').write_text('recent')
    preview = lr.clean(tmp_path, 48, dry_run=True)
    assert preview['candidate_files'] == 5 and preview['deleted_files'] == 0
    result = lr.clean(tmp_path, 48)
    assert result['deleted_files'] == 5 and not result['failures']
    for name in ('app.log', 'error.log', 'audit/report.json', 'audit/model.parquet', 'app.log.2'):
        assert (tmp_path / name).exists()


def test_log_permission_error_is_reported(tmp_path, monkeypatch):
    from backend.core import log_retention as lr
    old_file(tmp_path / 'app.log.1')
    monkeypatch.setattr(lr, '_open_paths', lambda: (set(), True))
    def refuse(self, *args, **kwargs):
        raise PermissionError('denied')
    monkeypatch.setattr(Path, 'unlink', refuse)
    assert lr.clean(tmp_path, 48)['failures']


def test_tracker_cleanup_preserves_recent_faces():
    from backend.core.face_tracker import FaceTracker
    import numpy as np
    tracker = FaceTracker(window_seconds=60)
    tracker.tracked_faces['qa']['old'] = (np.zeros(512), 'old', time.time() - 61)
    tracker.tracked_faces['qa']['new'] = (np.zeros(512), 'new', time.time())
    tracker._memory_estimate_bytes = tracker._estimate_embedding_size(512) * 2
    run(tracker._cleanup_once())
    assert set(tracker.tracked_faces['qa']) == {'new'}


def test_identity_cleanup_failure_propagates_and_records_failure(monkeypatch):
    from backend.core.identity_retention import IdentityRetentionManager
    from backend.core.task_history import task_history_manager
    manager = IdentityRetentionManager()
    monkeypatch.setattr(manager, '_cleanup_old_snapshots', AsyncMock(side_effect=OSError('denied')))
    record = AsyncMock()
    monkeypatch.setattr(task_history_manager, 'record_task_completed', record)
    with pytest.raises(OSError):
        run(manager.run_cleanup())
    assert record.call_args.kwargs['success'] is False


def test_real_database_replay_does_not_duplicate_detection_or_counter():
    """Use an outer rollback: no QA pipeline/detection survives this test."""
    from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
    from sqlalchemy import select, func
    from db_models import Pipeline, Detection
    from backend.core.detection_evidence import persist_detection
    async def exercise():
        engine = create_async_engine(settings.DATABASE_URL)
        try:
            async with engine.connect() as connection:
                transaction = await connection.begin()
                try:
                    async with AsyncSession(bind=connection, join_transaction_mode='create_savepoint', expire_on_commit=False) as db:
                        pipeline = Pipeline(pipeline_id='retry-qa-' + uuid.uuid4().hex, total_detections=0)
                        db.add(pipeline)
                        await db.flush()
                        data = frame()
                        data['pipeline_id'] = data['detection']['pipeline_id'] = pipeline.pipeline_id
                        data['faces'] = []
                        first = await persist_detection(db, detection_data=data)
                        await db.commit()
                        replay = await persist_detection(db, detection_data=data)
                        await db.commit()
                        assert replay.detection_id == first.detection_id
                        assert (await db.execute(select(func.count()).select_from(Detection).where(
                            Detection.uuid == data['detection']['uuid']))).scalar_one() == 1
                        await db.refresh(pipeline)
                        assert pipeline.total_detections == 1
                finally:
                    await transaction.rollback()
        finally:
            await engine.dispose()
    asyncio.run(exercise())


def test_expiration_sweeps_keep_future_and_non_expiring_items():
    from datetime import timedelta
    from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
    import db_models as m
    from backend.core.live_alert_service import live_alert_service
    from backend.core.watchlist_service import watchlist_service
    async def exercise():
        engine = create_async_engine(settings.DATABASE_URL)
        try:
            async with engine.connect() as connection:
                transaction = await connection.begin()
                try:
                    async with AsyncSession(bind=connection, join_transaction_mode='create_savepoint', expire_on_commit=False) as db:
                        person = m.Identity(type=m.IdentityType.UNKNOWN, status=m.IdentityStatus.ACTIVE)
                        db.add(person)
                        await db.flush()
                        entries, alerts = [], []
                        for expiry in (datetime.utcnow() - timedelta(days=1), datetime.utcnow() + timedelta(days=1), None):
                            watchlist = m.Watchlist(name='expiry-qa-' + uuid.uuid4().hex)
                            db.add(watchlist)
                            await db.flush()
                            entries.append(m.WatchlistEntry(watchlist_id=watchlist.id, identity_id=person.id, expires_at=expiry))
                            alerts.append(m.LiveSearchAlert(name='Expiry QA', identity_id=person.id,
                                expiration_type=m.LiveAlertExpirationType.DATE if expiry else m.LiveAlertExpirationType.NEVER,
                                expiration_date=expiry))
                        db.add_all(entries + alerts)
                        await db.flush()
                        assert await watchlist_service.cleanup_expired_entries(db) >= 1
                        assert await live_alert_service.cleanup_expired_alerts(db) >= 1
                        for record in entries + alerts:
                            await db.refresh(record)
                        assert [e.is_active for e in entries] == [False, True, True]
                        assert [a.status for a in alerts] == [m.LiveAlertStatus.EXPIRED, m.LiveAlertStatus.ACTIVE, m.LiveAlertStatus.ACTIVE]
                finally:
                    await transaction.rollback()
        finally:
            await engine.dispose()
    asyncio.run(exercise())


def test_maintenance_records_failed_expiry_job(monkeypatch):
    from backend.core.background_maintenance import BackgroundMaintenance
    from backend.core.task_history import task_history_manager
    from backend.core.live_alert_service import live_alert_service
    from db_connection import db_manager
    @asynccontextmanager
    async def session():
        yield object()
    monkeypatch.setattr(db_manager, 'get_session', session)
    monkeypatch.setattr(task_history_manager, 'record_task_started', AsyncMock())
    record = AsyncMock()
    monkeypatch.setattr(task_history_manager, 'record_task_completed', record)
    monkeypatch.setattr(live_alert_service, 'cleanup_expired_alerts', AsyncMock(side_effect=OSError('database unavailable')))
    with pytest.raises(OSError):
        run(BackgroundMaintenance().sweep('live_alert_cleanup'))
    assert record.call_args.args[2] is False


def test_cache_write_failure_does_not_break_optional_request_path():
    from backend.core.production_cache import ProductionCacheManager
    manager = ProductionCacheManager(redis_client=SimpleNamespace(setex=AsyncMock(side_effect=OSError('offline'))))
    run(manager._direct_set('qa', 'value', 10))
    with pytest.raises(OSError):
        run(manager._direct_set('qa', 'value', 10, strict=True))


def test_redis_listener_does_not_echo_own_worker_messages(monkeypatch):
    import json
    from backend.core import service_supervisor
    module = importlib.import_module('backend.core.websocket_manager')
    manager = module.WebSocketManager()
    event = {'message': {'type': 'qa'}, 'pipeline_id': 'camera', 'admin_only': True}
    manager.redis_pubsub = SimpleNamespace(get_message=AsyncMock(side_effect=[
        {'type': 'message', 'data': json.dumps({**event, 'worker_pid': module.PROCESS_ID})},
        {'type': 'message', 'data': json.dumps({**event, 'worker_pid': -1})}]))
    broadcast = AsyncMock()
    monkeypatch.setattr(manager, '_broadcast_local', broadcast)
    async def two_cycles(name, interval, work, **kwargs):
        await work()
        await work()
    monkeypatch.setattr(service_supervisor, 'supervised_loop', two_cycles)
    run(manager._redis_listener())
    broadcast.assert_awaited_once_with(event['message'], 'camera', True)

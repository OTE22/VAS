import asyncio
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from conftest import run_on_shared_loop as run


def test_state_does_not_confuse_waiting_stale_and_success():
    from backend.core.job_monitoring import service_rows
    rows = service_rows({
        'waiting': {'status': 'starting', 'next_run_at': 100},
        'stuck': {'status': 'starting'},
        'failed': {'status': 'degraded', 'last_error': 'disk full', 'consecutive_failures': 2},
        'idle_loop': {'status': 'running', 'last_success': 90},
    }, {'stuck'})
    by_name = {row['name']: row for row in rows}
    assert by_name['waiting']['state'] == 'waiting'
    assert by_name['waiting']['last_success_at'] is None
    assert by_name['stuck']['state'] == 'stale'
    assert by_name['failed']['failures'] == 2
    assert by_name['failed']['last_error'] == 'disk full'
    assert by_name['idle_loop']['state'] == 'healthy'


def test_monitoring_preserves_service_state_on_database_error(monkeypatch):
    from backend.core import job_monitoring as monitoring
    monkeypatch.setattr(monitoring, 'get_service_health', lambda: {'writer': {'status': 'running'}})
    monkeypatch.setattr(monitoring, 'stale_services', lambda: [])
    def broken():
        raise RuntimeError('private connection detail')
    monkeypatch.setattr(monitoring.db_manager, 'get_session', broken)
    result = run(monitoring.snapshot())
    assert result['services'][0]['state'] == 'healthy'
    assert result['history_available'] is False
    assert result['ml_worker']['status'] == 'unavailable'
    assert 'private connection detail' not in str(result)


def test_monitoring_admin_auth_and_timeout(monkeypatch):
    from backend.routes import logs
    from backend.core import job_monitoring
    app = FastAPI()
    app.include_router(logs.router)
    from db_connection import get_db
    app.dependency_overrides[get_db] = lambda: None
    client = TestClient(app)
    assert client.get('/api/logs/background-status').status_code in (401, 403)
    route = next(route for route in logs.router.routes if route.path == '/api/logs/background-status')
    app.dependency_overrides[route.dependant.dependencies[0].call] = lambda: object()
    monkeypatch.setattr(job_monitoring, 'snapshot', AsyncMock(return_value={'services': []}))
    response = client.get('/api/logs/background-status')
    assert response.status_code == 200
    assert 'no-store' in response.headers['cache-control']
    monkeypatch.setattr(job_monitoring, 'snapshot', AsyncMock(side_effect=asyncio.TimeoutError))
    assert client.get('/api/logs/background-status').status_code == 503


def test_ml_sources_are_separate_and_scan_caps_are_reported(tmp_path, monkeypatch):
    from backend.routes import logs
    from config import settings
    import utils.logging as logging_config
    monkeypatch.setattr(logging_config, '_resolve_log_settings', lambda _: (str(tmp_path), 20, 20))
    monkeypatch.setattr(settings, 'LOG_API_MAX_SCAN_FILES', 1)
    line = '2026-09-14 19:00:00,000 | INFO     | pid=1 | MainThread | req=- | backend.ml.worker | worker ready\n'
    (tmp_path / 'ml-worker.log').write_text(line)
    (tmp_path / 'ml-worker.log.1').write_text(line)
    (tmp_path / 'app.log').write_text(line.replace('worker ready', 'api only'))
    result = logs._collect(None, None, None, 100, 'ml-worker')
    assert result['truncated'] is True
    assert len(result['entries']) == 1
    assert result['entries'][0].message == 'worker ready'
    with pytest.raises(KeyError):
        logging_config.source_log_path('../../secrets')

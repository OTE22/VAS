import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path, monkeypatch):
    from backend.routes import logs
    from db_connection import get_db
    import utils.logging as logging_config
    monkeypatch.setattr(logging_config, '_resolve_log_settings', lambda _: (str(tmp_path), 20, 20))
    monkeypatch.setattr(logs, 'active_log_path', lambda: str(tmp_path / 'app.log'))
    app = FastAPI()
    app.include_router(logs.router)
    app.dependency_overrides[get_db] = lambda: None
    for route in logs.router.routes:
        if route.path in ('/api/logs', '/api/logs/stats'):
            app.dependency_overrides[route.dependant.dependencies[0].call] = lambda: object()
    return TestClient(app)


@pytest.mark.parametrize('source', ['ml-job', 'migrations', 'image-processing'])
def test_optional_sources_return_explicit_absence_and_become_readable(client, tmp_path, source):
    import utils.logging as logging_config
    for endpoint in ('/api/logs', '/api/logs/stats'):
        response = client.get(endpoint, params={'source': source})
        assert response.status_code == 200
        assert response.json()['source_available'] is False
        assert response.json()['source_message']
        assert 'no-store' in response.headers['cache-control']
    path = tmp_path / logging_config.LOG_SOURCE_FILES[source]
    path.write_text('2026-09-14 19:00:00,000 | INFO     | pid=1 | MainThread | req=- | test | command started\n')
    data = client.get('/api/logs', params={'source': source}).json()
    assert data['source_available'] is True
    assert data['source_message'] is None
    assert data['logs'][0]['message'] == 'command started'


@pytest.mark.parametrize('source', ['application', 'ml-worker', 'server'])
def test_missing_required_sources_remain_errors(client, source):
    assert client.get('/api/logs', params={'source': source}).status_code == 503
    assert client.get('/api/logs/stats', params={'source': source}).status_code == 503


def test_unreadable_optional_source_remains_error(client, tmp_path, monkeypatch):
    from backend.routes import logs
    path = tmp_path / 'ml-job.log'
    path.write_text('exists')
    monkeypatch.setattr(logs.os, 'access', lambda *args: False)
    assert client.get('/api/logs?source=ml-job').status_code == 503


def test_missing_directory_remains_error(client, tmp_path, monkeypatch):
    from backend.routes import logs
    monkeypatch.setattr(logs, 'source_log_path', lambda _: str(tmp_path / 'missing' / 'ml-job.log'))
    assert client.get('/api/logs?source=ml-job').status_code == 503

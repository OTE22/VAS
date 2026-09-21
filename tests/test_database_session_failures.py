"""Final transaction errors must reach HTTP callers, with balanced counters."""
import asyncio
from unittest.mock import AsyncMock

import pytest
from fastapi import Depends, FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy.exc import InterfaceError

import db_connection


def manager_for(session):
    manager = db_connection.DatabaseManager()
    session.__aenter__.return_value = session
    session.__aexit__.return_value = False
    manager.session_maker = lambda: session
    return manager


def test_commit_failure_is_not_an_http_success(monkeypatch):
    session = AsyncMock()
    session.commit.side_effect = InterfaceError('COMMIT', {}, Exception('connection is closed'))
    manager = manager_for(session)
    monkeypatch.setattr(db_connection, 'db_manager', manager)
    app = FastAPI()

    @app.post('/save')
    async def save(db=Depends(db_connection.get_db)):
        await db.flush()
        return {'saved': True}

    with TestClient(app, raise_server_exceptions=False) as client:
        assert client.post('/save').status_code == 500
    session.rollback.assert_awaited_once()
    assert manager._stats['active_sessions'] == 0
    assert manager._stats['failed_sessions'] == 1


@pytest.mark.parametrize('failure', [None, HTTPException(400, 'invalid'), RuntimeError('failed'), asyncio.CancelledError()])
def test_session_counter_returns_to_zero(failure):
    session = AsyncMock()
    manager = manager_for(session)

    async def run():
        try:
            async with manager.get_session():
                assert manager._stats['active_sessions'] == 1
                if failure is not None:
                    raise failure
        except BaseException as exc:
            assert exc is failure
        else:
            assert failure is None
        assert manager._stats['active_sessions'] == 0
        assert session.commit.await_count == (failure is None)
        assert session.rollback.await_count == (failure is not None)
    asyncio.run(run())


def test_session_open_failure_balances_counter():
    session = AsyncMock()
    manager = manager_for(session)
    session.__aenter__.side_effect = RuntimeError('cannot open')

    async def run():
        with pytest.raises(RuntimeError, match='cannot open'):
            async with manager.get_session():
                pytest.fail('session should not open')
        assert manager._stats['active_sessions'] == 0
    asyncio.run(run())

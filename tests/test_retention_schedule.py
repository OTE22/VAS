import asyncio
from datetime import datetime, timedelta
from unittest.mock import AsyncMock

from conftest import run_on_shared_loop as run


def test_restart_resumes_remaining_interval_instead_of_running_immediately():
    from backend.core.data_retention import retention_startup_delay
    now = datetime(2026, 9, 14, 20, 30)
    last = now - timedelta(minutes=10)
    delay = retention_startup_delay(last, 24, now)
    assert delay == 24 * 3600 - 600 - 60
    assert now + timedelta(seconds=delay + 60) == last + timedelta(hours=24)
    # Another restart ten minutes later does not move the due time.
    later = now + timedelta(minutes=10)
    assert later + timedelta(seconds=retention_startup_delay(last, 24, later) + 60) == last + timedelta(hours=24)


def test_first_boot_and_overdue_runs_keep_startup_grace():
    from backend.core.data_retention import retention_startup_delay
    now = datetime(2026, 9, 14)
    assert retention_startup_delay(None, 24, now) == 60
    assert retention_startup_delay(now - timedelta(days=2), 24, now) == 60


def test_start_uses_restored_history_and_disables_schedule_jitter(monkeypatch):
    from backend.core.data_retention import DataRetentionManager
    from backend.core import service_supervisor
    from config import settings
    monkeypatch.setattr(settings, 'CLEANUP_INTERVAL_HOURS', 24)
    calls = []
    async def supervised(*args, **kwargs):
        calls.append(kwargs)
        await asyncio.Event().wait()
    monkeypatch.setattr(service_supervisor, 'supervised_loop', supervised)
    async def exercise():
        manager = DataRetentionManager()
        last = datetime.utcnow() - timedelta(minutes=10)
        monkeypatch.setattr(manager, '_last_successful_cleanup', AsyncMock(return_value=last))
        await manager.start()
        await asyncio.sleep(0)
        assert manager.last_run_at == last
        assert calls[0]['initial_delay'] > 23 * 3600
        assert calls[0]['jitter'] == 0
        await manager.stop()
    run(exercise())


def test_history_failure_does_not_trigger_early_deletion(monkeypatch):
    from backend.core.data_retention import DataRetentionManager
    from backend.core import service_supervisor
    from config import settings
    monkeypatch.setattr(settings, 'CLEANUP_INTERVAL_HOURS', 24)
    calls = []
    async def supervised(*args, **kwargs):
        calls.append(kwargs)
        await asyncio.Event().wait()
    monkeypatch.setattr(service_supervisor, 'supervised_loop', supervised)
    async def exercise():
        manager = DataRetentionManager()
        monkeypatch.setattr(manager, '_last_successful_cleanup', AsyncMock(side_effect=RuntimeError('offline')))
        await manager.start()
        await asyncio.sleep(0)
        assert calls[0]['initial_delay'] == 24 * 3600 - 60
        await manager.stop()
    run(exercise())

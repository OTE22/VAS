import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

from conftest import run_on_shared_loop as run
from backend.core import service_supervisor as sup


@pytest.mark.parametrize('task_type,default,interval', [
    ('log_cleanup', 600, 6 * 3600),
    ('identity_retention', 3600, 24 * 3600),
    ('identity_clustering', 7 * 3600, 24 * 3600),
])
def test_recent_completion_never_gets_capped_to_startup_delay(monkeypatch, task_type, default, interval):
    last = datetime.now(timezone.utc) - timedelta(minutes=10)
    monkeypatch.setattr(sup, 'last_successful_completion', AsyncMock(return_value=last))
    async def check():
        delay = await sup.durable_initial_delay(task_type, default, interval, notification_lead_seconds=60)
        assert interval - 662 < delay <= interval - 660
        assert delay > default
        # A second startup restores the same deadline, not another full interval.
        again = await sup.durable_initial_delay(task_type, default, interval, notification_lead_seconds=60)
        assert abs(again - delay) < 1
    run(check())


@pytest.mark.parametrize('stamp,expected', [
    (None, 600),
    (datetime(2020, 1, 1), 60),
])
def test_first_installation_and_overdue_grace(monkeypatch, stamp, expected):
    monkeypatch.setattr(sup, 'last_successful_completion', AsyncMock(return_value=stamp))
    assert run(sup.durable_initial_delay('log_cleanup', 600, 21600, notification_lead_seconds=60)) == expected


def test_history_failure_waits_full_interval(monkeypatch):
    monkeypatch.setattr(sup, 'last_successful_completion', AsyncMock(side_effect=RuntimeError('offline')))
    assert run(sup.durable_initial_delay('log_cleanup', 600, 21600, notification_lead_seconds=60)) == 21540


def test_timestamp_offset_is_converted_to_utc(monkeypatch):
    last = datetime.now(timezone(timedelta(hours=3))) - timedelta(minutes=10)
    monkeypatch.setattr(sup, 'last_successful_completion', AsyncMock(return_value=last.isoformat()))
    delay = run(sup.durable_initial_delay('log_cleanup', 600, 21600, notification_lead_seconds=60))
    assert 20938 < delay <= 20940


@pytest.mark.parametrize('module_name,class_name,task_type,stop_name', [
    ('log_cleanup', 'LogCleanupManager', 'log_cleanup', 'stop'),
    ('identity_retention', 'IdentityRetentionManager', 'identity_retention', 'stop'),
    ('identity_clustering', 'IdentityClusteringService', 'identity_clustering', 'stop'),
])
def test_managers_restore_schedule_and_keep_intervals_live(monkeypatch, module_name, class_name, task_type, stop_name):
    import importlib
    module = importlib.import_module('backend.core.' + module_name)
    manager = getattr(module, class_name)()
    calls = []
    delay = AsyncMock(return_value=12345)
    monkeypatch.setattr(sup, 'durable_initial_delay', delay)
    async def loop(*args, **kwargs):
        calls.append((args, kwargs))
        await asyncio.Event().wait()
    monkeypatch.setattr(sup, 'supervised_loop', loop)
    async def exercise():
        await manager.start()
        await asyncio.sleep(0)
        try:
            args, kwargs = calls[0]
            assert args[0] == task_type
            assert kwargs['initial_delay'] == 12345
            assert kwargs['jitter'] == 0
            assert delay.call_args.kwargs['notification_lead_seconds'] == 60
            if task_type != 'log_cleanup':
                assert callable(args[1])
                attr = '_cleanup_interval_hours_override' if task_type == 'identity_retention' else '_cluster_interval_hours_override'
                monkeypatch.setattr(manager, attr, 48)
                assert args[1]() == 48 * 3600 - 60
        finally:
            await getattr(manager, stop_name)()
    run(exercise())

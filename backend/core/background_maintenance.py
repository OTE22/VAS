"""Scheduled expiry sweeps and liveness checks for long-lived consumer tasks."""
import asyncio
import time

from config import settings
from db_connection import db_manager
from backend.core.service_supervisor import supervised_loop, get_service_health


class BackgroundMaintenance:
    def __init__(self):
        self.tasks = []

    async def start(self, consumers, expected_services):
        async def check_consumers():
            failed = []
            for name, getter in consumers.items():
                task = getter()
                if task is None or task.done():
                    failed.append(name)
            registered = get_service_health()
            failed.extend(name for name in expected_services
                          if name not in registered or registered[name]['status'] == 'stopped')
            if failed:
                raise RuntimeError('Missing/stopped background workers: ' + ', '.join(failed))

        self.tasks.append(asyncio.create_task(supervised_loop(
            'worker_liveness', 15, check_consumers, initial_delay=5, jitter=0)))
        for name, getter in consumers.items():
            async def probe(getter=getter):
                task = getter()
                if task is None or task.done():
                    raise RuntimeError('Consumer task stopped or failed to start')
            self.tasks.append(asyncio.create_task(supervised_loop(
                name + '_liveness', 15, probe, initial_delay=5, jitter=0)))
        for name in ('live_alert_cleanup', 'watchlist_cleanup', 'pending_enrollment_cleanup'):
            self.tasks.append(asyncio.create_task(supervised_loop(
                name, 300, lambda name=name: self.sweep(name), initial_delay=60,
                error_backoff_base=60), name=name))

    async def stop(self):
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        self.tasks.clear()

    async def sweep(self, name):
        from backend.core.task_history import task_history_manager
        started = time.monotonic()
        title = name.replace('_', ' ').title()
        # Expiration is lifecycle maintenance, including while matching is disabled.
        await task_history_manager.record_task_started(name, title)
        try:
            async with db_manager.get_session() as db:
                if name == 'live_alert_cleanup':
                    from backend.core.live_alert_service import live_alert_service
                    count = await live_alert_service.cleanup_expired_alerts(db)
                elif name == 'watchlist_cleanup':
                    from backend.core.watchlist_service import watchlist_service
                    count = await watchlist_service.cleanup_expired_entries(db)
                else:
                    from backend.core.enrollment_service import sweep_expired_pending
                    count = await sweep_expired_pending(db, strict=True)
        except Exception as exc:
            await task_history_manager.record_task_completed(name, title, False,
                duration_seconds=time.monotonic() - started, details={'error': str(exc)[:500]})
            raise
        await task_history_manager.record_task_completed(name, title, True,
            duration_seconds=time.monotonic() - started, details={'processed': count})


background_maintenance = BackgroundMaintenance()

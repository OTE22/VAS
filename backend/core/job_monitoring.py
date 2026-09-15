"""Read-only admin snapshot: process liveness and durable outcomes stay distinct."""
import logging
import asyncio
import re
from pathlib import Path
from datetime import datetime, timezone

from sqlalchemy import select, func

from config import settings
from db_connection import db_manager
from db_models import BackgroundTaskHistory
from backend.core.service_supervisor import get_service_health, stale_services
from backend.ml.job_service import ml_worker_health

logger = logging.getLogger(__name__)


def iso(epoch):
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat() if epoch is not None else None


def service_rows(services, stale):
    rows = []
    for name, value in sorted(services.items()):
        state = value['status']
        if name in stale:
            state = 'stale'
        elif state == 'starting' and not value.get('cycle_active'):
            state = 'waiting'
        elif state == 'running':
            state = 'healthy'
        activity = 'Executing cycle' if value.get('cycle_active') else 'Waiting for next cycle'
        if state == 'stopped':
            activity = 'Service stopped'
        rows.append({
            'name': name, 'state': state,
            'activity': activity,
            'last_success_at': iso(value.get('last_success')),
            'next_run_at': iso(value.get('next_run_at')),
            'failures': value.get('consecutive_failures', 0),
            'last_error': value.get('last_error'),
        })
    return rows


def log_inventory():
    root = Path(settings.LOG_DIR)
    files = []
    for directory, dirs, names in __import__('os').walk(root, followlinks=False):
        dirs[:] = [name for name in dirs if not (Path(directory) / name).is_symlink()]
        for name in names:
            if not re.search(r'\.log(?:\.?[0-9]+)?(?:\.gz)?$', name):
                continue
            path = Path(directory) / name
            if path.is_symlink():
                continue
            try:
                stat = path.stat()
                files.append({'name': str(path.relative_to(root)), 'bytes': stat.st_size})
            except OSError:
                continue
    return {'directory': str(root), 'files': sorted(files, key=lambda item: item['name']),
            'total_bytes': sum(item['bytes'] for item in files)}


async def snapshot():
    services = service_rows(get_service_health(), set(stale_services()))
    result = {'checked_at': datetime.now(timezone.utc).isoformat(), 'services': services,
              'history_available': False, 'ml_worker': {'status': 'unavailable'},
              'errors': [], 'last_log_cleanup': None}
    result['log_inventory'] = await asyncio.to_thread(log_inventory)
    # Preserve process health when the database cannot be queried.
    try:
        async with db_manager.get_session() as db:
            result['ml_worker'] = await ml_worker_health(
                db, lease_seconds=int(settings.ML_JOB_LEASE_SECONDS))
            rows = (await db.execute(select(
                BackgroundTaskHistory.task_type,
                func.max(BackgroundTaskHistory.completed_at).label('completed_at')
            ).where(BackgroundTaskHistory.status == 'completed').group_by(
                BackgroundTaskHistory.task_type))).all()
            completed = {row.task_type: row.completed_at.isoformat() + 'Z' for row in rows
                         if row.completed_at is not None}
            for service in services:
                service['last_completed_at'] = completed.get(service['name'])
            cleanup = (await db.execute(select(BackgroundTaskHistory).where(
                BackgroundTaskHistory.task_type == 'log_cleanup',
                BackgroundTaskHistory.status.in_(['completed', 'failed'])
            ).order_by(BackgroundTaskHistory.id.desc()).limit(1))).scalar_one_or_none()
            if cleanup is not None:
                result['last_log_cleanup'] = {'status': cleanup.status,
                    'completed_at': cleanup.completed_at.isoformat() + 'Z' if cleanup.completed_at else None,
                    'result': cleanup.result or cleanup.details or {}}
            result['history_available'] = True
    except Exception:
        logger.exception('Background monitoring database query failed')
        result['errors'].append('Worker heartbeat or execution history could not be refreshed.')
    return result

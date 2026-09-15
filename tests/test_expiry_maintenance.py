import os
import time
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from conftest import run_on_shared_loop as run
from config import settings
from backend.core import enrollment_service as enrollment


def rows(values):
    return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: values))


@pytest.fixture
def pending_dir(tmp_path, monkeypatch):
    directory = tmp_path / 'pending'
    directory.mkdir()
    monkeypatch.setattr(enrollment, 'settings', SimpleNamespace(
        STORAGE_DIR=str(tmp_path), PENDING_UPLOAD_DIR=str(directory)))
    return directory


def test_orphans_beyond_first_batch_are_reached_and_valid_files_survive(pending_dir, monkeypatch):
    monkeypatch.setattr(enrollment, 'PENDING_SWEEP_BATCH', 2)
    for name in ['known1', 'known2', 'fresh', 'old1', 'old2', 'old3']:
        path = pending_dir / name
        path.write_bytes(b'test')
        if name != 'fresh':
            os.utime(path, (time.time() - 3600,) * 2)
    monkeypatch.setattr(enrollment.os, 'listdir', lambda _: ['known1', 'known2', 'fresh', 'old1', 'old2', 'old3'])
    db = SimpleNamespace(execute=AsyncMock(return_value=rows(['pending/known1', 'pending/known2'])))
    assert run(enrollment._sweep_orphan_pending_files(db, strict=True)) == 2
    assert all((pending_dir / name).exists() for name in ['known1', 'known2', 'fresh', 'old3'])
    assert not (pending_dir / 'old1').exists()
    assert not (pending_dir / 'old2').exists()


def test_pending_cleanup_refuses_gallery_images(pending_dir):
    gallery = pending_dir.parent / 'gallery.jpg'
    gallery.write_bytes(b'keep')
    with pytest.raises(enrollment.EnrollmentError):
        enrollment._pending_cleanup_path('gallery.jpg')
    enrollment._safe_unlink_pending('gallery.jpg')
    assert gallery.read_bytes() == b'keep'


def test_strict_expired_ticket_cleanup_deletes_file_and_row(pending_dir, monkeypatch):
    path = pending_dir / 'expired.jpg'
    path.write_bytes(b'expired')
    row = SimpleNamespace(storage_path='pending/expired.jpg')
    db = SimpleNamespace(execute=AsyncMock(return_value=rows([row])),
                         delete=AsyncMock(), commit=AsyncMock(), rollback=AsyncMock())
    monkeypatch.setattr(enrollment, '_sweep_orphan_pending_files', AsyncMock(return_value=0))
    assert run(enrollment.sweep_expired_pending(db, strict=True)) == 1
    assert not path.exists()
    db.delete.assert_awaited_once_with(row)
    db.commit.assert_awaited_once()


def test_strict_pending_failure_preserves_out_of_scope_file_and_row(pending_dir):
    path = pending_dir.parent / 'gallery.jpg'
    path.write_bytes(b'keep')
    db = SimpleNamespace(execute=AsyncMock(return_value=rows([SimpleNamespace(storage_path='gallery.jpg')])),
                         delete=AsyncMock(), commit=AsyncMock(), rollback=AsyncMock())
    with pytest.raises(enrollment.EnrollmentError):
        run(enrollment.sweep_expired_pending(db, strict=True))
    assert path.exists()
    db.delete.assert_not_awaited()
    db.commit.assert_not_awaited()
    db.rollback.assert_awaited_once()


@pytest.mark.parametrize('name', ['live_alert_cleanup', 'watchlist_cleanup', 'pending_enrollment_cleanup'])
@pytest.mark.parametrize('fails', [False, True])
def test_each_sweep_records_outcome_and_propagates_errors(monkeypatch, name, fails):
    from backend.core.background_maintenance import BackgroundMaintenance
    from backend.core.task_history import task_history_manager
    from backend.core.live_alert_service import live_alert_service
    from backend.core.watchlist_service import watchlist_service
    from db_connection import db_manager
    @asynccontextmanager
    async def session():
        yield object()
    monkeypatch.setattr(db_manager, 'get_session', session)
    started, completed = AsyncMock(), AsyncMock()
    monkeypatch.setattr(task_history_manager, 'record_task_started', started)
    monkeypatch.setattr(task_history_manager, 'record_task_completed', completed)
    operation = AsyncMock(side_effect=OSError('denied')) if fails else AsyncMock(return_value=3)
    target, attr = {
        'live_alert_cleanup': (live_alert_service, 'cleanup_expired_alerts'),
        'watchlist_cleanup': (watchlist_service, 'cleanup_expired_entries'),
        'pending_enrollment_cleanup': (enrollment, 'sweep_expired_pending'),
    }[name]
    monkeypatch.setattr(target, attr, operation)
    if fails:
        with pytest.raises(OSError):
            run(BackgroundMaintenance().sweep(name))
    else:
        assert run(BackgroundMaintenance().sweep(name)) == {'processed': 3}
    assert completed.call_args.args[2] is not fails
    assert 'Checks every 5 minutes' in started.call_args.kwargs['description']
    if name == 'pending_enrollment_cleanup':
        assert operation.call_args.kwargs['strict'] is True


def test_unknown_sweep_cannot_fall_through_to_enrollment_cleanup():
    from backend.core.background_maintenance import BackgroundMaintenance
    with pytest.raises(KeyError):
        run(BackgroundMaintenance().sweep('typo'))

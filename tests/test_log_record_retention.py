import logging
from logging.handlers import RotatingFileHandler
from types import SimpleNamespace

import pytest


OLD = b'2000-01-01 00:00:00,000 | ERROR    | pid=1 | MainThread | req=- | test | expired\n  expired traceback\n'
NEW = b'2099-01-01 00:00:00,000 | ERROR    | pid=1 | MainThread | req=- | test | retained\n  retained traceback\n'


def test_closed_pruning_uses_record_age_and_preserves_traceback(tmp_path):
    from backend.core.log_records import prune_closed
    path = tmp_path / 'app.log.1'
    path.write_bytes(b'unknown prefix\n' + OLD + NEW)
    result = prune_closed(path, 1700000000)
    assert result['expired_records'] == 1
    assert path.read_bytes() == b'unknown prefix\n' + NEW


def test_replace_failure_preserves_original_and_removes_temporary(tmp_path, monkeypatch):
    from backend.core import log_records
    path = tmp_path / 'app.log.1'
    path.write_bytes(OLD + NEW)
    def fail(*args):
        raise PermissionError('blocked')
    monkeypatch.setattr(log_records.os, 'replace', fail)
    with pytest.raises(PermissionError):
        log_records.prune_closed(path, 1700000000)
    assert path.read_bytes() == OLD + NEW
    assert list(tmp_path.iterdir()) == [path]


def test_owner_rotates_then_prunes_and_continues_writing(tmp_path, monkeypatch):
    import utils.logging as config_logging
    from backend.core.log_retention import clean_owned_log
    from config import settings
    path = tmp_path / 'app.log'
    path.write_bytes(OLD + NEW)
    handler = RotatingFileHandler(path, maxBytes=100000, backupCount=5)
    monkeypatch.setattr(config_logging, '_active_log_path', str(path))
    monkeypatch.setattr(config_logging, '_listener', SimpleNamespace(handlers=[handler]))
    monkeypatch.setattr(settings, 'WORKERS', 1)
    try:
        preview = clean_owned_log(tmp_path, 48, dry_run=True)
        assert preview['candidate_records'] == 1
        assert path.read_bytes() == OLD + NEW
        result = clean_owned_log(tmp_path, 48)
        assert result['active_rotated'] is True
        assert result['deleted_records'] == 1
        assert (tmp_path / 'app.log.1').read_bytes() == NEW
        handler.handle(logging.LogRecord('test', logging.INFO, '', 0, 'after cleanup', (), None))
        handler.flush()
        assert b'after cleanup' in path.read_bytes()
        assert b'after cleanup' not in (tmp_path / 'app.log.1').read_bytes()
    finally:
        handler.close()


def test_unowned_active_file_is_not_rewritten(tmp_path, monkeypatch):
    import utils.logging as config_logging
    from backend.core.log_retention import clean_owned_log
    path = tmp_path / 'app.log'
    path.write_bytes(OLD)
    monkeypatch.setattr(config_logging, '_active_log_path', str(path))
    monkeypatch.setattr(config_logging, '_listener', None)
    with pytest.raises(RuntimeError, match='not owned'):
        clean_owned_log(tmp_path, 48)
    assert path.read_bytes() == OLD

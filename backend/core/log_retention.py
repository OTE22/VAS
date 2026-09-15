"""Conservative file retention: explicit categories, active-file protection, preview."""
import logging
import os
import re
import time
from pathlib import Path

from config import settings


def _open_paths():
    paths = set()
    for logger in [logging.getLogger(), *logging.Logger.manager.loggerDict.values()]:
        for handler in getattr(logger, 'handlers', []):
            if getattr(handler, 'baseFilename', None):
                paths.add(Path(handler.baseFilename).resolve())
    # Protect quiet logs held open by other processes in this container too.
    for directory in Path('/proc').glob('[0-9]*/fd'):
        try:
            for fd in directory.iterdir():
                try:
                    paths.add(fd.resolve(strict=True))
                except FileNotFoundError:
                    pass
        except PermissionError:
            # Fail closed for legacy logs when process ownership is unobservable.
            return paths, False
    return paths, os.name == 'posix'


def clean(log_dir, retention_hours, *, dry_run=False):
    from utils.logging import active_log_path, LOG_SOURCE_FILES
    root = Path(log_dir).resolve()
    result = {'deleted_files': 0, 'candidate_files': 0, 'freed_space_mb': 0.0,
              'candidate_bytes': 0, 'failures': [], 'categories': {}, 'dry_run': dry_run,
              'deleted_records': 0, 'candidate_records': 0, 'trimmed_files': 0}
    if not root.exists():
        return result
    active = Path(active_log_path()).resolve()
    opened, can_check_legacy = _open_paths()
    now = time.time()
    app_pattern = re.compile(re.escape(Path(settings.LOG_FILE_NAME).name) + r'\.[1-9][0-9]*(?:\.gz)?$')
    legacy_pattern = re.compile(r'(?:access|error)\.log(?:\.[1-9][0-9]*(?:\.gz)?)?$')
    for directory, dirs, files in os.walk(root, followlinks=False):
        dirs[:] = [d for d in dirs if not (Path(directory) / d).is_symlink()]
        for name in files:
            path = Path(directory) / name
            relative = path.relative_to(root)
            category = None
            if len(relative.parts) == 1 and (app_pattern.fullmatch(name) or any(re.fullmatch(re.escape(base) + r'\.[1-9][0-9]*', name) for base in LOG_SOURCE_FILES.values())):
                category, hours = 'application_rotations', retention_hours
            elif len(relative.parts) == 1 and legacy_pattern.fullmatch(name) and can_check_legacy:
                category, hours = 'legacy_server_logs', settings.LEGACY_LOG_RETENTION_DAYS * 24
            elif relative.parts[0] in ('smoke', 'regression'):
                category, hours = 'diagnostics', settings.DIAGNOSTIC_LOG_RETENTION_DAYS * 24
            elif relative.parts[0] in ('audit', 'deploy', 'map_builds', 'legacy', 'diagnostics') and re.search(r'\.log(?:\.?[0-9]+)?$', name):
                # Audit data/models/scripts/reports are intentionally retained.
                category, hours = 'audit_text_logs', settings.DIAGNOSTIC_LOG_RETENTION_DAYS * 24
            if category is None or hours <= 0:
                continue
            try:
                if path.is_symlink() or path.resolve() in opened or path.resolve() == active:
                    continue
                if not path.resolve().is_relative_to(root):
                    continue
                stat = path.stat()
                if category == 'application_rotations' and path.suffix != '.gz':
                    from backend.core.log_records import prune_closed
                    outcome = prune_closed(path, now - hours * 3600, dry_run=dry_run)
                    if outcome['recognized']:
                        if outcome['expired_records']:
                            result['candidate_records'] += outcome['expired_records']
                            result['candidate_bytes'] += outcome['expired_bytes']
                            result['candidate_files'] += 1
                            result['categories'][category] = result['categories'].get(category, 0) + 1
                            if not dry_run:
                                result['deleted_records'] += outcome['expired_records']
                                result['trimmed_files'] += 1
                                result['freed_space_mb'] += outcome['expired_bytes'] / (1024 * 1024)
                        continue
                if stat.st_mtime >= now - hours * 3600:
                    continue
                result['candidate_files'] += 1
                result['candidate_bytes'] += stat.st_size
                result['categories'][category] = result['categories'].get(category, 0) + 1
                if not dry_run:
                    # Do not delete a file replaced/updated since enumeration.
                    current = path.stat()
                    if (current.st_ino, current.st_mtime_ns, current.st_size) != (stat.st_ino, stat.st_mtime_ns, stat.st_size):
                        continue
                    path.unlink()
                    result['deleted_files'] += 1
                    result['freed_space_mb'] += stat.st_size / (1024 * 1024)
            except FileNotFoundError:
                continue
            except OSError as exc:
                result['failures'].append(f'{relative}: {exc}')
    return result


def clean_owned_log(log_dir, retention_hours, *, dry_run=False):
    """Rotate via its owner, then prune closed record files under the sink lock.

    This never rewrites/truncates the active file. The local API deployment has
    one writer; multi-worker shared files are refused instead of risking loss.
    """
    from utils.logging import active_log_path, active_log_writer_lock
    from backend.core.log_records import has_expired, prune_closed
    active = Path(active_log_path()).resolve()
    root = Path(log_dir).resolve()
    cutoff = time.time() - retention_hours * 3600
    if retention_hours <= 0 or not active.is_relative_to(root):
        return clean(log_dir, retention_hours, dry_run=dry_run)
    with active_log_writer_lock() as handler:
        rotate = active.exists() and has_expired(active, cutoff)
        if dry_run:
            result = clean(log_dir, retention_hours, dry_run=True)
            if rotate:
                outcome = prune_closed(active, cutoff, dry_run=True)
                result['candidate_records'] += outcome['expired_records']
                result['candidate_bytes'] += outcome['expired_bytes']
                result['candidate_files'] += 1
            result['active_rotation_needed'] = rotate
            return result
        if rotate:
            if int(settings.WORKERS) != 1 or handler.backupCount < 1:
                raise RuntimeError('Record retention requires one log writer and at least one rotation')
            handler.doRollover()
        result = clean(log_dir, retention_hours)
        result['active_rotated'] = rotate
        return result

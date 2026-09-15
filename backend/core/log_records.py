"""Stream complete log records so retention never separates a traceback."""
import os
import re
import tempfile
from datetime import datetime
from pathlib import Path

HEADER = re.compile(rb'^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})(?:,\d{3})?\s*\|')


def record_time(line):
    match = HEADER.match(line)
    if match:
        try:
            return datetime.strptime(match[1].decode('ascii'), '%Y-%m-%d %H:%M:%S').timestamp()
        except ValueError:
            pass
    return None


def has_expired(path, cutoff):
    with open(path, 'rb') as source:
        return any(stamp is not None and stamp < cutoff for stamp in map(record_time, source))


def prune_closed(path, cutoff, dry_run=False):
    """Atomically replace a CLOSED file; unrecognized leading text is preserved."""
    path = Path(path)
    before = path.stat()
    removed = 0
    recognized = 0
    freed = 0
    temporary = None
    sink = None
    try:
        if not dry_run:
            fd, temporary = tempfile.mkstemp(prefix='.retention-', dir=path.parent)
            sink = os.fdopen(fd, 'wb')
        with path.open('rb') as source:
            discard = False
            for line in source:
                stamp = record_time(line)
                if stamp is not None:
                    recognized += 1
                    discard = stamp < cutoff
                    removed += int(discard)
                if discard:
                    freed += len(line)
                elif sink:
                    sink.write(line)
        if sink:
            sink.flush()
            os.fsync(sink.fileno())
            sink.close()
        if removed and not dry_run:
            after = path.stat()
            if (before.st_ino, before.st_mtime_ns, before.st_size) != (after.st_ino, after.st_mtime_ns, after.st_size):
                raise OSError('Log changed during retention; original preserved')
            os.chmod(temporary, before.st_mode)
            os.replace(temporary, path)
            temporary = None
        return {'recognized': recognized, 'expired_records': removed, 'expired_bytes': freed}
    finally:
        if sink and not sink.closed:
            sink.close()
        if temporary:
            os.unlink(temporary)

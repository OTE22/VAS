"""
System Logs Viewer Routes
=========================
Admin-only read access to THE configured application log set — the same
rotating file `utils/logging.py` writes, and nothing else.

    all backend components -> central logger -> stdout + one rotating file
                                                       -> /api/logs -> admin-logs.js

What this module deliberately does NOT do:

* **Accept a path, filename or glob from a client.** The file set comes from
  `utils.logging.rotated_log_paths()`, which builds it from `settings.LOG_DIR`
  and `settings.LOG_FILE_NAME`. There is no attacker-controlled component in
  any path here, so traversal is impossible by construction rather than by
  sanitising.
* **Read unbounded.** A log read must never be a way to pin the event loop or
  exhaust memory. Every query is bounded by `LOG_API_MAX_SCAN_FILES`,
  `LOG_API_MAX_SCAN_BYTES` and `LOG_API_TIMEOUT_SECONDS`, and reports when it
  stopped early instead of pretending the result is complete.
* **Return an empty 200 when logging is broken.** A missing log directory or
  unreadable active file is an operational fault, not "no logs" — it returns
  503 with a specific reason. The previous version answered 200 with an empty
  list for a missing directory, which is indistinguishable from a quiet system.

Why the parser was rewritten
----------------------------
The three previous regexes all rejected the format the logger actually emits
(padded `%(levelname)-8s`, a `pid=N` prefix, and a `threadName` field they did
not know about). Every line therefore fell through to a keyword heuristic that:

* stamped each entry with `datetime.utcnow()`, so `date_from`/`date_to`
  filtered against the time of the REQUEST — any range not containing today
  returned nothing regardless of content;
* dropped `process_id` and `logger_name`, then repeated the raw metadata inside
  the message body;
* guessed the level by substring, so an ERROR mentioning "debug" was filed as
  DEBUG and vanished from a level filter;
* silently discarded continuation lines — measured at 329 dropped out of 417
  non-conforming lines in a live sample, which is most of every traceback.

There is now ONE pattern, derived from `utils.logging.LOG_FORMAT`, and lines
that do not match it are attached to the preceding entry as continuation text
rather than dropped. That is what keeps a stack trace intact and attached to
the record that raised it.
"""

import asyncio
import logging
import os
import re
import sys
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from pydantic import BaseModel

parent_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if parent_dir not in sys.path:
    sys.path.insert(0, parent_dir)

from config import settings
from backend.auth.auth_service import require_role
from db_models import User
from utils.logging import active_log_path, rotated_log_paths

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Logs"])

# Log content must never be served stale by a browser or proxy.
NO_STORE = "no-store, no-cache, must-revalidate"

LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")

# Mirrors utils.logging.LOG_FORMAT exactly:
#   asctime | levelname-8 | pid=N | threadName | req=ID | logger | message
# `levelname` is space-padded to 8, hence \s* before the delimiter — getting
# that wrong is what made the old pattern match only "CRITICAL".
LOG_LINE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}(?:,\d{3})?)\s*\|\s*"
    r"(?P<level>DEBUG|INFO|WARNING|ERROR|CRITICAL)\s*\|\s*"
    r"pid=(?P<pid>\d+)\s*\|\s*"
    r"(?P<thread>[^|]*?)\s*\|\s*"
    r"req=(?P<request_id>[^|]*?)\s*\|\s*"
    r"(?P<name>[^|]*?)\s*\|\s*"
    r"(?P<message>.*)$"
)

# Same shape without the req= field, so records written before request-id
# support are still parsed rather than dumped into the fallback.
LOG_LINE_LEGACY = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}(?:,\d{3})?)\s*\|\s*"
    r"(?P<level>DEBUG|INFO|WARNING|ERROR|CRITICAL)\s*\|\s*"
    r"pid=(?P<pid>\d+)\s*\|\s*"
    r"(?:(?P<thread>[^|]*?)\s*\|\s*)?"
    r"(?P<name>[^|]*?)\s*\|\s*"
    r"(?P<message>.*)$"
)


class LogEntry(BaseModel):
    """One log RECORD — not one line. A traceback is part of its record."""
    timestamp: str
    level: str
    process_id: Optional[str] = None
    logger_name: Optional[str] = None
    request_id: Optional[str] = None
    message: str
    full_line: str
    line_number: Optional[int] = None
    source_file: Optional[str] = None


class LogsResponse(BaseModel):
    logs: List[LogEntry]
    total_count: int
    page: int
    page_size: int
    total_pages: int
    has_next: bool
    has_previous: bool
    date_from: Optional[str] = None
    date_to: Optional[str] = None
    level_filter: Optional[str] = None
    filtered_count: int
    # Honesty fields: a bounded scan must say so, or a partial answer reads as
    # a complete one.
    scanned_files: int = 0
    scanned_bytes: int = 0
    truncated: bool = False


def _parse_ts(raw: str) -> Optional[datetime]:
    for fmt in ("%Y-%m-%d %H:%M:%S,%f", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(raw, fmt)
        except ValueError:
            continue
    return None


def _tail_bytes(path: str, budget: int) -> Tuple[str, int]:
    """Read at most `budget` bytes from the END of a file.

    The newest records are at the end and the viewer shows newest first, so the
    tail is both the cheapest and the most useful slice. A partial first line is
    discarded rather than parsed as a corrupt record.
    """
    if budget <= 0:
        return "", 0
    size = os.path.getsize(path)
    with open(path, "rb") as handle:
        if size > budget:
            handle.seek(size - budget)
            blob = handle.read()
            newline = blob.find(b"\n")
            blob = blob[newline + 1:] if newline != -1 else blob
        else:
            blob = handle.read()
    return blob.decode("utf-8", errors="replace"), len(blob)


def _entries_from_text(text: str, source: str) -> List[LogEntry]:
    """Parse forward, attaching continuation lines to the record above them."""
    entries: List[LogEntry] = []
    for offset, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        match = LOG_LINE.match(line) or LOG_LINE_LEGACY.match(line)
        if match:
            data = match.groupdict()
            entries.append(LogEntry(
                timestamp=data["ts"],
                level=data["level"],
                process_id=data.get("pid"),
                logger_name=(data.get("name") or "").strip() or None,
                request_id=(data.get("request_id") or "").strip() or None,
                message=data["message"],
                full_line=line,
                line_number=offset,
                source_file=source,
            ))
        elif entries:
            # Continuation: traceback body, banner, wrapped text. It belongs to
            # the record above — dropping it is how stack traces got shredded.
            entries[-1].message += "\n" + line
            entries[-1].full_line += "\n" + line
    return entries


def _collect(level: Optional[str], date_from: Optional[datetime],
             date_to: Optional[datetime], needed: int) -> Dict[str, Any]:
    """Gather matching entries newest-first across the configured log set.

    Blocking by design — the caller runs it in a thread. Stops at the first of:
    enough entries for the requested page, the file cap, the byte cap, or the
    time budget.
    """
    max_files = int(settings.LOG_API_MAX_SCAN_FILES)
    max_bytes = int(settings.LOG_API_MAX_SCAN_BYTES)
    deadline = time.monotonic() + float(settings.LOG_API_TIMEOUT_SECONDS)

    paths = rotated_log_paths(max_files=max_files)
    collected: List[LogEntry] = []
    scanned_files = 0
    scanned_bytes = 0
    truncated = False

    for index, path in enumerate(paths):        # already newest-first
        if scanned_bytes >= max_bytes or time.monotonic() > deadline:
            truncated = True
            break
        try:
            text, read_bytes = _tail_bytes(path, max_bytes - scanned_bytes)
            file_size = os.path.getsize(path)
        except OSError as exc:
            logger.warning("[LOGS] Skipping unreadable log file %s: %s",
                           os.path.basename(path), exc)
            continue
        scanned_files += 1
        scanned_bytes += read_bytes
        if file_size > read_bytes:
            truncated = True

        entries = _entries_from_text(text, os.path.basename(path))
        entries.reverse()                       # newest first within the file

        for entry in entries:
            if level and level != "ALL" and entry.level != level:
                continue
            if date_from or date_to:
                stamp = _parse_ts(entry.timestamp)
                if stamp is None:
                    continue        # filter on real timestamps or not at all
                if date_from and stamp < date_from:
                    continue
                if date_to and stamp > date_to:
                    continue
            collected.append(entry)

        if len(collected) >= needed:
            # More files remain unscanned, so there is more history than shown.
            truncated = truncated or index + 1 < len(paths)
            break

    return {
        "entries": collected,
        "scanned_files": scanned_files,
        "scanned_bytes": scanned_bytes,
        "truncated": truncated,
    }


def _require_log_source() -> str:
    """The active log path, or a 503 explaining exactly what is wrong.

    A missing directory or file means logging is misconfigured or the volume is
    not mounted. Answering 200 with an empty list would present that as a quiet
    system, which is the opposite of the truth.
    """
    path = active_log_path()
    directory = os.path.dirname(path)
    if not os.path.isdir(directory):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(f"Log directory is not available (LOG_DIR={settings.LOG_DIR}). "
                    "Check that the log volume is mounted and writable."),
        )
    if not os.path.isfile(path):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(f"Active log file {settings.LOG_FILE_NAME} does not exist in "
                    f"LOG_DIR={settings.LOG_DIR}. Logging may not be configured."),
        )
    if not os.access(path, os.R_OK):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(f"Active log file {settings.LOG_FILE_NAME} is not readable "
                    "by the API process."),
        )
    return path


@router.get("/api/logs/config", summary="Log viewer configuration")
async def get_logs_config(
    response: Response,
    current_user: User = Depends(require_role(["admin"])),
):
    """Bounds and vocabulary for the log viewer.

    The frontend reads page sizes and level names from here instead of holding
    its own copies, so changing `LOG_API_*` in settings changes the UI without
    touching JavaScript.
    """
    response.headers["Cache-Control"] = NO_STORE
    default_size = int(settings.LOG_API_DEFAULT_PAGE_SIZE)
    max_size = int(settings.LOG_API_MAX_PAGE_SIZE)
    options = sorted({s for s in (25, 50, 100, 200, 500, default_size, max_size)
                      if 0 < s <= max_size})
    return {
        "default_page_size": default_size,
        "max_page_size": max_size,
        "page_size_options": options,
        "levels": list(LEVELS),
        "default_level": "all",
        "max_scan_files": int(settings.LOG_API_MAX_SCAN_FILES),
        "max_scan_bytes": int(settings.LOG_API_MAX_SCAN_BYTES),
        "timeout_seconds": float(settings.LOG_API_TIMEOUT_SECONDS),
        "log_file": os.path.basename(active_log_path()),
        "retention_hours": int(settings.LOGS_LIFE_TIME_HOURS),
    }


@router.get("/api/logs", response_model=LogsResponse, summary="Read application logs")
async def get_logs(
    response: Response,
    page: int = Query(1, ge=1),
    page_size: Optional[int] = Query(None, ge=1),
    date_from: Optional[str] = Query(None, description="YYYY-MM-DD"),
    date_to: Optional[str] = Query(None, description="YYYY-MM-DD"),
    level: Optional[str] = Query(None, description="DEBUG|INFO|WARNING|ERROR|CRITICAL|all"),
    current_user: User = Depends(require_role(["admin"])),
):
    """Read the application log from the rotated files on disk, newest first, filtered by level and date. The scan is bounded by file count, byte budget and a timeout; truncated scans still report has_next. 503 when the log directory is unreadable."""
    response.headers["Cache-Control"] = NO_STORE
    _require_log_source()

    max_size = int(settings.LOG_API_MAX_PAGE_SIZE)
    if page_size is None:
        page_size = int(settings.LOG_API_DEFAULT_PAGE_SIZE)
    elif page_size > max_size:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"page_size must not exceed the configured maximum of {max_size}",
        )

    wanted_level = (level or "").strip().upper() or None
    if wanted_level and wanted_level != "ALL" and wanted_level not in LEVELS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"level must be one of {', '.join(LEVELS)} or 'all'",
        )

    def _parse_day(raw: Optional[str], end_of_day: bool) -> Optional[datetime]:
        if not raw:
            return None
        try:
            day = datetime.strptime(raw, "%Y-%m-%d")
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"Invalid date '{raw}'. Use YYYY-MM-DD.",
            )
        return day.replace(hour=23, minute=59, second=59) if end_of_day else day

    start = _parse_day(date_from, False)
    end = _parse_day(date_to, True)
    needed = page * page_size

    result = await asyncio.get_running_loop().run_in_executor(
        None, _collect, wanted_level, start, end, needed)

    entries = result["entries"]
    total = len(entries)
    offset = (page - 1) * page_size
    window = entries[offset:offset + page_size]
    total_pages = (total + page_size - 1) // page_size if total else 0

    return LogsResponse(
        logs=window,
        total_count=total,
        page=page,
        page_size=page_size,
        total_pages=total_pages,
        # `truncated` means more exists beyond the scan bound, so there IS a
        # next page even when this scan happened to end on a boundary.
        has_next=(page < total_pages) or bool(result["truncated"] and len(window) == page_size),
        has_previous=page > 1,
        date_from=date_from,
        date_to=date_to,
        level_filter=level,
        filtered_count=total,
        scanned_files=result["scanned_files"],
        scanned_bytes=result["scanned_bytes"],
        truncated=result["truncated"],
    )


@router.get("/api/logs/stats", summary="Application log statistics")
async def get_log_stats(
    response: Response,
    current_user: User = Depends(require_role(["admin"])),
):
    """Counts by level over the configured log set, within the same bounds.

    Levels come from the PARSED level field. The previous version tested
    substrings like `' ERROR '` against each raw line with independent `if`s, so
    one line could increment several counters and any message merely mentioning
    a level name was counted as one.
    """
    response.headers["Cache-Control"] = NO_STORE
    _require_log_source()

    def _scan() -> Dict[str, Any]:
        max_files = int(settings.LOG_API_MAX_SCAN_FILES)
        max_bytes = int(settings.LOG_API_MAX_SCAN_BYTES)
        deadline = time.monotonic() + float(settings.LOG_API_TIMEOUT_SECONDS)

        counts = {name: 0 for name in LEVELS}
        files: List[Dict[str, Any]] = []
        scanned_bytes = 0
        truncated = False
        newest = None

        for path in rotated_log_paths(max_files=max_files):
            try:
                size = os.path.getsize(path)
                modified = datetime.fromtimestamp(os.path.getmtime(path))
            except OSError:
                continue
            newest = modified if newest is None or modified > newest else newest
            files.append({
                "name": os.path.basename(path),
                "size_mb": round(size / (1024 * 1024), 2),
                "modified": modified.isoformat(),
            })
            if scanned_bytes >= max_bytes or time.monotonic() > deadline:
                truncated = True
                continue
            try:
                text, read_bytes = _tail_bytes(path, max_bytes - scanned_bytes)
            except OSError:
                continue
            scanned_bytes += read_bytes
            if size > read_bytes:
                truncated = True
            for entry in _entries_from_text(text, os.path.basename(path)):
                counts[entry.level] += 1

        return {
            "counts": counts,
            "files": files,
            "scanned_bytes": scanned_bytes,
            "truncated": truncated,
            "newest": newest.isoformat() if newest else None,
            "total_mb": round(sum(f["size_mb"] for f in files), 2),
        }

    scan = await asyncio.get_running_loop().run_in_executor(None, _scan)
    counts = scan["counts"]
    return {
        "total_debug": counts["DEBUG"],
        "total_info": counts["INFO"],
        "total_warning": counts["WARNING"],
        "total_errors": counts["ERROR"],
        "total_critical": counts["CRITICAL"],
        "total_entries": sum(counts.values()),
        "file_size_mb": scan["total_mb"],
        "last_modified": scan["newest"],
        "file_path": settings.LOG_DIR,
        "log_files": scan["files"],
        "logs_life_time_hours": int(settings.LOGS_LIFE_TIME_HOURS),
        "scanned_bytes": scan["scanned_bytes"],
        "truncated": scan["truncated"],
    }


@router.post("/api/logs/cleanup", summary="Delete log entries past retention")
async def manual_log_cleanup(
    response: Response,
    current_user: User = Depends(require_role(["admin"])),
):
    """Apply `LOGS_LIFE_TIME_HOURS` to the configured log set.

    Delegates to `backend/core/log_cleanup.py`, which deletes whole ROTATED
    files rather than rewriting the active one: the active file is held open by
    a RotatingFileHandler with its own write offset, and truncating it
    underneath the handler corrupts the next write and fights the handler's own
    maxBytes rotation.
    """
    response.headers["Cache-Control"] = NO_STORE
    _require_log_source()
    try:
        from backend.core.log_cleanup import log_cleanup_manager
        if not log_cleanup_manager:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Log cleanup manager is not initialized.",
            )
        return await log_cleanup_manager.cleanup_old_logs()
    except HTTPException:
        raise
    except Exception as exc:                                   # noqa: BLE001
        logger.error("[LOGS] Cleanup failed: %s", exc, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Log cleanup failed; see server logs for details.",
        )

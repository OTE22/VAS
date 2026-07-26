"""
Centralized logging configuration
=================================
ONE place that decides where every application log goes:

    Console handler (stdout)      -> Docker logs (`docker logs -f face_recognition_api`)
    RotatingFileHandler           -> /var/log/face-recognition/app.log (bounded)

Design rules:
- Idempotent: safe to call setup_logging() any number of times (gunicorn master,
  each worker, dev `python main.py`, tests) — only the first call configures.
- Async/multiprocess safe: producers push to a Queue; a single QueueListener
  thread does the actual I/O, so event-loop code never blocks on disk writes.
- No silent loggers: root gets the queue handler and `propagate` stays True for
  application loggers; uvicorn/gunicorn loggers keep their own stdout wiring
  (gunicorn.conf.py sends them to "-") and don't propagate, so nothing dupes.
- Security: a SensitiveDataFilter redacts secrets (tokens, cookies, passwords,
  connection-string credentials, raw embeddings) BEFORE any handler sees them.
- Level comes from LOG_LEVEL (config/env, default INFO) — debug spam stays out
  of Docker unless explicitly enabled.
"""

import atexit
import logging
import logging.handlers
import os
import re
import sys
from queue import Queue
from typing import Optional

_log_queue: Optional[Queue] = None
_listener: Optional[logging.handlers.QueueListener] = None

LOG_FORMAT = "%(asctime)s | %(levelname)-8s | pid=%(process)d | %(name)s | %(message)s"
FILE_LOG_FORMAT = "%(asctime)s | %(levelname)-8s | pid=%(process)d | %(threadName)s | %(name)s | %(message)s"


class SensitiveDataFilter(logging.Filter):
    """Redacts secrets from every record before ANY handler receives it.

    Applied to the queue handler on the root logger, so both the console
    (Docker) and the rotating file get the sanitized message.
    """

    REDACTED = "***REDACTED***"

    PATTERNS = [
        # Header-style credentials:  Authorization: Bearer xyz / Basic xyz / cookie: ...
        # (value may include an auth scheme followed by a space — consume both)
        (re.compile(r"(?i)\b(authorization|proxy-authorization|set-cookie|cookie|x-api-key)"
                    r"(['\"]?\s*[:=]\s*)['\"]?(?:Bearer\s+|Basic\s+|Digest\s+)?[^,'\";\s}{\]]+"),
         r"\1\2" + REDACTED),
        (re.compile(r"(?i)\b(password|passwd|pwd|password_hash|client_secret|secret_key|secret|"
                    r"api[_-]?key|apikey|access[_-]?token|refresh[_-]?token|session[_-]?token|"
                    r"id[_-]?token|private[_-]?key)"
                    r"(['\"]?\s*[:=]\s*)['\"]?[^,'\";\s}{\]]+"), r"\1\2" + REDACTED),
        # Bearer tokens wherever they appear
        (re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._\-]{12,}"), "Bearer " + REDACTED),
        # Bare JWTs (three base64url segments starting with eyJ)
        (re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{4,}\.[A-Za-z0-9_\-]{4,}\b"),
         "***JWT-REDACTED***"),
        # Credentials embedded in connection strings: postgresql+asyncpg://user:pass@host
        (re.compile(r"(?i)\b([a-z][a-z0-9+]{1,30}):\/\/([^:\/\s'\"]+):([^@\/\s'\"]+)@"),
         r"\1://\2:" + REDACTED + "@"),
        # Raw face embeddings / biometric vectors: long arrays of floats
        (re.compile(r"[\[\(](?:\s*-?\d+(?:\.\d+)?(?:e-?\d+)?\s*,\s*){12,}[^\]\)]*[\]\)]"),
         "[***EMBEDDING-REDACTED***]"),
    ]

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:
            return True  # never let redaction break logging

        sanitized = message
        for pattern, replacement in self.PATTERNS:
            sanitized = pattern.sub(replacement, sanitized)

        if sanitized != message:
            record.msg = sanitized
            record.args = ()
        return True


def _resolve_log_settings(log_dir: Optional[str]):
    """LOG_DIR / LOG_LEVEL from central config, env, or defaults."""
    level_name = os.getenv("LOG_LEVEL", "INFO")
    if log_dir is None:
        log_dir = os.getenv("LOG_DIR")
    try:
        parent_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        if parent_dir not in sys.path:
            sys.path.insert(0, parent_dir)
        from config import settings
        log_dir = log_dir or getattr(settings, "LOG_DIR", None)
        level_name = getattr(settings, "LOG_LEVEL", level_name) or level_name
    except Exception:
        pass
    log_dir = log_dir or "./logs"
    level = getattr(logging, str(level_name).upper(), logging.INFO)
    return log_dir, level


def setup_logging(
    log_to_file: bool = True,
    log_dir: Optional[str] = None,
    filename: str = "app.log",
):
    """Configure application-wide logging (idempotent).

    Console (stdout) -> Docker logs; optional rotating file for persistence.
    """
    global _log_queue, _listener

    if _listener:
        return  # already configured in this process — never double-attach

    log_dir, level = _resolve_log_settings(log_dir)

    root = logging.getLogger()
    root.setLevel(level)

    # Replace any pre-existing root handlers (e.g. from stray basicConfig calls)
    for handler in root.handlers[:]:
        root.removeHandler(handler)

    _log_queue = Queue(-1)
    queue_handler = logging.handlers.QueueHandler(_log_queue)
    # Redact in the producing thread, before the record ever reaches a handler
    queue_handler.addFilter(SensitiveDataFilter())
    root.addHandler(queue_handler)

    handlers = []

    # Console -> stdout -> Docker logs (the primary sink)
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(logging.Formatter(LOG_FORMAT))
    handlers.append(console_handler)

    # Optional persistent copy, BOUNDED (10MB x 5) — never replaces the console
    if log_to_file:
        try:
            os.makedirs(log_dir, exist_ok=True)
            file_handler = logging.handlers.RotatingFileHandler(
                os.path.join(log_dir, filename),
                maxBytes=10 * 1024 * 1024,
                backupCount=5,
                encoding="utf-8",
            )
            file_handler.setFormatter(logging.Formatter(FILE_LOG_FORMAT))
            handlers.append(file_handler)
        except OSError as e:
            print(f"[LOGGING] File logging disabled ({e}); console only", file=sys.stderr)

    _listener = logging.handlers.QueueListener(
        _log_queue, *handlers, respect_handler_level=True
    )
    _listener.start()
    atexit.register(_stop_listener)

    _configure_third_party_loggers(level)

    logging.getLogger(__name__).info(
        "✅ Logging initialized: level=%s console=stdout file=%s",
        logging.getLevelName(level),
        os.path.join(log_dir, filename) if log_to_file else "disabled",
    )


def _configure_third_party_loggers(level: int):
    """Make server/framework loggers flow to Docker exactly once.

    gunicorn/uvicorn loggers get their handlers from gunicorn.conf.py
    (accesslog/errorlog = "-", i.e. stdout/stderr). When those handlers exist
    we keep them and disable propagation (no duplicates). If they have no
    handlers (e.g. plain `uvicorn` dev run before its own config), they
    propagate to root so nothing is silently lost.
    """
    for name in (
        "uvicorn", "uvicorn.error", "uvicorn.access",
        "gunicorn", "gunicorn.error", "gunicorn.access",
        "fastapi",
    ):
        lg = logging.getLogger(name)
        lg.setLevel(level)
        if lg.handlers:
            lg.propagate = False  # already writing to stdout via gunicorn "-"
        else:
            lg.propagate = True

    # Application + infra loggers: no private handlers, propagate to root
    for name in ("backend", "sql_agent", "redis"):
        lg = logging.getLogger(name)
        lg.handlers = []
        lg.propagate = True

    # SQLAlchemy is extremely chatty at INFO (every statement w/ echo);
    # WARNING keeps real DB problems visible without flooding Docker.
    logging.getLogger("sqlalchemy").setLevel(logging.WARNING)
    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)


def _stop_listener():
    """Flush and stop the queue listener on interpreter shutdown."""
    global _listener
    if _listener:
        try:
            _listener.stop()
        except Exception:
            pass
        _listener = None

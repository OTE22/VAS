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
from contextvars import ContextVar
from queue import Queue
from typing import List, Optional

_log_queue: Optional[Queue] = None
_listener: Optional[logging.handlers.QueueListener] = None
_active_log_path: Optional[str] = None

# ONE format for BOTH sinks. stdout and the rotating file previously used
# different formatters (the file carried threadName, stdout did not), which
# meant the log viewer had to guess which shape it was parsing depending on
# where a line came from.
#
# Fields: timestamp | level | pid | thread | request id | logger | message
LOG_FORMAT = (
    "%(asctime)s | %(levelname)-8s | pid=%(process)d | %(threadName)s | "
    "req=%(request_id)s | %(name)s | %(message)s"
)

# Set by the HTTP middleware for the duration of a request. A ContextVar
# (not thread-local) because the request path is async: one thread serves many
# concurrent requests, and only a context var follows the coroutine.
request_id_var: ContextVar[str] = ContextVar("request_id", default="-")


def set_request_id(value: Optional[str]) -> None:
    """Bind a request id to the current context; every later log line carries it."""
    request_id_var.set(value or "-")


class StdoutHandler(logging.StreamHandler):
    """The console sink: stdout, for `docker logs`.

    KNOWN LIMITATION under gunicorn + uvicorn.workers.UvicornWorker.
    Records emitted during STARTUP reach `docker logs`; records emitted while
    SERVING A REQUEST reach the rotating file but not stdout. Measured by
    tagging a login with a unique X-Request-ID and grepping both sinks: the
    file gets the line, `docker logs` does not. The handler does not raise —
    `logging.Handler.handleError` produces no output — so the write appears to
    succeed and the bytes go somewhere Docker is not reading.

    Ruled out by direct measurement, so do not re-try these:
      * `os.dup(1)` at setup     — the worker's fd 1 is replaced afterwards
      * `sys.__stdout__`         — not connected in the worker (nothing at all
                                   reached stdout, including startup lines)
      * moving setup earlier     — post_fork behaves the same as app-import
      * handler level / filters  — both sinks share one level, and the
                                   RequestContextFilter is on both
      * buffering                — PYTHONUNBUFFERED=1 is set and emit() flushes

    Still unexplained: raw `os.write(1, ...)` from the worker DOES reach
    `docker logs`, and the worker's fd 1 is the same pipe as PID 1's, so the
    descriptor is right. The gap is between the handler's stream object and
    that descriptor. Gunicorn's own access log reaches stdout because it holds
    a handler created inside the worker after uvicorn's setup.

    The rotating file is unaffected and remains complete, which is what
    /api/logs serves — so no log data is lost, only the container-log mirror is
    incomplete for request-time records.
    """

    def __init__(self):
        super().__init__(sys.stdout)


class RequestContextFilter(logging.Filter):
    """Guarantees `%(request_id)s` resolves on EVERY record.

    Without this, one record emitted outside a request (a background job, a
    startup line) would raise a formatting error and logging would silently
    drop it. `-` means "no request in scope", which is a fact worth seeing.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if not hasattr(record, "request_id"):
            try:
                record.request_id = request_id_var.get()
            except Exception:                                  # noqa: BLE001
                record.request_id = "-"
        return True


class SensitiveDataFilter(logging.Filter):
    """Redacts secrets from every record before ANY handler receives it.

    Applied to the queue handler on the root logger, so both the console
    (Docker) and the rotating file get the sanitized message.
    """

    REDACTED = "***REDACTED***"

    PATTERNS = [
        # Header-style credentials:  Authorization: Bearer xyz / Basic xyz / cookie: ...
        # (value may include an auth scheme followed by a space — consume both).
        # x-webhook-key is the pipeline ingest header:
        # it is presented on every camera POST, so a single logged request line
        # would publish the credential for the whole fleet.
        (re.compile(r"(?i)\b(authorization|proxy-authorization|set-cookie|cookie|x-api-key|"
                    r"x-webhook-key)"
                    r"(['\"]?\s*[:=]\s*)['\"]?(?:Bearer\s+|Basic\s+|Digest\s+)?[^,'\";\s}{\]]+"),
         r"\1\2" + REDACTED),
        # Named credential fields.
        #
        # `webhook[_-]?api[_-]?keys?` is listed explicitly rather than relying on
        # the `api[_-]?key` alternative below it: that one is anchored with \b,
        # and in "WEBHOOK_API_KEYS" the position before "API" sits between two
        # word characters, so the boundary never matches and the value was
        # logged in full.
        # `webhook[_-]?auth[_-]?token` is listed for the same reason as
        # `webhook[_-]?api[_-]?keys?` above: none of the `*[_-]?token`
        # alternatives matches "WEBHOOK_AUTH_TOKEN", because each is anchored to
        # its own prefix and a bare \b before "TOKEN" falls between two word
        # characters. Its value is folded into WEBHOOK_API_KEYS at startup and so
        # is already caught by the literal-value harvest below, but a record that
        # names the field must redact on the NAME too — the harvest is empty
        # before settings are constructed.
        (re.compile(r"(?i)\b(password|passwd|pwd|password_hash|client_secret|secret_key|secret|"
                    r"webhook[_-]?api[_-]?keys?|webhook[_-]?auth[_-]?token|webhook[_-]?keys?|"
                    # Issued ingest credentials. The literal-value harvest below
                    # CANNOT help here: it is built from `settings`, and a minted
                    # token never enters settings — it exists only in the one
                    # response that returns it. The API field is named plainly
                    # `token`, which no other alternative here matches (they are
                    # each anchored to their own prefix), so a careless
                    # logger.info("issued %s", payload) would print it in full.
                    r"webhook[_-]?credential[_-]?token|ingest[_-]?token|"
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

    # Literal secret VALUES, redacted wherever they appear regardless of the
    # surrounding text.
    #
    # The patterns above key off the FIELD NAME, which only works when the value
    # is logged next to a name they recognize. `key=<ingest key>` or a value
    # interpolated into a sentence slips straight through. The ingest key is
    # presented on every camera request, so it has the most chances to appear in
    # an unanticipated shape — and unlike a session token it does not expire.
    #
    # Resolved once, lazily: importing config at module scope would make the
    # logging package depend on settings being constructed first.
    _literal_secrets: Optional[tuple] = None

    @classmethod
    def literal_secrets(cls) -> tuple:
        if cls._literal_secrets is not None:
            return cls._literal_secrets
        values = set()
        try:
            from config import settings

            from backend.security.webhook_auth import parse_keys
            values.update(parse_keys(settings.WEBHOOK_API_KEYS))
            for name in ("JWT_SECRET_KEY", "POSTGRES_PASSWORD"):
                value = getattr(settings, name, "") or ""
                if value:
                    values.add(str(value))
        except Exception:                                      # noqa: BLE001
            # Logging must work before (and without) configuration.
            cls._literal_secrets = ()
            return cls._literal_secrets
        # Short values would redact innocent text; only hide things long enough
        # to be a credential.
        cls._literal_secrets = tuple(v for v in values if len(v) >= 12)
        return cls._literal_secrets

    @classmethod
    def reset_literal_secrets(cls) -> None:
        """Force re-resolution — used by tests and after a config reload."""
        cls._literal_secrets = None

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:
            return True  # never let redaction break logging

        sanitized = message
        for pattern, replacement in self.PATTERNS:
            sanitized = pattern.sub(replacement, sanitized)

        for secret in self.literal_secrets():
            if secret in sanitized:
                sanitized = sanitized.replace(secret, self.REDACTED)

        if sanitized != message:
            record.msg = sanitized
            record.args = ()
        return True


def _settings():
    """The central settings object, or None during early bootstrap."""
    try:
        parent_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        if parent_dir not in sys.path:
            sys.path.insert(0, parent_dir)
        from config import settings
        return settings
    except Exception:                                          # noqa: BLE001
        # Logging must work before (and without) configuration — a broken
        # config is precisely when you need the error reported.
        return None


def _resolve_log_settings(log_dir: Optional[str]):
    """(log_dir, level, file_level) from central config, env, or defaults."""
    cfg = _settings()
    file_level_name = ""
    if cfg is not None:
        # config.py IS the interface: pydantic already resolved LOG_DIR/LOG_LEVEL
        # from the environment, so reading os.getenv first would shadow the
        # settings object and make the setting unreachable at runtime. The env
        # reads below are the bootstrap fallback for when config cannot load.
        log_dir = log_dir or cfg.LOG_DIR
        level_name = cfg.LOG_LEVEL or os.getenv("LOG_LEVEL", "INFO")
        file_level_name = getattr(cfg, "LOG_FILE_LEVEL", "") or ""
    else:
        level_name = os.getenv("LOG_LEVEL", "INFO")
        log_dir = log_dir or os.getenv("LOG_DIR")
    log_dir = log_dir or "./logs"
    level = getattr(logging, str(level_name).upper(), logging.INFO)
    # Empty LOG_FILE_LEVEL means "identical to stdout", which is the default
    # and what makes the two sinks byte-for-byte comparable.
    file_level = getattr(logging, str(file_level_name).upper(), level) if file_level_name else level
    return log_dir, level, file_level


def log_file_name() -> str:
    cfg = _settings()
    return (getattr(cfg, "LOG_FILE_NAME", None) or "app.log") if cfg else "app.log"


def active_log_path() -> str:
    """THE configured log file — the one the logger writes and the API reads.

    Both ends call this, so they cannot drift apart. Returns the path that was
    actually opened when logging was configured; falls back to computing it
    from settings when called before setup (e.g. in a test process).
    """
    if _active_log_path:
        return _active_log_path
    log_dir, _level, _file_level = _resolve_log_settings(None)
    return os.path.join(log_dir, log_file_name())


def rotated_log_paths(max_files: Optional[int] = None) -> List[str]:
    """The configured log set, NEWEST FIRST: app.log, app.log.1, app.log.2, ...

    RotatingFileHandler names backups with ascending integers where `.1` is the
    most recently rotated, so ordering is deterministic and needs no stat().
    Only files that exist are returned. Nothing here accepts caller input, so
    there is no path a client can influence.
    """
    active = active_log_path()
    paths = [active] if os.path.isfile(active) else []
    cfg = _settings()
    backup_count = int(getattr(cfg, "LOG_BACKUP_COUNT", 5) or 5) if cfg else 5
    for index in range(1, backup_count + 1):
        candidate = f"{active}.{index}"
        if os.path.isfile(candidate):
            paths.append(candidate)
    if max_files is not None:
        paths = paths[:max_files]
    return paths


def setup_logging(
    log_to_file: bool = True,
    log_dir: Optional[str] = None,
    filename: str = "app.log",
    force: bool = False,
):
    """Configure application-wide logging (idempotent unless `force`).

    Console (stdout) -> Docker logs; optional rotating file for persistence.

    `force` tears the existing stack down and rebuilds it. Production never
    passes it: the idempotence guard is what stops a second import from
    double-attaching handlers and duplicating every record. It exists for the
    test suite, where tests/conftest.py configures logging at import time to
    isolate the log directory — without a way to rebuild, a test calling
    setup_logging(log_dir=tmp_path) gets the early return and then asserts
    against the SESSION listener, silently proving nothing about the argument
    it passed.
    """
    global _log_queue, _listener, _active_log_path

    if _listener:
        if not force:
            return  # already configured in this process — never double-attach
        try:
            _listener.stop()          # flush queued records before discarding
        except Exception:                                      # noqa: BLE001
            pass
        _listener = None
        _log_queue = None
        _active_log_path = None

    log_dir, level, file_level = _resolve_log_settings(log_dir)
    cfg = _settings()
    filename = filename or log_file_name()
    max_bytes = int(getattr(cfg, "LOG_MAX_BYTES", 10 * 1024 * 1024) or 10 * 1024 * 1024) if cfg else 10 * 1024 * 1024
    backup_count = int(getattr(cfg, "LOG_BACKUP_COUNT", 5) or 5) if cfg else 5

    root = logging.getLogger()
    root.setLevel(min(level, file_level))

    # Replace any pre-existing root handlers (e.g. from stray basicConfig calls)
    for handler in root.handlers[:]:
        root.removeHandler(handler)

    _log_queue = Queue(-1)
    queue_handler = logging.handlers.QueueHandler(_log_queue)
    # Redact in the producing thread, before the record ever reaches a handler.
    # Order matters: the context filter runs first so `request_id` exists on
    # every record by the time a formatter asks for it.
    queue_handler.addFilter(RequestContextFilter())
    queue_handler.addFilter(SensitiveDataFilter())
    root.addHandler(queue_handler)

    # ONE formatter instance, shared by both sinks — stdout and the file are
    # byte-identical for the same record, so the viewer parses one shape.
    formatter = logging.Formatter(LOG_FORMAT)
    handlers = []

    # Console -> stdout -> Docker logs
    console_handler = StdoutHandler()
    console_handler.setFormatter(formatter)
    # Also on the SINK, not only on the queue handler. `%(request_id)s` is in
    # the format string, so a record that reaches a sink without that attribute
    # makes the formatter raise, logging swallows it via handleError, and the
    # record is lost with no trace. Any path that bypasses the queue (a direct
    # emit, a library that attaches to the listener) must still format.
    console_handler.addFilter(RequestContextFilter())
    console_handler.setLevel(level)
    # stdout gets the match trace's SUMMARY lines only; the file gets all of it.
    console_handler.addFilter(UploadMatchConsoleFilter())
    handlers.append(console_handler)

    # The persistent copy: one bounded rotating file, sized from settings.
    if log_to_file:
        try:
            os.makedirs(log_dir, exist_ok=True)
            _active_log_path = os.path.join(log_dir, filename)
            file_handler = logging.handlers.RotatingFileHandler(
                _active_log_path,
                maxBytes=max_bytes,
                backupCount=backup_count,
                encoding="utf-8",
            )
            file_handler.setFormatter(formatter)
            file_handler.addFilter(RequestContextFilter())
            # Same level as stdout by default. LOG_FILE_LEVEL only differs when
            # an operator deliberately wants a verbose on-disk trace that would
            # drown `docker logs` — see config.py.
            file_handler.setLevel(file_level)
            handlers.append(file_handler)
        except OSError as e:
            _active_log_path = None
            print(f"[LOGGING] File logging disabled ({e}); console only", file=sys.stderr)

    _listener = logging.handlers.QueueListener(
        _log_queue, *handlers, respect_handler_level=True
    )
    _listener.start()
    atexit.register(_stop_listener)

    _configure_third_party_loggers(level, file_level)

    logging.getLogger(__name__).info(
        "✅ Logging initialized: level=%s file_level=%s console=stdout file=%s "
        "(max_bytes=%d backups=%d)",
        logging.getLevelName(level), logging.getLevelName(file_level),
        _active_log_path or "disabled", max_bytes, backup_count,
    )


def reinitialize_after_fork():
    """Re-arm logging in a freshly forked worker.

    A QueueListener runs in a THREAD, and threads do not survive fork(). With
    gunicorn `preload_app = True` (which this deployment turns on for GPU),
    setup_logging() runs in the master and each worker inherits a truthy
    `_listener` whose thread is dead — so the idempotence guard short-circuits,
    records pile up in a queue nobody drains, and the worker logs NOTHING.

    Called from gunicorn.conf.py's post_fork hook.

    NO-OP when this process never inherited a configuration (preload_app=False,
    the default for CPU deployments). In that case the worker imports the app
    itself and setup_logging() runs at import time — which is LATER than
    post_fork and therefore after gunicorn has finished wiring the worker's
    stdio. Re-arming here anyway would bind the console handler to a sys.stdout
    that gunicorn then replaces, and the worker's records would reach the
    rotating file but never `docker logs`.
    """
    global _log_queue, _listener, _active_log_path
    # Unconditional. Two reasons, both measured:
    #  * with preload_app=True the inherited listener's THREAD is dead, so the
    #    worker would log nothing at all;
    #  * with UvicornWorker, sys.stdout is only the container's stdout at this
    #    point — configuring later (at app import) produces a console handler
    #    whose writes never reach `docker logs`, while the file keeps working.
    _listener = None          # any inherited object's thread is gone
    _log_queue = None
    _active_log_path = None
    for handler in logging.getLogger().handlers[:]:
        logging.getLogger().removeHandler(handler)
    setup_logging(log_to_file=True)


# Stages of the [UPLOAD_MATCH] trace that belong on stdout (Docker logs).
# Everything else — including one line per raw candidate — stays in the
# rotating file, where it is greppable by request_id without drowning the
# container log on a busy system.
UPLOAD_MATCH_STDOUT_STAGES = frozenset({"request_completed", "request_failed"})


class UploadMatchConsoleFilter(logging.Filter):
    """Keep the per-stage match trace out of stdout, but not out of the file.

    Records are tagged by `_match_log` with `upload_match_stage` (a fixed enum
    string, never user data, never rendered into the message). A record without
    that attribute is not part of the trace and passes untouched, so this filter
    cannot affect any other logging.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        stage = getattr(record, "upload_match_stage", None)
        if stage is None:
            return True
        return stage in UPLOAD_MATCH_STDOUT_STAGES


def _configure_third_party_loggers(level: int, file_level: int = None):
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

    # The image-match trace emits two stages at DEBUG (raw_candidate,
    # candidate_filtered). Its logger is lowered explicitly so those records are
    # CREATED; the console filter above then keeps them off stdout while the
    # file handler records them. Scoped to this one module on purpose.
    # Only lower it when the FILE is configured to accept debug — otherwise the
    # records are created and then dropped by both handlers, which is pure cost.
    if file_level is not None and file_level <= logging.DEBUG:
        logging.getLogger("backend.core.advanced_search").setLevel(logging.DEBUG)

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

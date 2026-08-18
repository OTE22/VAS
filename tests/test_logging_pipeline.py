"""
One logging system, end to end
==============================
Run inside the api container:

    docker exec face_recognition_api python -m pytest tests/test_logging_pipeline.py -v

The production flow this file pins:

    all backend components -> central logger -> stdout + one rotating file
                                                       -> /api/logs -> admin-logs.js

`tests/test_logging_redaction.py` covers the redaction FILTER in isolation.
This file covers the PIPELINE: that every component reaches the same file, that
stdout carries the same records, that the API reads exactly the file the logger
writes, that rotated history is readable, and that the bounds and access rules
hold. Each test names the defect it prevents.
"""

import importlib
import io
import logging
import logging.handlers
import os
import re
import sys
import tempfile
from datetime import datetime

import pytest

import utils.logging as ulog
from utils.logging import (LOG_FORMAT, RequestContextFilter, SensitiveDataFilter,
                           active_log_path, rotated_log_paths, set_request_id)


# ---------------------------------------------------------------------------
# Harness: build the real handler stack over a temp dir, without touching the
# process-wide configuration the running app depends on.
# ---------------------------------------------------------------------------

class _Pipeline:
    """A console+file pair wired exactly as setup_logging wires them."""

    def __init__(self, directory, level=logging.INFO, file_level=None):
        self.dir = directory
        self.path = os.path.join(directory, "app.log")
        self.stream = io.StringIO()

        formatter = logging.Formatter(LOG_FORMAT)
        self.console = logging.StreamHandler(self.stream)
        self.console.setFormatter(formatter)
        self.console.setLevel(level)

        self.file = logging.handlers.RotatingFileHandler(
            self.path, maxBytes=2048, backupCount=3, encoding="utf-8")
        self.file.setFormatter(formatter)
        self.file.setLevel(file_level if file_level is not None else level)

        self.logger = logging.getLogger(f"pipeline_{id(self)}")
        self.logger.setLevel(logging.DEBUG)
        self.logger.propagate = False
        self.logger.handlers = []
        # Same filter order as the queue handler in setup_logging.
        for handler in (self.console, self.file):
            handler.addFilter(RequestContextFilter())
            handler.addFilter(SensitiveDataFilter())
            self.logger.addHandler(handler)

    def child(self, name):
        return self.logger.getChild(name)

    def flush(self):
        self.console.flush()
        self.file.flush()

    def file_text(self):
        self.flush()
        with open(self.path, encoding="utf-8") as handle:
            return handle.read()

    def console_text(self):
        self.flush()
        return self.stream.getvalue()

    def close(self):
        for handler in (self.console, self.file):
            handler.close()
        self.logger.handlers = []


@pytest.fixture
def pipeline(tmp_path):
    p = _Pipeline(str(tmp_path))
    yield p
    p.close()


# ---------------------------------------------------------------------------
# 1. Logs from API routes, the SQL agent and background jobs reach ONE file
# ---------------------------------------------------------------------------

def test_every_component_reaches_the_same_file(pipeline):
    """A route, the SQL agent and a background job share one destination.

    They are separate logger trees (`backend.routes.*`, `sql_agent.*`,
    `backend.core.*`); the point of central configuration is that none of them
    owns a handler and all of them land in the same place.
    """
    pipeline.child("backend.routes.identities").info("route line alpha")
    pipeline.child("sql_agent.tools.agent_tools").info("sql agent line beta")
    pipeline.child("backend.core.data_retention").info("background job line gamma")

    text = pipeline.file_text()
    assert "route line alpha" in text
    assert "sql agent line beta" in text
    assert "background job line gamma" in text
    for name in ("backend.routes.identities", "sql_agent.tools.agent_tools",
                 "backend.core.data_retention"):
        assert name in text, f"{name} did not reach the configured file"


def test_application_code_uses_module_loggers_not_private_handlers():
    """No application module may attach its own handler or call basicConfig.

    A private handler is how a component ends up writing somewhere the log
    viewer cannot see.
    """
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    offenders = []
    for root_dir in ("backend", "sql_agent"):
        for dirpath, _dirs, names in os.walk(os.path.join(repo, root_dir)):
            if "__pycache__" in dirpath:
                continue
            for name in names:
                if not name.endswith(".py"):
                    continue
                path = os.path.join(dirpath, name)
                with open(path, encoding="utf-8", errors="replace") as handle:
                    source = handle.read()
                relative = os.path.relpath(path, repo)
                for needle in ("logging.basicConfig(", ".addHandler(",
                               "logging.FileHandler(", "RotatingFileHandler("):
                    if needle in source:
                        offenders.append(f"{relative}: {needle}")
    assert not offenders, (
        "application modules must not configure logging themselves; "
        "utils/logging.py is the only place that may: " + "; ".join(offenders))


# ---------------------------------------------------------------------------
# 2. The same messages appear on stdout
# ---------------------------------------------------------------------------

def test_stdout_and_file_receive_the_same_record(pipeline):
    pipeline.child("backend.routes.health").warning("dual sink message")
    assert "dual sink message" in pipeline.console_text()
    assert "dual sink message" in pipeline.file_text()


def test_both_sinks_use_one_formatter(pipeline):
    """stdout and the file previously used DIFFERENT formats (the file carried
    threadName, stdout did not), so the viewer had to guess which shape it was
    parsing depending on where the line came from."""
    pipeline.child("backend.core.metrics").info("identical formatting")

    console_line = [l for l in pipeline.console_text().splitlines()
                    if "identical formatting" in l][0]
    file_line = [l for l in pipeline.file_text().splitlines()
                 if "identical formatting" in l][0]
    assert console_line == file_line

    assert pipeline.console.formatter._fmt == pipeline.file.formatter._fmt


def test_console_and_file_share_the_configured_level_by_default():
    """LOG_FILE_LEVEL defaults to empty, meaning 'same as LOG_LEVEL'."""
    log_dir, level, file_level = ulog._resolve_log_settings(None)
    from config import settings
    if not (settings.LOG_FILE_LEVEL or ""):
        assert level == file_level, (
            "with LOG_FILE_LEVEL unset the two sinks must run at one level")


# ---------------------------------------------------------------------------
# 3. Rotated files are read correctly
# ---------------------------------------------------------------------------

def test_rotated_files_are_discovered_newest_first(tmp_path, monkeypatch):
    """RotatingFileHandler names backups so `.1` is the MOST recent rotation.

    The previous log viewer never opened a rotated file at all — on the live
    system that hid tens of megabytes of history behind the last rotation.
    """
    active = tmp_path / "app.log"
    for name in ("app.log", "app.log.1", "app.log.2"):
        (tmp_path / name).write_text("x\n", encoding="utf-8")
    monkeypatch.setattr(ulog, "_active_log_path", str(active))

    paths = rotated_log_paths()
    assert [os.path.basename(p) for p in paths] == ["app.log", "app.log.1", "app.log.2"]

    assert len(rotated_log_paths(max_files=2)) == 2, "max_files must bound the set"


def test_rotation_preserves_history_and_the_api_reads_it(pipeline):
    """Write past maxBytes, then prove the rotated content is still reachable
    through the same helper the API uses."""
    from backend.routes.logs import _entries_from_text, _tail_bytes

    for index in range(200):
        pipeline.child("backend.core.batch_writer").info("rotation probe %03d", index)
    pipeline.flush()

    rotated = [p for p in os.listdir(pipeline.dir) if p.startswith("app.log.")]
    assert rotated, "the handler never rotated; the test wrote too little"

    # A ROTATED file must yield parseable records. (Records older than
    # backupCount rotations are gone by design — that is what the bound is
    # for — so the invariant is "rotated history is readable", not "nothing
    # is ever discarded".)
    rotated_entries = []
    for name in sorted(rotated):
        text, _read = _tail_bytes(os.path.join(pipeline.dir, name), 1024 * 1024)
        rotated_entries.extend(_entries_from_text(text, name))
    assert rotated_entries, "a rotated file yielded no parseable records"
    assert all(e.level == "INFO" for e in rotated_entries)
    assert any("rotation probe" in e.message for e in rotated_entries)

    # The active file and its rotated siblings together are what the API reads.
    active_entries = _entries_from_text(
        _tail_bytes(pipeline.path, 1024 * 1024)[0], "app.log")
    assert active_entries, "the active file yielded no records after rotation"


# ---------------------------------------------------------------------------
# 4. Secrets are redacted (in the file the API serves)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("message,secret", [
    ("user login password=SuperSecret123!", "SuperSecret123!"),
    ("Authorization: Bearer abcdef1234567890TOKEN", "abcdef1234567890TOKEN"),
    ("Cookie: access_token=zzzyyyxxxwwwvvv", "zzzyyyxxxwwwvvv"),
    ("dsn postgresql+asyncpg://appuser:s3cr3tpassword@postgres/db", "s3cr3tpassword"),
    ("api_key=abcd1234efgh5678ijkl", "abcd1234efgh5678ijkl"),
])
def test_secrets_never_reach_the_log_file(pipeline, message, secret):
    """Redaction runs on the PRODUCING side, so whatever the API later reads off
    disk is already sanitised — there is no second redaction pass to forget."""
    pipeline.child("backend.routes.auth").info(message)
    text = pipeline.file_text()
    assert secret not in text
    assert "REDACTED" in text
    assert secret not in pipeline.console_text()


def test_exception_tracebacks_survive_redaction(pipeline):
    """Redaction rewrites record.msg; it must not discard exc_info."""
    try:
        raise ValueError("boom with password=hunter2secret inside")
    except ValueError:
        pipeline.child("backend.core.identity_service").error(
            "operation failed", exc_info=True)

    text = pipeline.file_text()
    assert "Traceback (most recent call last)" in text
    assert "ValueError" in text
    assert "operation failed" in text


# ---------------------------------------------------------------------------
# 5. Non-admin access is rejected
# ---------------------------------------------------------------------------

def test_every_logs_route_requires_admin():
    import inspect
    from backend.routes import logs as logs_routes

    source = inspect.getsource(logs_routes)
    handlers = ("get_logs_config", "get_logs", "get_log_stats", "manual_log_cleanup")
    for name in handlers:
        function = getattr(logs_routes, name)
        signature = inspect.signature(function)
        assert "current_user" in signature.parameters, f"{name} has no auth dependency"
    assert source.count('require_role(["admin"])') >= len(handlers), (
        "every /api/logs route must be admin-only")


def test_logs_routes_set_no_store():
    import inspect
    from backend.routes import logs as logs_routes

    for name in ("get_logs_config", "get_logs", "get_log_stats", "manual_log_cleanup"):
        body = inspect.getsource(getattr(logs_routes, name))
        assert "Cache-Control" in body and "NO_STORE" in body, (
            f"{name} does not set Cache-Control: no-store")


# ---------------------------------------------------------------------------
# 6. The logger's output file and the API's input file are the same path
# ---------------------------------------------------------------------------

def test_writer_and_reader_resolve_the_identical_path():
    """The whole point of the consolidation.

    The API used to hard-code three basenames and read whichever was non-empty
    first (app.log, then error.log, then access.log) — three different formats,
    only one of them written by this application.
    """
    import inspect
    from backend.routes import logs as logs_routes

    writer_path = active_log_path()
    reader_paths = rotated_log_paths()
    assert reader_paths[:1] in ([writer_path], []), (
        "the API's first source is not the file the logger writes")

    source = inspect.getsource(logs_routes)
    for stale in ('"error.log"', '"access.log"', "'error.log'", "'access.log'"):
        assert stale not in source, (
            f"{stale} is still referenced; the API must read ONE configured set")


def test_the_api_never_accepts_a_path_from_a_client():
    """No filename, path or glob parameter may exist on any log route."""
    import inspect
    from backend.routes import logs as logs_routes

    for name in ("get_logs_config", "get_logs", "get_log_stats", "manual_log_cleanup"):
        params = inspect.signature(getattr(logs_routes, name)).parameters
        for banned in ("path", "file", "filename", "log_file", "directory"):
            assert banned not in params, f"{name} exposes a client-controlled {banned}"


def test_log_dir_is_mounted_persistently_in_every_stack():
    """Without a volume the rotating set lives in the container's writable
    layer, and `docker compose up -d` throws away the history.

    Checks the BASE stacks only. docker-compose.gpu.yml and prod.gpu.yml are
    hardware overlays layered with a second `-f`; they inherit every mount from
    their base and would fail a standalone check for a mount they are not
    supposed to restate. The companion test below makes that inheritance
    explicit rather than assumed.
    """
    import yaml
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for name in ("docker-compose.cpu.yml", "docker-compose.prod.yml"):
        path = os.path.join(repo, "docker", name)
        if not os.path.exists(path):
            continue
        spec = yaml.safe_load(open(path, encoding="utf-8"))
        services = spec.get("services") or {}
        api = services.get("face_recognition") or services.get("api")
        assert api, f"{name}: no api service found"
        mounts = [str(v) for v in (api.get("volumes") or [])]
        assert any("/var/log/face-recognition" in m for m in mounts), (
            f"{name} does not mount LOG_DIR persistently")


def test_the_gpu_overlays_do_not_replace_the_log_mount():
    """Compose merges `volumes` by TARGET path, so an overlay that declared its
    own /var/log/face-recognition entry would silently replace the base's —
    turning a named volume into whatever the overlay said. The overlays must
    not mention the log directory at all."""
    import yaml
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for name in ("docker-compose.gpu.yml", "docker-compose.prod.gpu.yml"):
        path = os.path.join(repo, "docker", name)
        if not os.path.exists(path):
            continue
        spec = yaml.safe_load(open(path, encoding="utf-8"))
        api = (spec.get("services") or {}).get("face_recognition") or {}
        mounts = [str(v) for v in (api.get("volumes") or [])]
        assert not any("/var/log/face-recognition" in m for m in mounts), (
            f"{name} restates the log mount; it must inherit it from the base "
            f"stack, or the two can drift apart")


# ---------------------------------------------------------------------------
# 7. No duplicate handlers cause repeated lines
# ---------------------------------------------------------------------------

def test_a_record_is_written_once_per_sink(pipeline):
    pipeline.child("backend.core.face_tracker").info("exactly once please")
    assert pipeline.file_text().count("exactly once please") == 1
    assert pipeline.console_text().count("exactly once please") == 1


def test_root_has_exactly_one_queue_handler():
    """setup_logging is idempotent — a second call must not double-attach."""
    setup = ulog.setup_logging
    setup()
    setup()
    queue_handlers = [h for h in logging.getLogger().handlers
                      if isinstance(h, logging.handlers.QueueHandler)]
    assert len(queue_handlers) == 1, (
        f"expected 1 QueueHandler on root, found {len(queue_handlers)}")


def test_application_loggers_do_not_hold_private_handlers():
    ulog.setup_logging()
    for name in ("backend", "sql_agent"):
        assert not logging.getLogger(name).handlers, (
            f"{name} holds its own handler, which duplicates or diverts output")
        assert logging.getLogger(name).propagate is True


# ---------------------------------------------------------------------------
# 8. Changing configuration changes real behaviour, with no frontend edit
# ---------------------------------------------------------------------------

def test_log_dir_setting_moves_the_file(monkeypatch, tmp_path):
    """The SETTING decides, not a raw os.getenv read.

    The resolver used to check os.environ["LOG_DIR"] before settings.LOG_DIR.
    Since pydantic already resolves LOG_DIR from the environment, that ordering
    only had one effect: it shadowed the settings object and made the value
    unreachable at runtime.
    """
    monkeypatch.setattr(ulog, "_active_log_path", None)
    from config import settings
    monkeypatch.setattr(settings, "LOG_DIR", str(tmp_path), raising=False)
    assert active_log_path() == os.path.join(str(tmp_path), settings.LOG_FILE_NAME)


def test_log_file_name_setting_changes_the_target(monkeypatch, tmp_path):
    monkeypatch.setattr(ulog, "_active_log_path", None)
    from config import settings
    monkeypatch.setattr(settings, "LOG_DIR", str(tmp_path), raising=False)
    monkeypatch.setattr(settings, "LOG_FILE_NAME", "service.log", raising=False)
    assert os.path.basename(active_log_path()) == "service.log"


def test_rotation_size_and_backup_count_come_from_settings():
    import inspect
    source = inspect.getsource(ulog.setup_logging)
    assert "LOG_MAX_BYTES" in source and "LOG_BACKUP_COUNT" in source, (
        "rotation is hard-coded rather than configured")
    assert "10 * 1024 * 1024," not in source.split("max_bytes =")[-1].split("\n")[1:2], (
        "maxBytes must come from the setting, not a literal"
    )


def test_page_size_and_levels_come_from_settings_not_javascript():
    """The frontend must hold no page-size or level literals."""
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    js = open(os.path.join(repo, "frontend", "js", "admin-logs.js"),
              encoding="utf-8").read()
    code = "\n".join(line for line in js.splitlines()
                     if not line.strip().startswith("//") and not line.strip().startswith("*"))

    assert "currentPageSize = 50" not in code
    assert "'50'" not in code and '"50"' not in code, (
        "a page size is still hard-coded in admin-logs.js")
    assert "/api/logs/config" in code, (
        "the viewer does not load its bounds from the backend")

    html = open(os.path.join(repo, "frontend", "admin", "logs.html"),
                encoding="utf-8").read()
    assert '<option value="50"' not in html, "logs.html still hard-codes a page size"
    assert '<option value="DEBUG"' not in html, "logs.html still hard-codes the level list"


def test_api_rejects_a_page_size_above_the_configured_maximum():
    import inspect
    from backend.routes import logs as logs_routes

    source = inspect.getsource(logs_routes.get_logs)
    assert "LOG_API_MAX_PAGE_SIZE" in source
    assert "422" in source or "HTTP_422" in source, (
        "an over-limit page_size must be rejected, not silently clamped")


def test_scan_bounds_are_configured():
    import inspect
    from backend.routes import logs as logs_routes

    source = inspect.getsource(logs_routes)
    for setting in ("LOG_API_MAX_SCAN_FILES", "LOG_API_MAX_SCAN_BYTES",
                    "LOG_API_TIMEOUT_SECONDS"):
        assert setting in source, f"{setting} is not enforced by the logs API"


def test_missing_log_source_is_an_error_not_an_empty_success(monkeypatch, tmp_path):
    """A missing directory used to return 200 with an empty list, which is
    indistinguishable from a quiet system."""
    from fastapi import HTTPException
    from backend.routes import logs as logs_routes

    missing = str(tmp_path / "definitely-not-there")
    monkeypatch.setattr(ulog, "_active_log_path", os.path.join(missing, "app.log"))

    with pytest.raises(HTTPException) as excinfo:
        logs_routes._require_log_source()
    assert excinfo.value.status_code == 503
    assert "not available" in str(excinfo.value.detail).lower()


# ---------------------------------------------------------------------------
# Parser: the format the logger emits must be the format the API parses
# ---------------------------------------------------------------------------

def test_the_api_parses_the_format_the_logger_emits(pipeline):
    """All three previous regexes rejected the real format, so every line fell
    to a keyword heuristic that stamped entries with `datetime.utcnow()` —
    which is why date filtering never worked."""
    from backend.routes.logs import _entries_from_text

    set_request_id("abc123def456")
    try:
        pipeline.child("backend.routes.search").warning("parser round trip")
    finally:
        set_request_id(None)

    entries = _entries_from_text(pipeline.file_text(), "app.log")
    match = [e for e in entries if "parser round trip" in e.message]
    assert match, "the API could not parse a line this logger just wrote"

    entry = match[0]
    assert entry.level == "WARNING"
    assert entry.logger_name.endswith("backend.routes.search")
    assert entry.process_id == str(os.getpid())
    assert entry.request_id == "abc123def456"
    assert entry.message == "parser round trip", "metadata leaked into the message"
    # A REAL timestamp, not the time of the request.
    assert datetime.strptime(entry.timestamp, "%Y-%m-%d %H:%M:%S,%f")


def test_traceback_lines_attach_to_their_record(pipeline):
    """Continuation lines used to be dropped — measured at 329 of 417
    non-conforming lines in a live sample, i.e. most of every traceback."""
    from backend.routes.logs import _entries_from_text

    try:
        raise RuntimeError("attached failure")
    except RuntimeError:
        pipeline.child("backend.core.model_manager").error("job died", exc_info=True)

    entries = _entries_from_text(pipeline.file_text(), "app.log")
    owner = [e for e in entries if "job died" in e.message]
    assert owner, "the record carrying the traceback was not parsed"
    assert "Traceback" in owner[0].message, "the traceback detached from its record"
    assert "RuntimeError" in owner[0].message


def test_request_id_is_present_on_records_outside_a_request(pipeline):
    """A background job has no request; the field must still resolve or the
    formatter raises and the record is silently dropped."""
    from backend.routes.logs import _entries_from_text

    set_request_id(None)
    pipeline.child("backend.core.log_cleanup").info("no request in scope")
    entries = _entries_from_text(pipeline.file_text(), "app.log")
    match = [e for e in entries if "no request in scope" in e.message]
    assert match and match[0].request_id in (None, "-")


def test_log_cleanup_never_rewrites_the_active_file():
    """It used to read app.log, filter the lines and rewrite it with open('w')
    while the RotatingFileHandler held the same file open at its own offset."""
    import inspect
    from backend.core.log_cleanup import LogCleanupManager

    from tests._repo_scan import strip_comments_and_docstrings
    source = strip_comments_and_docstrings(
        inspect.getsource(LogCleanupManager.cleanup_old_logs))
    assert "'w'" not in source and '"w"' not in source, (
        "log cleanup still opens a log file for writing")
    assert "active_log_path" in source, (
        "cleanup does not exclude the active file from deletion")


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))


# ---------------------------------------------------------------------------
# Log QUALITY: an ERROR must mean something an operator can act on
# ---------------------------------------------------------------------------

def test_client_disconnect_is_not_logged_as_an_error():
    """A browser refreshing mid-handshake is normal traffic, not a fault.

    `websocket.accept()` raising ClientDisconnected was logged at ERROR with a
    full stack trace in BOTH `websocket_manager.connect` and the route that
    re-raised it — two error records with two tracebacks per refresh. That is
    how an error log becomes something people stop reading.
    """
    import inspect
    from backend.core import websocket_manager
    from backend.routes import websocket as websocket_route

    for module in (websocket_manager, websocket_route):
        source = inspect.getsource(module)
        assert "_CLIENT_GONE" in source, (
            f"{module.__name__} does not distinguish a client disconnect "
            "from a server fault")

    # The route must return rather than fall through to the generic handler,
    # so a disconnect produces no second record and no close() on a dead socket.
    route_source = inspect.getsource(websocket_route.websocket_endpoint)
    disconnect_branch = route_source.split("except _CLIENT_GONE")[1].split("except Exception")[0]
    assert "logger.debug" in disconnect_branch
    assert "exc_info" not in disconnect_branch
    assert "return" in disconnect_branch


def test_client_gone_tuple_survives_a_missing_optional_import():
    """ClientDisconnected is uvicorn-internal and may move between versions;
    losing it must degrade the classification, never break the server."""
    from backend.core.websocket_manager import _CLIENT_GONE

    assert ConnectionResetError in _CLIENT_GONE
    assert BrokenPipeError in _CLIENT_GONE
    assert len(_CLIENT_GONE) >= 3, "the disconnect classification is empty"


def test_a_null_bbox_does_not_destroy_the_dashboard_payload():
    """`Face.bbox_x1..y2` are nullable=True, so NULL is a legal row state.

    `float(face.bbox_x1)` on such a row raised TypeError inside the
    initial-data loop, and the handler that caught it sits ~370 lines away —
    so one malformed face discarded the ENTIRE payload and the operator got an
    empty dashboard plus "Error loading initial_data".
    """
    from types import SimpleNamespace
    from backend.routes.websocket import _bbox

    complete = SimpleNamespace(bbox_x1=1, bbox_y1=2, bbox_x2=3, bbox_y2=4)
    assert _bbox(complete) == [1.0, 2.0, 3.0, 4.0]

    # Any missing corner yields None rather than raising.
    for missing in ("bbox_x1", "bbox_y1", "bbox_x2", "bbox_y2"):
        row = SimpleNamespace(bbox_x1=1, bbox_y1=2, bbox_x2=3, bbox_y2=4)
        setattr(row, missing, None)
        assert _bbox(row) is None, f"a NULL {missing} must not raise"

    # Junk is also survivable — the payload matters more than one box.
    assert _bbox(SimpleNamespace(bbox_x1="x", bbox_y1=2, bbox_x2=3, bbox_y2=4)) is None


def test_no_unguarded_bbox_float_conversion_remains():
    from tests._repo_scan import strip_comments_and_docstrings
    from backend.routes import websocket as ws

    source = strip_comments_and_docstrings(open(ws.__file__, encoding="utf-8").read())
    assert "float(face.bbox_x1)" not in source, (
        "an unguarded bbox conversion is back; use _bbox()")


# ---------------------------------------------------------------------------
# Test-log isolation: a test run must never write to the production log
# ---------------------------------------------------------------------------

def _wait_for_marker(marker: str, timeout: float = 5.0) -> str:
    """Read the session log once `marker` appears, or time out.

    The QueueListener drains in a THREAD, so a record logged on this thread is
    not on disk the instant the call returns. Flushing the handlers does not
    help — the record may still be in the queue. Poll instead of sleeping a
    fixed amount, which is both faster and not flaky under load.
    """
    import time
    deadline = time.monotonic() + timeout
    path = active_log_path()
    while time.monotonic() < deadline:
        listener = getattr(ulog, "_listener", None)
        if listener is not None:
            for handler in listener.handlers:
                try:
                    handler.flush()
                except Exception:                              # noqa: BLE001
                    pass
        try:
            with open(path, encoding="utf-8") as handle:
                text = handle.read()
            if marker in text:
                return text
        except OSError:
            pass
        time.sleep(0.05)
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def test_logging_is_isolated_from_the_production_directory():
    """The guard for the defect that produced four false investigations.

    Tests deliberately corrupt snapshots and configure unusable backends to
    prove the code rejects them. Those ERROR/CRITICAL records used to land in
    the same rotating file GET /api/logs serves, describing faults that never
    happened — including "Searches are being served by a DIFFERENT backend than
    configured" on a deployment running pgvector/flat.
    """
    import conftest

    resolved = os.path.realpath(active_log_path())
    session_dir = os.path.realpath(conftest.TEST_LOG_DIR)
    production = os.path.realpath(conftest.PRODUCTION_LOG_DIR)

    assert os.path.commonpath([resolved, session_dir]) == session_dir, (
        f"test logging resolved to {resolved}, outside the session directory")
    if os.path.isdir(production):
        assert os.path.commonpath([resolved, production]) != production, (
            f"test logging resolved INSIDE the production log directory {production}")


def test_deliberate_error_and_critical_records_stay_in_the_session_file():
    """An ERROR and a CRITICAL emitted here must be in the temp file and
    absent from the production file."""
    import conftest

    marker = f"qa-isolation-probe-{os.getpid()}"
    logging.getLogger("backend.core.vector_index.factory").error(
        "[VECTOR_INDEX] %s deliberate error", marker)
    logging.getLogger("backend.core.vector_index.factory").critical(
        "[VECTOR_INDEX] %s deliberate critical", marker)

    session_text = _wait_for_marker(f"{marker} deliberate critical")
    assert session_text.count(marker) >= 2, "records did not reach the session log"

    production_file = os.path.join(conftest.PRODUCTION_LOG_DIR, "app.log")
    if os.path.isfile(production_file):
        with open(production_file, encoding="utf-8", errors="replace") as handle:
            assert marker not in handle.read(), (
                "a test record reached the PRODUCTION log")


def test_isolation_does_not_duplicate_records():
    """Re-asserting isolation must not stack a second set of handlers."""
    marker = f"qa-dup-probe-{os.getpid()}"
    logging.getLogger("backend.core.metrics").info("dup check %s", marker)

    assert _wait_for_marker(marker).count(marker) == 1, (
        "the record appears more than once — duplicate handlers are attached")

    queue_handlers = [h for h in logging.getLogger().handlers
                      if isinstance(h, logging.handlers.QueueHandler)]
    assert len(queue_handlers) == 1, (
        f"expected exactly 1 QueueHandler on root, found {len(queue_handlers)}")

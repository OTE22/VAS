"""
Logging pipeline tests
======================
Run inside the api container:

    docker exec face_recognition_api python -m pytest tests/test_logging_redaction.py -v

Proves:
  * the SensitiveDataFilter redacts every category of secret before any
    handler receives the record (console AND file get sanitized text)
  * setup_logging() is idempotent and console output goes to stdout
  * safe operational fields (usernames, ips, request ids) are NOT redacted
"""

import io
import logging
import os
import sys

import pytest

from utils.logging import SensitiveDataFilter, setup_logging
import utils.logging as ulog


def _redact(message: str) -> str:
    """Run a message through the filter exactly as the logging pipeline does."""
    record = logging.LogRecord(
        name="test", level=logging.INFO, pathname=__file__, lineno=1,
        msg=message, args=(), exc_info=None,
    )
    SensitiveDataFilter().filter(record)
    return record.getMessage()


# ---------------------------------------------------------------------------
# Redaction: every sensitive category must disappear
# ---------------------------------------------------------------------------

FAKE_JWT = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIiwibmFtZSI6ImFkbWluIn0.abc123DEF456ghi789JKL"


@pytest.mark.parametrize("message,secret", [
    ("login body password=SuperSecret123!", "SuperSecret123!"),
    ('{"password": "hunter2xyz"}', "hunter2xyz"),
    ("Authorization: Bearer abcdef1234567890TOKEN", "abcdef1234567890TOKEN"),
    ("headers={'authorization': 'Basic dXNlcjpwYXNz'}", "dXNlcjpwYXNz"),
    ("Cookie: access_token=sekrit-cookie-value-123", "sekrit-cookie-value-123"),
    ("Set-Cookie: session=deadbeefcafe999", "deadbeefcafe999"),
    (f"token verified: {FAKE_JWT}", FAKE_JWT),
    ("access_token=tok_live_9f8e7d6c5b4a", "tok_live_9f8e7d6c5b4a"),
    ("refresh_token: rt_0123456789abcdef", "rt_0123456789abcdef"),
    ("api_key=sk-proj-FAKEKEY123456", "sk-proj-FAKEKEY123456"),
    ("connecting to postgresql+asyncpg://postgres:dbpass123@postgres:5432/face", "dbpass123"),
    ("redis url redis://default:redispass456@redis:6379/0", "redispass456"),
    ("embedding=[0.123, -0.456, 0.789, 0.111, -0.222, 0.333, 0.444, -0.555, "
     "0.666, 0.777, -0.888, 0.999, 0.101, -0.202, 0.303]",
     "0.789"),
])
def test_secret_is_redacted(message, secret):
    sanitized = _redact(message)
    assert secret not in sanitized, f"secret leaked through filter: {sanitized}"
    assert "REDACTED" in sanitized


def test_safe_fields_survive():
    msg = "[AUTH] Login failed user=admin reason=invalid_credentials ip=172.22.0.1 request_id=abc123 status=401"
    assert _redact(msg) == msg  # nothing here is secret — must pass unmodified


def test_normal_logs_untouched():
    msg = "[WEBHOOK] request_id=abc123 pipeline_id=camera-1 POST /webhook/camera-1 -> 202 in 0.012s"
    assert _redact(msg) == msg


def test_small_number_lists_not_treated_as_embeddings():
    # bboxes and short arrays must NOT be redacted
    msg = "bbox=[0.1, 0.2, 0.3, 0.4] landmarks 5 points"
    assert _redact(msg) == msg


# ---------------------------------------------------------------------------
# Configuration behavior
# ---------------------------------------------------------------------------

# These four build a REAL stack via the `fresh_logging` fixture rather than
# calling setup_logging() directly. tests/conftest.py configures logging at
# import time to keep test records out of the production file, so a bare
# setup_logging(log_dir=tmp_path) hits the idempotence guard, returns early,
# and leaves the test asserting against the session listener — passing without
# ever exercising the argument it passed. The fixture forces a rebuild and
# restores the isolated session logger afterwards.

def test_setup_logging_idempotent(tmp_path, fresh_logging):
    fresh_logging(tmp_path)
    first_listener = ulog._listener
    first_handlers = list(logging.getLogger().handlers)

    setup_logging(log_to_file=True, log_dir=str(tmp_path))  # no force = no-op
    assert ulog._listener is first_listener
    assert logging.getLogger().handlers == first_handlers


def test_root_has_single_queue_handler_with_redaction(tmp_path, fresh_logging):
    fresh_logging(tmp_path)
    root = logging.getLogger()
    queue_handlers = [h for h in root.handlers
                      if isinstance(h, logging.handlers.QueueHandler)]
    assert len(queue_handlers) == 1, "exactly one queue handler on root"
    assert any(isinstance(f, SensitiveDataFilter) for f in queue_handlers[0].filters)


def test_console_handler_targets_stdout(tmp_path, fresh_logging):
    fresh_logging(tmp_path)
    listener_handlers = ulog._listener.handlers
    # isinstance, not `type(...) is`: the console sink is the StdoutHandler
    # subclass. The invariant under test is "a console handler writing to
    # stdout", not which class implements it. RotatingFileHandler is also a
    # StreamHandler subclass, so it is excluded explicitly.
    stream_handlers = [h for h in listener_handlers
                       if isinstance(h, logging.StreamHandler)
                       and not isinstance(h, logging.FileHandler)]
    assert stream_handlers, "listener must include a console StreamHandler"
    # The handler binds sys.stdout when constructed. Building it HERE, inside
    # the test, is what makes this identity check mean something: a handler
    # built at conftest import time holds the real fd 1 while pytest has since
    # replaced sys.stdout with its capture object, and the two never match.
    assert stream_handlers[0].stream is sys.stdout
    assert stream_handlers[0].stream is not sys.stderr


def test_file_handler_is_rotating(tmp_path, fresh_logging):
    fresh_logging(tmp_path)
    rotating = [h for h in ulog._listener.handlers
                if isinstance(h, logging.handlers.RotatingFileHandler)]
    assert rotating, "persistent file handler must be rotation-bounded"
    assert rotating[0].maxBytes > 0 and rotating[0].backupCount > 0
    # Proves the rebuild honoured log_dir. Without this the test passed against
    # whatever directory the session listener already pointed at.
    assert os.path.realpath(rotating[0].baseFilename).startswith(
        os.path.realpath(str(tmp_path)))


def test_secret_never_reaches_handler_output(tmp_path):
    """End-to-end: log a secret through a real handler, capture the output."""
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter("%(message)s"))

    lg = logging.getLogger("redaction.e2e")
    lg.setLevel(logging.INFO)
    lg.propagate = False
    lg.addFilter(SensitiveDataFilter())
    lg.addHandler(handler)
    try:
        lg.info("login attempt password=TopSecret99 Authorization: Bearer aaaabbbbccccdddd1234")
        handler.flush()
        output = stream.getvalue()
        assert "TopSecret99" not in output
        assert "aaaabbbbccccdddd1234" not in output
        assert "REDACTED" in output
    finally:
        lg.removeHandler(handler)


def test_fresh_logging_fixture_restored_session_isolation(session_log_dir):
    """The `fresh_logging` tests above must not leave logging in their tmp_path.

    Placed last in this module ON PURPOSE, and it cannot be moved to the
    pipeline suite: in a full run `test_logging_pipeline` sorts BEFORE
    `test_logging_redaction`, so the isolation guards there execute before the
    fixture is ever used and would never observe a leak. pytest deletes the
    tmp_path directories, so a failed restore means every subsequent test in
    the session logs into a directory that no longer exists.

    Takes the directory from the `session_log_dir` FIXTURE. An earlier version
    did `import tests.conftest`, which re-executed that module, re-ran the
    isolation it was checking, and therefore passed even with the restore
    deleted — a guard that could not fail.
    """
    active = os.path.realpath(ulog.active_log_path())
    session = os.path.realpath(session_log_dir)
    assert active.startswith(session), (
        f"logging was left at {active!r} instead of the session directory "
        f"{session!r} — the fresh_logging fixture did not restore isolation")
    assert os.path.isdir(os.path.dirname(active)), (
        "the active log directory no longer exists on disk")


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))

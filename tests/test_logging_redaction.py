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

def test_setup_logging_idempotent(tmp_path):
    setup_logging(log_to_file=True, log_dir=str(tmp_path))
    first_listener = ulog._listener
    first_handlers = list(logging.getLogger().handlers)

    setup_logging(log_to_file=True, log_dir=str(tmp_path))  # second call = no-op
    assert ulog._listener is first_listener
    assert logging.getLogger().handlers == first_handlers


def test_root_has_single_queue_handler_with_redaction(tmp_path):
    setup_logging(log_to_file=True, log_dir=str(tmp_path))
    root = logging.getLogger()
    queue_handlers = [h for h in root.handlers
                      if isinstance(h, logging.handlers.QueueHandler)]
    assert len(queue_handlers) == 1, "exactly one queue handler on root"
    assert any(isinstance(f, SensitiveDataFilter) for f in queue_handlers[0].filters)


def test_console_handler_targets_stdout(tmp_path):
    setup_logging(log_to_file=True, log_dir=str(tmp_path))
    listener_handlers = ulog._listener.handlers
    stream_handlers = [h for h in listener_handlers
                       if type(h) is logging.StreamHandler]
    assert stream_handlers, "listener must include a console StreamHandler"
    assert stream_handlers[0].stream is sys.stdout


def test_file_handler_is_rotating(tmp_path):
    setup_logging(log_to_file=True, log_dir=str(tmp_path))
    rotating = [h for h in ulog._listener.handlers
                if isinstance(h, logging.handlers.RotatingFileHandler)]
    assert rotating, "persistent file handler must be rotation-bounded"
    assert rotating[0].maxBytes > 0 and rotating[0].backupCount > 0


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


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))

"""
Shared test infrastructure.

asyncpg connections are LOOP-BOUND: the pooled engine binds to the event
loop that first initialized it. If every test module created its own loop,
the second module's helpers would fail with "attached to a different loop".
All async test helpers must therefore run on THIS single shared loop.

This module ALSO redirects logging to a throwaway directory before anything
else is imported — see `_isolate_test_logging()`.
"""

# ---------------------------------------------------------------------------
# Log isolation. This block runs FIRST, before any project import, on purpose.
#
# Tests import modules that call setup_logging(), and LOG_DIR resolves to the
# real /var/log/face-recognition — so a test run wrote into the SAME rotating
# file GET /api/logs serves. That is not noise, it is false alarms: the suite
# deliberately corrupts snapshots and configures unusable backends to prove the
# code rejects them, so it emits ERROR and CRITICAL records describing faults
# that never happened. One of them reads "Searches are being served by a
# DIFFERENT backend than configured" while the deployment runs pgvector/flat.
# Four separate investigations in one session started from records like that.
#
# Only the DESTINATION is redirected. The handler stack stays real — same
# formatter, same filters, a real RotatingFileHandler — so the pipeline tests
# still exercise production behaviour.
# ---------------------------------------------------------------------------

import atexit
import os
import shutil
import sys
import tempfile

# The production directory, captured BEFORE anything can change it. Kept so the
# verification below can prove we are not writing there.
PRODUCTION_LOG_DIR = os.environ.get("LOG_DIR") or "/var/log/face-recognition"

TEST_LOG_DIR = tempfile.mkdtemp(prefix="qa-logs-")

# Set the environment BEFORE importing config or utils.logging: pydantic reads
# LOG_DIR at Settings construction, and utils.logging falls back to the env when
# config is unavailable. Both paths must point at the temp dir.
os.environ["LOG_DIR"] = TEST_LOG_DIR

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)


class LogIsolationError(RuntimeError):
    """Raised at collection time when tests would write to the production log."""


def _isolate_test_logging() -> str:
    """Point the real logging stack at TEST_LOG_DIR, then PROVE it landed there.

    Raises LogIsolationError on any failure. It deliberately does not degrade:
    falling back to the production log is the exact outcome this exists to
    prevent, and a silent fallback would be indistinguishable from success.
    """
    import logging

    from config import settings
    settings.LOG_DIR = TEST_LOG_DIR

    import utils.logging as ulog

    # Stop and flush anything already installed, then clear ONLY the state this
    # application owns. Without the reset, setup_logging's idempotence guard
    # returns a listener still bound to the production file.
    if getattr(ulog, "_listener", None) is not None:
        try:
            ulog._listener.stop()
        except Exception:                                      # noqa: BLE001
            pass
    ulog._listener = None
    ulog._log_queue = None
    ulog._active_log_path = None

    root = logging.getLogger()
    for handler in root.handlers[:]:
        try:
            handler.flush()
            handler.close()
        except Exception:                                      # noqa: BLE001
            pass
        root.removeHandler(handler)

    ulog.setup_logging(log_to_file=True, log_dir=TEST_LOG_DIR)

    # --- verification: the whole point of this function -------------------
    resolved = os.path.realpath(ulog.active_log_path())
    temp_root = os.path.realpath(TEST_LOG_DIR)
    prod_root = os.path.realpath(PRODUCTION_LOG_DIR)

    if os.path.commonpath([resolved, temp_root]) != temp_root:
        raise LogIsolationError(
            f"test logging resolved to {resolved!r}, which is outside the "
            f"session directory {temp_root!r}")
    if os.path.isdir(prod_root) and os.path.commonpath([resolved, prod_root]) == prod_root:
        raise LogIsolationError(
            f"test logging resolved INSIDE the production log directory "
            f"{prod_root!r} — refusing to run")
    if not os.access(os.path.dirname(resolved), os.W_OK):
        raise LogIsolationError(f"test log directory {resolved!r} is not writable")

    logging.getLogger(__name__).info(
        "[QA] test logging isolated to %s (production %s untouched)",
        resolved, prod_root)
    return resolved


def _teardown_test_logging():
    """Stop the listener, close handlers, and remove the temp logs.

    Set QA_KEEP_TEST_LOGS=1 to retain them for failure diagnosis. No production
    logger is restored inside the pytest process — this process must never
    write to the production log, including during teardown.
    """
    import logging
    try:
        import utils.logging as ulog
        if getattr(ulog, "_listener", None) is not None:
            ulog._listener.stop()
            ulog._listener = None
    except Exception:                                          # noqa: BLE001
        pass
    for handler in logging.getLogger().handlers[:]:
        try:
            handler.flush()
            handler.close()
        except Exception:                                      # noqa: BLE001
            pass
        logging.getLogger().removeHandler(handler)
    if not os.environ.get("QA_KEEP_TEST_LOGS"):
        shutil.rmtree(TEST_LOG_DIR, ignore_errors=True)


# Abort the whole run rather than let one record reach production.
ACTIVE_TEST_LOG = _isolate_test_logging()
atexit.register(_teardown_test_logging)


import asyncio                                                 # noqa: E402

import pytest                                                  # noqa: E402

SHARED_LOOP = asyncio.new_event_loop()


def run_on_shared_loop(coro):
    return SHARED_LOOP.run_until_complete(coro)


def pytest_configure(config):
    # Re-assert the invariant: a module imported since conftest loaded may have
    # reconfigured logging. Aborts the run rather than let a record through.
    resolved = os.path.realpath(_isolate_test_logging())
    if os.path.commonpath([resolved, os.path.realpath(TEST_LOG_DIR)]) !=             os.path.realpath(TEST_LOG_DIR):
        raise LogIsolationError(f"logging escaped to {resolved!r} after collection")

    config.addinivalue_line(
        "markers",
        "benchmark: scale/performance measurement. Deselected by default "
        "because it builds a 100k-vector index (~200 MB, tens of seconds). "
        "Run explicitly with `-m benchmark`; passing its budget is mandatory "
        "for a release-readiness verdict.")


@pytest.fixture
def session_log_dir():
    """The session's isolated log directory.

    A fixture, not `import tests.conftest`: importing this module by name
    re-EXECUTES it under a second module object, which mints a fresh mkdtemp
    AND re-runs _isolate_test_logging(). A test doing that to read TEST_LOG_DIR
    silently repairs the isolation it was trying to check, and passes no matter
    what. Fixtures resolve against the module pytest already loaded.
    """
    return TEST_LOG_DIR


@pytest.fixture
def fresh_logging():
    """Build a REAL fresh logging stack at a given directory, then re-isolate.

    Two hazards this exists to close, both created by configuring logging at
    conftest import time:

    1. setup_logging() returns early when a listener exists, so a test calling
       setup_logging(log_dir=tmp_path) inspected the SESSION listener and
       asserted nothing about the directory it passed. `force=True` rebuilds.
    2. A forced rebuild leaves the process logging into tmp_path, which pytest
       deletes — every later test would then write into a removed directory.
       Re-isolating on teardown is not tidiness, it keeps the session invariant
       ("never the production log") true for the rest of the run.
    """
    import utils.logging as ulog

    def _configure(log_dir, **kwargs):
        ulog.setup_logging(log_to_file=True, log_dir=str(log_dir),
                           force=True, **kwargs)
        return ulog

    yield _configure
    _isolate_test_logging()


def pytest_collection_modifyitems(config, items):
    """Keep benchmarks out of ordinary runs unless asked for by name or marker.

    Excluded from CI, but never silently skipped when someone runs `-m
    benchmark` — the verdict depends on those numbers actually being measured.
    """
    if config.getoption("-m"):          # an explicit marker expression wins
        return
    skip = pytest.mark.skip(reason="benchmark: run with `-m benchmark`")
    for item in items:
        if "benchmark" in item.keywords:
            item.add_marker(skip)


def pytest_sessionfinish(session, exitstatus):
    """Flush and dispose of the session's logging before the process exits."""
    _teardown_test_logging()

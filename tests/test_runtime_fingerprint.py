"""The runtime fingerprint: drift between repository and runtime must be
visible from one log line or one curl.

    docker exec face_recognition_api python -m pytest tests/test_runtime_fingerprint.py -v

Two real incidents motivated this: a container whose env was three days older
than the compose file, and an image older than the Dockerfile step it was
assumed to contain. Both were invisible because nothing running stated its own
provenance.
"""

import json
import re
import urllib.request

BASE = "http://localhost:8000"

REQUIRED_KEYS = {
    "version", "git_commit", "environment", "container", "vector_backend",
    "database", "migrations_mode", "expected_migration_head", "workers", "gpu",
}


def _runtime():
    with urllib.request.urlopen(BASE + "/health/detailed", timeout=60) as response:
        return json.loads(response.read())["runtime"]


def test_health_detailed_carries_the_full_fingerprint():
    runtime = _runtime()
    missing = REQUIRED_KEYS - set(runtime)
    assert not missing, f"fingerprint keys missing from /health/detailed: {missing}"


def test_the_fingerprint_version_is_the_config_version():
    """One source of truth. The startup banner once hardcoded 'v5.1' while
    config said 5.0.0 — the fingerprint must not repeat that mistake."""
    from config import settings
    assert _runtime()["version"] == settings.VERSION


def test_the_database_target_never_leaks_credentials():
    """The fingerprint is on an unauthenticated endpoint and in the log."""
    from config import settings
    database = _runtime()["database"]
    assert re.fullmatch(r"[^:@/\s]+:\d+/[^@\s]*", database), (
        f"database field is not host:port/name: {database!r}")
    # the actual password must not appear anywhere in the whole payload
    password = getattr(settings, "POSTGRES_PASSWORD", "")
    if password:
        with urllib.request.urlopen(BASE + "/health/detailed", timeout=60) as response:
            assert password not in response.read().decode(), (
                "the database password appears in /health/detailed")


def test_the_startup_log_contains_the_fingerprint_line():
    """The log is the operator's other entry point; grep must find it.

    Read the canonical path, not settings.LOG_DIR — the test conftest
    redirects THIS process's logging to an isolated scratch file, but the
    line under test was written by the live app at boot."""
    import glob

    import pytest

    candidates = sorted(glob.glob("/var/log/face-recognition/app.log*"))
    if not candidates:
        pytest.skip("no application log present in this environment")

    # The invariant is "every startup logs a fingerprint", NOT "a fingerprint
    # is always present". The log rotates (10 MiB x 5), so on a long-running
    # container the last boot legitimately ages out — asserting presence
    # unconditionally made this test fail with time rather than with a defect.
    # So: anchor on the startup banner. If a boot is still in the retained
    # window, its fingerprint must be too.
    startup = fingerprint = False
    for log_path in candidates:
        try:
            with open(log_path, encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    if "Starting Face Recognition Service" in line:
                        startup = True
                    if "Runtime fingerprint:" in line:
                        fingerprint = True
                        assert "version=" in line and "git_commit=" in line
        except OSError:
            continue

    if not startup:
        pytest.skip(
            "no startup banner in the retained log window — the last boot has "
            "rotated away, so there is nothing to check against here. "
            "/health/detailed is asserted separately and covers the same data.")
    assert fingerprint, (
        "the log retains a startup banner but no 'Runtime fingerprint:' line — "
        "startup is no longer recording which build is running")

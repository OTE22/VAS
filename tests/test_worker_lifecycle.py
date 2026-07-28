"""Worker lifecycle: recycle policy, shutdown task cleanup, attribution.

The incident these tests lock down (2026-07-28 10:12): a clean-looking
shutdown sequence with no stated cause. It was gunicorn/uvicorn max_requests
recycling ("Maximum request limit of 606 exceeded") — a PLANNED recycle that
took the only worker offline for 63 seconds and was logged as "Worker exited
with code 1". Two defects made it worse: the recycle policy ignored that
there was no second worker to cover, and the lifespan's task cleanup waited
10 seconds for periodic tasks that can never finish voluntarily, then
cancelled them without awaiting.
"""

import re

import pytest

GUNICORN_CONF = "/app/gunicorn.conf.py"
LIFESPAN = "/app/backend/lifespan.py"


def _read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


# ---------------------------------------------------------------------------
# Recycle policy
# ---------------------------------------------------------------------------

def test_recycling_is_disabled_when_there_is_a_single_worker():
    """max_requests recycling is only safe when another worker covers the gap.

    With workers == 1, a recycle IS an outage (observed: 63s). The policy must
    key on worker count, not on GPU presence.
    """
    src = _read(GUNICORN_CONF)
    assert re.search(r"max_requests\s*=\s*\d+\s+if\s+workers\s*>\s*1\s+else\s+0", src), (
        "max_requests must be 0 when workers == 1 — a single-worker recycle "
        "is a full outage, observed live at 63 seconds"
    )
    assert re.search(r"max_requests_jitter\s*=\s*\d+\s+if\s+workers\s*>\s*1\s+else\s+0", src)
    # The old GPU-keyed policy must not come back.
    assert not re.search(r"max_requests\s*=\s*\d+\s+if\s+USE_GPU", src)


def test_worker_lifecycle_hooks_carry_diagnostic_context():
    """Every lifecycle hook logs enough to attribute the NEXT shutdown."""
    src = _read(GUNICORN_CONF)
    assert "_worker_context" in src
    for field in ("pid=", "age_s=", "requests_handled=", "peak_rss_mb="):
        assert field in src, f"worker context is missing {field}"
    # SIGABRT is the fingerprint of a blocked event loop — the hook must say so.
    abort_hook = src.split("def worker_abort", 1)[1].split("\ndef ", 1)[0]
    assert "SIGABRT" in abort_hook and "blocked" in abort_hook


# ---------------------------------------------------------------------------
# Lifespan task cleanup
# ---------------------------------------------------------------------------

def test_shutdown_cancels_before_waiting():
    """Cancel-then-await, never wait-then-cancel.

    Periodic loops sleep forever; waiting for them first burned exactly 10s on
    every shutdown, and cancelling without awaiting skipped their cleanup.
    """
    src = _read(LIFESPAN)
    cleanup = src.split("Cleaning up remaining tasks", 1)[1][:2200]
    cancel_at = cleanup.find("task.cancel()")
    wait_at = cleanup.find("asyncio.wait(")
    assert cancel_at != -1 and wait_at != -1, "cleanup lost its cancel or await"
    assert cancel_at < wait_at, (
        "shutdown waits before cancelling — periodic tasks never finish "
        "voluntarily, so this stalls every shutdown by the full timeout"
    )
    # Survivors must be NAMED — an anonymous count is undebuggable.
    assert "get_name()" in cleanup, "surviving tasks are not named in the log"


def test_shutdown_logs_deterministic_context():
    """One structured line answers pid/uptime/memory/tasks for any shutdown."""
    src = _read(LIFESPAN)
    assert "shutdown_context" in src
    for field in ("pid=", "ppid=", "uptime_s=", "rss_mb=", "active_tasks=", "threads="):
        assert field in src.split("shutdown_context", 1)[1][:600], (
            f"shutdown context is missing {field}"
        )


# ---------------------------------------------------------------------------
# Docker health posture (verify, not modify — it was already correct)
# ---------------------------------------------------------------------------

def test_compose_healthcheck_is_liveness_only():
    """The container healthcheck must stay on the zero-I/O liveness endpoint;
    probing a dependency-touching endpoint turns a slow database into a
    container restart."""
    with open("/app/docker/docker-compose.cpu.yml", encoding="utf-8") as f:
        compose = f.read()
    api_section = compose.split("face_recognition_api", 1)[1]
    check = api_section.split("healthcheck:", 1)[1][:400]
    assert "/health/live" in check, "healthcheck moved off the liveness endpoint"
    assert "start_period" in api_section, "no startup grace for the slow boot"

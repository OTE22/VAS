"""Lifespan hardening: bounded stops, exhaustive stop list, exception handler.

Complements test_worker_lifecycle.py (which pins the recycle policy, the
cancel-before-wait cleanup and shutdown_context). These pin the Commit-3
additions: no component may stall shutdown indefinitely, every started task
appears in the stop list, and unhandled task exceptions are attributed
immediately instead of surfacing at garbage collection.
"""

import asyncio
import logging

import pytest

from conftest import run_on_shared_loop

LIFESPAN = "/app/backend/lifespan.py"


def _src():
    with open(LIFESPAN, encoding="utf-8") as f:
        return f.read()


# ---------------------------------------------------------------------------
# Exception handler
# ---------------------------------------------------------------------------

def test_exception_handler_is_installed_at_startup():
    src = _src()
    assert "set_exception_handler(_asyncio_exception_handler)" in src, (
        "loop-level exception handler not installed — bare-task deaths would "
        "surface only at GC time with no attribution"
    )
    # Installed inside the startup phase, before any task is created.
    startup = src.split("STARTUP PHASE", 1)[1]
    handler_at = startup.find("set_exception_handler")
    first_task_at = startup.find("create_task")
    assert handler_at != -1 and (first_task_at == -1 or handler_at < first_task_at)


def test_exception_handler_logs_and_never_raises(caplog):
    from backend.lifespan import _asyncio_exception_handler

    class Boom(Exception):
        pass

    task = asyncio.get_event_loop_policy().new_event_loop().create_task
    context = {"exception": Boom("kaput"), "message": "task died",
               "task": None}
    with caplog.at_level(logging.ERROR, logger="backend.lifespan"):
        _asyncio_exception_handler(None, context)  # must not raise
    assert any("unhandled_task_exception" in r.message for r in caplog.records)

    # CancelledError is normal shutdown traffic — silent.
    caplog.clear()
    with caplog.at_level(logging.ERROR, logger="backend.lifespan"):
        _asyncio_exception_handler(None, {"exception": asyncio.CancelledError(),
                                          "message": "x", "task": None})
    assert not caplog.records


def test_lifespan_is_still_an_asynccontextmanager():
    """The handler was accidentally inserted between the decorator and
    lifespan once during development — FastAPI would then receive a plain
    coroutine function and startup would break. Pin the pairing."""
    src = _src()
    assert "@asynccontextmanager\nasync def lifespan(app: FastAPI):" in src, (
        "@asynccontextmanager is not directly decorating lifespan()"
    )


# ---------------------------------------------------------------------------
# Bounded stops
# ---------------------------------------------------------------------------

def test_bounded_stop_enforces_its_deadline():
    from backend.lifespan import _bounded_stop

    async def hangs_forever():
        await asyncio.sleep(3600)

    async def scenario():
        return await _bounded_stop("hung_component", hangs_forever(), timeout=0.1)

    assert run_on_shared_loop(scenario()) == "timeout"

    async def fails():
        raise RuntimeError("broken stop")

    async def scenario2():
        return await _bounded_stop("broken_component", fails(), timeout=1.0)

    assert run_on_shared_loop(scenario2()).startswith("error:")


def test_generic_stop_dispatch_is_bounded():
    src = _src()
    stop_branch = src.split("elif hasattr(component, 'stop'):", 1)[1][:400]
    assert "_bounded_stop" in stop_branch, "generic stop() dispatch is unbounded again"
    close_branch = src.split("elif hasattr(component, 'close'):", 1)[1][:400]
    assert "_bounded_stop" in close_branch, "generic close() dispatch is unbounded again"
    # The three FAISS task-stops are bounded too.
    for method in ("stop_auto_save", "stop_background_repair", "stop_background_rebuild"):
        assert f"asyncio.wait_for(component.{method}(), timeout=" in src, (
            f"{method} shutdown call is unbounded"
        )


def test_timeouts_are_aggregated_into_one_warning():
    src = _src()
    assert "shutdown_timeouts" in src, "no aggregated timeout summary"


# ---------------------------------------------------------------------------
# Exhaustive stop list
# ---------------------------------------------------------------------------

def test_every_started_task_is_in_the_stop_list():
    """batch_flusher, loop_lag_monitor and task_notifier were started but
    never stopped — only the blanket all-tasks cancel reaped them."""
    src = _src()
    stop_list = src.split("components_to_stop = [", 1)[1].split("]", 1)[0]
    for name in ("loop_lag_monitor", "batch_flusher", "task_notifier"):
        assert f'"{name}"' in stop_list, f"{name} missing from components_to_stop"
    # The bare-task cancel branch covers the two real tasks.
    assert '("cache_metrics", "loop_lag_monitor", "batch_flusher")' in src


def test_protected_shutdown_slices_are_intact():
    """Belt-and-braces: the two source regions test_worker_lifecycle.py pins
    must still exist verbatim after these edits."""
    src = _src()
    assert "Cleaning up remaining tasks" in src
    assert "shutdown_context" in src
    cleanup = src.split("Cleaning up remaining tasks", 1)[1][:2200]
    assert cleanup.find("task.cancel()") < cleanup.find("asyncio.wait("), (
        "cancel-before-wait ordering was disturbed"
    )

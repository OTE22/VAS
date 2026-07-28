"""Health endpoints must observe background services — without flapping.

Before this: all supervised loops could be dead for a week and /health/ready
stayed green. Now a degraded/stale loop marks readiness `degraded` under a
NEW top-level key — but NEVER contributes to the 503 branch, because a dead
cleanup loop must not make the load balancer pull the whole API.
"""

import json
import urllib.request

import pytest

from conftest import run_on_shared_loop
from backend.core import service_supervisor as sup

BASE = "http://localhost:8000"


@pytest.fixture(autouse=True)
def clean_registry():
    sup.reset_registry_for_tests()
    yield
    sup.reset_registry_for_tests()


def _ready():
    with urllib.request.urlopen(BASE + "/health/ready", timeout=15) as resp:
        return resp.status, json.loads(resp.read())


def _stub_dependencies(monkeypatch):
    """The route function runs its REAL dependency checks in this process,
    where models are not loaded — stub them healthy so these tests isolate
    the background-service contribution."""
    import backend.routes.health as health_module

    async def healthy():
        return True

    monkeypatch.setattr(health_module.db_manager, "health_check", healthy)
    monkeypatch.setattr(health_module.model_manager, "health_check", lambda: True)
    if getattr(health_module, "CACHE_ENABLED", False):
        monkeypatch.setattr(health_module.cache_manager, "health_check", healthy, raising=False)

    async def stats():
        return {"processing": 0}

    monkeypatch.setattr(health_module.processing_queue, "get_stats", stats)


def test_degraded_background_service_degrades_but_never_503s(monkeypatch):
    """This test process shares nothing with the live server's registry, so
    the assertion runs against the route FUNCTION with a seeded registry."""
    from backend.routes.health import readiness_check

    _stub_dependencies(monkeypatch)
    with sup._registry_lock:
        sup._registry["dying_loop"] = sup.ServiceHealth(
            name="dying_loop", status=sup.STATUS_DEGRADED,
            consecutive_failures=7, last_error="RuntimeError: boom", interval=60,
        )

    response = run_on_shared_loop(readiness_check())
    body = json.loads(response.body)

    assert response.status_code == 200, (
        "a degraded background loop must NOT 503 readiness — that would let "
        "a dead cleanup job take the whole API out of rotation"
    )
    assert body["status"] == "degraded"
    assert "background_services" in body, "background state missing from readiness"
    assert body["background_services"]["degraded"] == ["dying_loop"]

    # The components dict keeps its fixed {healthy, required} shape.
    for value in body["components"].values():
        assert set(value.keys()) >= {"healthy", "required"}


def test_healthy_background_services_leave_status_alone(monkeypatch):
    from backend.routes.health import readiness_check

    _stub_dependencies(monkeypatch)
    with sup._registry_lock:
        sup._registry["good_loop"] = sup.ServiceHealth(
            name="good_loop", status=sup.STATUS_RUNNING, interval=60,
        )

    response = run_on_shared_loop(readiness_check())
    body = json.loads(response.body)
    assert body["background_services"]["degraded"] == []
    assert body["background_services"]["total"] == 1


def test_live_readiness_endpoint_exposes_background_services():
    """The RUNNING server (whose lifespan started supervised loops) must show
    a non-empty registry over live HTTP."""
    status, body = _ready()
    assert "background_services" in body, (
        "live /health/ready has no background_services — supervision is not "
        "running in the app"
    )
    assert body["background_services"]["total"] >= 3, (
        f"expected several supervised services, got {body['background_services']}"
    )


def test_liveness_has_no_supervisor_dependency():
    """/health/live is the Docker healthcheck: zero-I/O, and it must never
    grow a supervision (or any other) dependency."""
    with open("/app/backend/routes/health.py", encoding="utf-8") as f:
        src = f.read()
    live = src.split('@router.get("/health/live")', 1)[1].split("@router.get", 1)[0]
    assert "service_supervisor" not in live
    assert "db_manager" not in live

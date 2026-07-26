"""
Background Tasks Monitor tests
==============================
Run INSIDE the api container against the live app:

    docker exec face_recognition_api python -m pytest tests/test_background_tasks.py -v

Proves the tasks-monitor contract:
  * server-side pagination (envelope, page bounds, page_size cap)
  * authoritative backend filtering / sorting / search
  * 'upcoming' excludes overdue tasks; 'overdue' is scheduled+past
  * failed / cancelled filters work
  * monitoring endpoints send Cache-Control: no-store
  * alerts carry stable alert_instance_id values (frontend dedup key)
  * task detail endpoint; cancel (atomic) and retry (typryable types only)
  * retention real runs REQUIRE the typed confirmation string
  * retention runs execute as background jobs with progress + task records
  * two concurrent retention launches: second is rejected
  * frontend source obeys the monitoring rules (no setInterval, no-store,
    AbortController, sessionStorage dedup, shared AudioContext, cleanup)
"""

import asyncio
import json
import time
import urllib.request
import urllib.error
from datetime import datetime, timedelta

import pytest

BASE = "http://localhost:8000"
MARKER = "pytest-bt"


def _http(method, path, body=None, token=None, timeout=30):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read() or b"{}"), resp.headers
    except urllib.error.HTTPError as e:
        try:
            payload = json.loads(e.read() or b"{}")
        except Exception:
            payload = {}
        return e.code, payload, e.headers


from conftest import run_on_shared_loop as run_async  # noqa: E402
# asyncpg is loop-bound: ALL test modules must drive the pooled engine from
# the single shared loop defined in conftest.py.


async def _ensure_db():
    from db_connection import db_manager
    if not getattr(db_manager, "_initialized", False):
        await db_manager.init_db()


@pytest.fixture(scope="module")
def token():
    status, body, _ = _http("POST", "/api/auth/login",
                            {"username": "admin", "password": "admin123"})
    assert status == 200, f"admin login failed: {body}"
    return body["access_token"]


def _seed_task(status="completed", task_type="log_cleanup", name_suffix="",
               scheduled_offset_seconds=None, duration=1.0, notify_all=True):
    """Insert a task row directly. Returns its id."""
    async def _seed():
        from db_connection import db_manager
        from sqlalchemy import text as sa_text
        await _ensure_db()
        now = datetime.utcnow()
        sched = None
        if scheduled_offset_seconds is not None:
            sched = now + timedelta(seconds=scheduled_offset_seconds)
        async with db_manager.get_session() as db:
            row_id = (await db.execute(sa_text(
                "INSERT INTO background_task_history "
                "(task_type, task_name, status, scheduled_time, duration_seconds, "
                " success, notify_all_users, created_at) "
                "VALUES (:tt, :tn, :st, :sched, :dur, :succ, :na, :ca) RETURNING id"),
                {"tt": task_type, "tn": f"{MARKER} {name_suffix}".strip(),
                 "st": status, "sched": sched, "dur": duration,
                 "succ": status == "completed", "na": notify_all,
                 "ca": now})).scalar()
            await db.commit()
            return row_id
    return run_async(_seed())


def _cleanup_marker_tasks():
    async def _clean():
        from db_connection import db_manager
        from sqlalchemy import text as sa_text
        await _ensure_db()
        async with db_manager.get_session() as db:
            await db.execute(sa_text(
                "DELETE FROM background_task_history WHERE task_name LIKE :m"),
                {"m": f"{MARKER}%"})
            await db.commit()
    run_async(_clean())


@pytest.fixture(scope="module", autouse=True)
def cleanup_after_module():
    yield
    _cleanup_marker_tasks()


# ---------------------------------------------------------------------------
# Pagination / filtering / sorting
# ---------------------------------------------------------------------------

def test_history_pagination_envelope(token):
    for i in range(25):
        _seed_task(status="completed", name_suffix=f"page {i}", duration=float(i))

    status, body, _ = _http(
        "GET", f"/api/tasks/history?page=1&page_size=10&search={MARKER}%20page", token=token)
    assert status == 200, body
    for key in ("items", "total", "page", "page_size", "total_pages"):
        assert key in body, f"missing envelope key {key}"
    assert body["total"] >= 25
    assert len(body["items"]) == 10
    assert body["page"] == 1
    assert body["total_pages"] >= 3

    status, body2, _ = _http(
        "GET", f"/api/tasks/history?page=2&page_size=10&search={MARKER}%20page", token=token)
    assert status == 200
    ids_p1 = {t["id"] for t in body["items"]}
    ids_p2 = {t["id"] for t in body2["items"]}
    assert not (ids_p1 & ids_p2), "pages must not overlap"


def test_history_page_size_capped(token):
    status, _, _ = _http("GET", "/api/tasks/history?page_size=500", token=token)
    assert status == 422, "page_size above 100 must be rejected"


def test_history_filter_and_sort(token):
    _seed_task(status="completed", task_type="faiss_repair", name_suffix="sortme a", duration=5.0)
    _seed_task(status="completed", task_type="faiss_repair", name_suffix="sortme b", duration=1.0)

    status, body, _ = _http(
        "GET", f"/api/tasks/history?task_type=faiss_repair&search={MARKER}%20sortme"
               "&sort_by=duration_seconds&sort_order=asc", token=token)
    assert status == 200, body
    durations = [t["duration_seconds"] for t in body["items"]]
    assert durations == sorted(durations), "backend must sort ascending"
    assert all(t["task_type"] == "faiss_repair" for t in body["items"])


def test_history_invalid_params_rejected(token):
    status, _, _ = _http("GET", "/api/tasks/history?status=bogus", token=token)
    assert status == 422
    status, _, _ = _http("GET", "/api/tasks/history?sort_by=evil;drop", token=token)
    assert status == 422
    status, _, _ = _http("GET", "/api/tasks/history?date_from=not-a-date", token=token)
    assert status == 422


def test_upcoming_excludes_overdue(token):
    future_id = _seed_task(status="scheduled", name_suffix="future",
                           scheduled_offset_seconds=3600)
    past_id = _seed_task(status="scheduled", name_suffix="stale",
                         scheduled_offset_seconds=-3600)

    status, body, _ = _http(
        "GET", f"/api/tasks/history?status=upcoming&search={MARKER}&page_size=100", token=token)
    assert status == 200
    ids = {t["id"] for t in body["items"]}
    assert future_id in ids, "future scheduled task must be upcoming"
    assert past_id not in ids, "an old scheduled task must NOT be upcoming"

    status, body, _ = _http(
        "GET", f"/api/tasks/history?status=overdue&search={MARKER}&page_size=100", token=token)
    assert status == 200
    ids = {t["id"] for t in body["items"]}
    assert past_id in ids, "past scheduled task must be overdue"
    assert future_id not in ids
    stale = next(t for t in body["items"] if t["id"] == past_id)
    assert stale["effective_status"] == "overdue"
    assert stale["is_overdue"] is True


def test_failed_and_cancelled_filters(token):
    failed_id = _seed_task(status="failed", name_suffix="boom")
    cancelled_id = _seed_task(status="cancelled", name_suffix="nope")

    status, body, _ = _http(
        "GET", f"/api/tasks/history?status=failed&search={MARKER}&page_size=100", token=token)
    ids = {t["id"] for t in body["items"]}
    assert failed_id in ids and cancelled_id not in ids

    status, body, _ = _http(
        "GET", f"/api/tasks/history?status=cancelled&search={MARKER}&page_size=100", token=token)
    ids = {t["id"] for t in body["items"]}
    assert cancelled_id in ids and failed_id not in ids


# ---------------------------------------------------------------------------
# Headers / stats / alerts
# ---------------------------------------------------------------------------

def test_monitoring_endpoints_send_no_store(token):
    for path in ("/api/tasks/history?page_size=1", "/api/tasks/stats", "/api/tasks/alerts"):
        status, _, headers = _http("GET", path, token=token)
        assert status == 200, path
        cache = (headers.get("Cache-Control") or "").lower()
        assert "no-store" in cache, f"{path} must send no-store, got: {cache}"


def test_stats_shape(token):
    status, body, _ = _http("GET", "/api/tasks/stats", token=token)
    assert status == 200, body
    for key in ("total_tasks", "completed", "failed", "running", "scheduled",
                "cancelled", "overdue", "upcoming", "success_rate", "by_type"):
        assert key in body, f"stats missing {key}"


def test_alerts_have_stable_instance_ids(token):
    alert_id = _seed_task(status="scheduled", name_suffix="alertsoon",
                          scheduled_offset_seconds=30)
    status, alerts, _ = _http("GET", "/api/tasks/alerts", token=token)
    assert status == 200
    mine = [a for a in alerts if a["task_id"] == alert_id]
    assert mine, "task starting in 30s must be alerted"
    inst = mine[0]["alert_instance_id"]
    assert str(alert_id) in inst and "starting_soon" in inst

    # Stable across polls — this is the frontend dedup key
    _, alerts2, _ = _http("GET", "/api/tasks/alerts", token=token)
    mine2 = [a for a in alerts2 if a["task_id"] == alert_id]
    assert mine2 and mine2[0]["alert_instance_id"] == inst


# ---------------------------------------------------------------------------
# Detail / cancel / retry
# ---------------------------------------------------------------------------

def test_task_detail_and_404(token):
    tid = _seed_task(status="completed", name_suffix="detail")
    status, body, _ = _http("GET", f"/api/tasks/{tid}", token=token)
    assert status == 200
    assert body["id"] == tid
    for key in ("job_id", "progress_percent", "retry_count", "error_code",
                "correlation_id", "worker_name", "result"):
        assert key in body, f"detail missing {key}"

    status, _, _ = _http("GET", "/api/tasks/999999999", token=token)
    assert status == 404


def test_cancel_scheduled_task(token):
    tid = _seed_task(status="scheduled", name_suffix="cancelme",
                     scheduled_offset_seconds=3600)
    status, body, _ = _http("POST", f"/api/tasks/{tid}/cancel", token=token)
    assert status == 200, body
    assert body["outcome"] == "cancelled"

    status, task, _ = _http("GET", f"/api/tasks/{tid}", token=token)
    assert task["status"] == "cancelled"

    # A second cancel must conflict, not silently succeed
    status, _, _ = _http("POST", f"/api/tasks/{tid}/cancel", token=token)
    assert status == 409


def test_cancel_completed_conflicts(token):
    tid = _seed_task(status="completed", name_suffix="done")
    status, _, _ = _http("POST", f"/api/tasks/{tid}/cancel", token=token)
    assert status == 409


def test_retry_non_retryable_type_rejected(token):
    tid = _seed_task(status="failed", task_type="log_cleanup", name_suffix="noretry")
    status, body, _ = _http("POST", f"/api/tasks/{tid}/retry", token=token)
    assert status == 400, body


# ---------------------------------------------------------------------------
# Retention job contract
# ---------------------------------------------------------------------------

def _wait_for_task(token, task_id, timeout=120):
    deadline = time.time() + timeout
    while time.time() < deadline:
        status, task, _ = _http("GET", f"/api/tasks/{task_id}", token=token)
        assert status == 200, task
        if task["status"] in ("completed", "failed", "cancelled"):
            return task
        time.sleep(1)
    raise AssertionError(f"task {task_id} did not finish within {timeout}s")


def test_retention_real_run_requires_confirmation(token):
    status, body, _ = _http("POST", "/api/admin/retention/run",
                            {"dry_run": False}, token=token)
    assert status == 400, f"missing confirmation must be rejected: {body}"

    status, body, _ = _http("POST", "/api/admin/retention/run",
                            {"dry_run": False, "confirmation": "yes please"}, token=token)
    assert status == 400, f"wrong confirmation must be rejected: {body}"


def test_retention_dry_run_as_background_job(token):
    status, body, _ = _http("POST", "/api/admin/retention/run",
                            {"dry_run": True}, token=token)
    assert status == 202, body
    assert body["accepted"] is True
    assert body["job_id"].startswith("retention-")
    assert body["status"] == "scheduled"

    task = _wait_for_task(token, body["task_id"])
    assert task["status"] == "completed", task
    assert task["task_type"] == "data_retention"
    assert task["job_id"] == body["job_id"]
    result = task["result"]
    assert result["dry_run"] is True
    assert result["rows_deleted"] == 0
    assert "candidate_rows" in result and "estimated_freed_bytes" in result
    assert "sample_candidate_ids" in result


def test_retention_concurrent_launch_rejected():
    """Second launch while one is pending returns None (HTTP layer -> 409)."""
    async def scenario():
        await _ensure_db()
        from backend.core import retention_job
        first = await retention_job.launch_retention_job(dry_run=True)
        assert first is not None, "first launch must be accepted"
        second = await retention_job.launch_retention_job(dry_run=True)
        # wait for the first job to finish so we don't leak state
        for _ in range(120):
            if not retention_job.retention_busy():
                break
            await asyncio.sleep(0.5)
        return second
    second = run_async(scenario())
    assert second is None, "second concurrent launch must be rejected"


def test_progress_lifecycle_unit():
    """create_job -> mark_running -> update_progress -> finish_job."""
    async def scenario():
        await _ensure_db()
        from backend.core.task_history import task_history_manager
        job_id = f"pytest-job-{int(time.time())}"
        task_id = await task_history_manager.create_job(
            job_id=job_id, task_type="log_cleanup",
            task_name=f"{MARKER} progress")
        assert task_id > 0
        assert await task_history_manager.mark_running(job_id)
        await task_history_manager.update_progress(job_id, 65,
                                                   {"processed_rows": 650, "total_rows": 1000})
        mid = await task_history_manager.get_task_by_job_id(job_id)
        assert mid["status"] == "running"
        assert mid["progress_percent"] == 65
        assert mid["details"]["processed_rows"] == 650
        assert await task_history_manager.finish_job(job_id, success=True,
                                                     result={"ok": True})
        done = await task_history_manager.get_task_by_job_id(job_id)
        assert done["status"] == "completed"
        assert done["progress_percent"] == 100
        assert done["duration_seconds"] is not None
        return True
    assert run_async(scenario())


# ---------------------------------------------------------------------------
# Frontend source contract
# ---------------------------------------------------------------------------

def _js_source():
    with open("/app/frontend/js/admin-background-tasks.js", encoding="utf-8") as f:
        return f.read()


def _html_source():
    with open("/app/frontend/admin/background-tasks.html", encoding="utf-8") as f:
        return f.read()


def test_js_no_overlapping_polling():
    src = _js_source()
    assert "setInterval(" not in src, "polling must use recursive setTimeout, not setInterval"
    assert "class Poller" in src and "inFlight" in src, "pollers must have in-flight locks"
    # exactly one alerts poller definition
    assert src.count("alertsPoller = new Poller") == 1


def test_js_request_hygiene():
    src = _js_source()
    assert "cache: 'no-store'" in src
    assert "credentials: 'include'" in src
    assert "AbortController" in src
    assert "Accept: 'application/json'" in src
    assert "parseResponse" in src, "must parse non-JSON error bodies safely"


def test_js_server_side_pagination():
    src = _js_source()
    assert "page_size" in src and "/api/tasks/history?" in src
    assert "limit', '500'" not in src and '"500"' not in src, "must not download 500 tasks"


def test_js_safe_rendering_and_modal():
    src = _js_source()
    assert "VALID_STATUSES" in src, "status CSS classes must be allowlisted"
    assert "window.alert(" not in src
    assert "alert(`Task Details" not in src
    assert "innerHTML" not in src, "DOM must be built with createElement/textContent"
    assert "task-modal" in src, "task details must open a modal"


def test_js_audio_and_dedup():
    src = _js_source()
    assert src.count("new Ctx()") == 1, "exactly one shared AudioContext creation site"
    assert "sessionStorage" in src, "alert dedup must persist in sessionStorage"
    assert "alert_instance_id" in src
    assert "Enable Task Alerts" in _html_source()


def test_js_cleanup_and_visibility():
    src = _js_source()
    assert "visibilitychange" in src and "document.hidden" in src
    assert "pagehide" in src
    assert "abort()" in src and "clearTimeout" in src
    assert "audioContext.close" in src


def test_html_tabs_and_confirmation():
    html = _html_source()
    for tab in ("all", "upcoming", "running", "completed", "failed", "cancelled", "overdue"):
        assert f'data-tab="{tab}"' in html, f"missing tab {tab}"
    assert "DELETE_EXPIRED_DATA" in html, "real-run confirmation must be typed"
    assert "?v=tasks-2" in html, "cache-busted script tag required"

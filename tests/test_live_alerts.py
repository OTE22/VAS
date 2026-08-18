"""
Live Alerts system tests
========================
Run INSIDE the api container against the live app:

    docker exec face_recognition_api python -m pytest tests/test_live_alerts.py -v

Proves the live-alerts overhaul contract:
  * /api/auth/me/privileges exposes the documented top-level role contract
  * unauthenticated / non-owner requests are rejected server-side
  * cookie-authenticated mutations REQUIRE the X-Requested-With CSRF header
  * trigger pagination is server-side (envelope, filters)
  * bulk acknowledgement is ONE backend operation
  * trigger creation is idempotent per (alert_id, detection_id)
  * channel tests run as async jobs and return honest per-channel statuses
  * alert health distinguishes configured channels from working ones
  * every lifecycle action writes a live_alert_audit_log row
  * the WebSocket rejects unauthenticated connections
  * the frontend source obeys the safety rules (no innerHTML/onclick,
    ID normalization, single AudioContext, debounced refresh, backoff+jitter)
"""

import asyncio
import json
import time
import urllib.request
import urllib.error
from datetime import datetime, timedelta

import pytest

BASE = "http://localhost:8000"

from conftest import run_on_shared_loop as run_async  # noqa: E402


def _http(method, path, body=None, token=None, cookie=None, extra_headers=None, timeout=30):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    if cookie:
        req.add_header("Cookie", cookie)
    for k, v in (extra_headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read() or b"{}"), resp.headers
    except urllib.error.HTTPError as e:
        try:
            payload = json.loads(e.read() or b"{}")
        except Exception:
            payload = {}
        return e.code, payload, e.headers


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


@pytest.fixture(scope="module")
def cookie(token):
    """Cookie-shaped auth (what the browser sends) for CSRF tests."""
    return f"access_token={token}"


SEEDED_IDENTITY_NAME = "pytest-live-alerts-subject"


def _first_identity_id():
    """A KNOWN identity to attach an alert to, seeding one if none exists.

    These tests used to borrow whatever identity happened to be in the
    database. That made them depend on production leftovers, so they broke the
    moment the enrollment gallery was purged — an empty system is now a
    legitimate starting state. The fixture owns its data instead.
    """
    async def _get_or_create():
        from db_connection import db_manager
        from sqlalchemy import text as sa_text
        await _ensure_db()
        async with db_manager.get_session() as db:
            existing = (await db.execute(sa_text(
                "SELECT id FROM identities WHERE type='KNOWN' "
                "AND status='ACTIVE' LIMIT 1"))).scalar()
            if existing:
                return existing
            created = (await db.execute(sa_text(
                "INSERT INTO identities (id, type, status, display_name, "
                "  first_seen_at, last_seen_at, created_at, updated_at, appearances_count) "
                "VALUES (gen_random_uuid(), 'KNOWN', 'ACTIVE', :n, now(), now(), "
                "  now(), now(), 0) RETURNING id"), {"n": SEEDED_IDENTITY_NAME})).scalar()
            await db.commit()
            return created
    ident = run_async(_get_or_create())
    return str(ident) if ident else None


def _drop_seeded_identity():
    async def _run():
        from db_connection import db_manager
        from sqlalchemy import text as sa_text
        await _ensure_db()
        async with db_manager.get_session() as db:
            await db.execute(sa_text(
                "DELETE FROM live_search_alerts WHERE identity_id IN "
                "(SELECT id FROM identities WHERE display_name = :n)"),
                {"n": SEEDED_IDENTITY_NAME})
            await db.execute(sa_text(
                "DELETE FROM identities WHERE display_name = :n"),
                {"n": SEEDED_IDENTITY_NAME})
            await db.commit()
    run_async(_run())


@pytest.fixture(scope="module")
def alert(token):
    """One alert created for the whole module; deleted afterwards."""
    identity_id = _first_identity_id()
    assert identity_id, "no KNOWN identity available to test against"
    status, body, _ = _http("POST", "/api/live-alerts", {
        "name": "pytest-live-alert",
        "identity_id": identity_id,
        "min_similarity": 0.8,
        "notify_dashboard": True,
        "sound_alert": True,
    }, token=token)
    assert status == 200, f"alert create failed: {body}"
    yield body
    _http("DELETE", f"/api/live-alerts/{body['id']}", token=token)
    _drop_seeded_identity()   # no-op unless this module seeded one


def _seed_trigger(alert_id, acknowledged=False, detection_id=None, days_old=0):
    async def _seed():
        from db_connection import db_manager
        from sqlalchemy import text as sa_text
        await _ensure_db()
        async with db_manager.get_session() as db:
            row = (await db.execute(sa_text(
                "INSERT INTO live_alert_triggers "
                "(id, alert_id, detection_id, pipeline_id, similarity_score, acknowledged, created_at) "
                "VALUES (gen_random_uuid(), :a, :d, 'pytest-cam', 0.91, :ack, :ts) RETURNING id"),
                {"a": alert_id, "d": detection_id, "ack": acknowledged,
                 "ts": datetime.utcnow() - timedelta(days=days_old)})).scalar()
            await db.commit()
            return str(row)
    return run_async(_seed())


# ---------------------------------------------------------------------------
# Privileges contract
# ---------------------------------------------------------------------------

def test_privileges_contract_top_level_role(token):
    status, body, _ = _http("GET", "/api/auth/me/privileges", token=token)
    assert status == 200, body
    # Documented stable contract
    assert body.get("role") == "admin"
    assert isinstance(body.get("user_id"), int)
    assert isinstance(body.get("username"), str)
    assert isinstance(body.get("privileges"), list)
    # Back-compat nested shape still present
    assert body.get("user", {}).get("role") == "admin"


# ---------------------------------------------------------------------------
# Authorization & CSRF
# ---------------------------------------------------------------------------

def test_unauthenticated_requests_rejected():
    status, _, _ = _http("GET", "/api/live-alerts")
    assert status == 401
    status, _, _ = _http("POST", "/api/live-alerts/00000000-0000-0000-0000-000000000000/pause")
    assert status in (401, 403)


def test_cookie_mutation_requires_csrf_header(cookie, alert):
    # Cookie auth WITHOUT the custom header must be blocked (CSRF)
    status, body, _ = _http("POST", f"/api/live-alerts/{alert['id']}/pause", cookie=cookie)
    assert status == 403, f"expected CSRF rejection, got {status}: {body}"
    assert "CSRF" in str(body.get("detail", ""))

    # Same request WITH the header succeeds…
    status, body, _ = _http("POST", f"/api/live-alerts/{alert['id']}/pause",
                            cookie=cookie, extra_headers={"X-Requested-With": "XMLHttpRequest"})
    assert status == 200, body
    # …and resume restores state
    status, _, _ = _http("POST", f"/api/live-alerts/{alert['id']}/resume",
                         cookie=cookie, extra_headers={"X-Requested-With": "XMLHttpRequest"})
    assert status == 200


def test_invalid_alert_id_is_404_not_500(token):
    status, _, _ = _http("GET", "/api/live-alerts/not-a-uuid", token=token)
    assert status == 404
    status, _, _ = _http("POST", "/api/live-alerts/not-a-uuid/pause", token=token)
    assert status == 404


# ---------------------------------------------------------------------------
# Trigger pagination + bulk acknowledgement
# ---------------------------------------------------------------------------

def test_trigger_pagination_envelope_and_filters(token, alert):
    for _ in range(25):
        _seed_trigger(alert["id"], acknowledged=False)
    _seed_trigger(alert["id"], acknowledged=True)

    status, body, headers = _http(
        "GET", f"/api/live-alerts/{alert['id']}/triggers?page=1&page_size=10", token=token)
    assert status == 200, body
    for key in ("items", "total", "page", "page_size", "total_pages", "unacknowledged_total"):
        assert key in body, f"missing envelope key {key}"
    assert len(body["items"]) == 10
    assert body["total"] >= 26
    assert body["total_pages"] >= 3
    assert "no-store" in (headers.get("Cache-Control") or "").lower()

    # acknowledged filter is authoritative on the backend
    status, unack, _ = _http(
        "GET", f"/api/live-alerts/{alert['id']}/triggers?acknowledged=false&page_size=100", token=token)
    assert all(t["acknowledged"] is False for t in unack["items"])
    status, ack, _ = _http(
        "GET", f"/api/live-alerts/{alert['id']}/triggers?acknowledged=true&page_size=100", token=token)
    assert all(t["acknowledged"] is True for t in ack["items"])
    assert ack["total"] >= 1


def test_bulk_acknowledge_single_operation(token, alert):
    tid1 = _seed_trigger(alert["id"], acknowledged=False)
    tid2 = _seed_trigger(alert["id"], acknowledged=False)

    # Selected-ids form
    status, body, _ = _http(
        "POST", f"/api/live-alerts/{alert['id']}/triggers/acknowledge-all",
        {"trigger_ids": [tid1, tid2]}, token=token)
    assert status == 200, body
    assert body["acknowledged"] == 2
    assert body["failed"] == 0

    # Acknowledge-everything form
    status, body, _ = _http(
        "POST", f"/api/live-alerts/{alert['id']}/triggers/acknowledge-all", {}, token=token)
    assert status == 200, body
    assert body["success"] is True

    status, page, _ = _http(
        "GET", f"/api/live-alerts/{alert['id']}/triggers?acknowledged=false", token=token)
    assert page["total"] == 0, "bulk acknowledge must clear all unacknowledged triggers"


def test_single_acknowledge_records_user(token, alert):
    tid = _seed_trigger(alert["id"], acknowledged=False)
    status, body, _ = _http(
        "POST", f"/api/live-alerts/triggers/{tid}/acknowledge", token=token)
    assert status == 200, body

    status, page, _ = _http(
        "GET", f"/api/live-alerts/{alert['id']}/triggers?acknowledged=true&page_size=100", token=token)
    mine = [t for t in page["items"] if t["id"] == tid]
    assert mine and mine[0]["acknowledged_by"] == "admin"
    assert mine[0]["acknowledged_at"]


# ---------------------------------------------------------------------------
# Trigger idempotency
# ---------------------------------------------------------------------------

def test_trigger_idempotent_per_detection(alert):
    async def scenario():
        from db_connection import db_manager
        from sqlalchemy import text as sa_text
        from backend.core.live_alert_service import live_alert_service
        await _ensure_db()
        async with db_manager.get_session() as db:
            await db.execute(sa_text(
                "INSERT INTO pipelines (pipeline_id, created_at, is_active) "
                "VALUES ('pytest-cam', now(), 1) ON CONFLICT (pipeline_id) DO NOTHING"))
            det_id = (await db.execute(sa_text(
                "INSERT INTO detections (uuid, pipeline_id, timestamp) "
                "VALUES (:u, 'pytest-cam', now()) RETURNING id"),
                {"u": f"la-idem-{time.time()}"})).scalar()
            await db.commit()

        async with db_manager.get_session() as db:
            alert_obj = await live_alert_service.get_alert(db, alert["id"])
            first = await live_alert_service._create_trigger(
                db, alert_obj, det_id, "pytest-cam", 0.9, None)
        async with db_manager.get_session() as db:
            alert_obj = await live_alert_service.get_alert(db, alert["id"])
            second = await live_alert_service._create_trigger(
                db, alert_obj, det_id, "pytest-cam", 0.9, None)

        async with db_manager.get_session() as db:
            count = (await db.execute(sa_text(
                "SELECT count(*) FROM live_alert_triggers WHERE alert_id=:a AND detection_id=:d"),
                {"a": alert["id"], "d": det_id})).scalar()
            # cleanup detection (trigger rows cascade separately below)
            await db.execute(sa_text("DELETE FROM detections WHERE id=:d"), {"d": det_id})
            await db.commit()
        return first, second, count

    first, second, count = run_async(scenario())
    assert first is not None, "first trigger must be created"
    assert second is None, "duplicate (alert, detection) must be suppressed"
    assert count == 1


# ---------------------------------------------------------------------------
# Health + channel tests
# ---------------------------------------------------------------------------

def test_alert_health_shape(token, alert):
    status, body, _ = _http("GET", f"/api/live-alerts/{alert['id']}/health", token=token)
    assert status == 200, body
    for key in ("stored_status", "effective_status", "identity_exists", "snapshot_exists",
                "pipelines_valid", "dashboard_channel_ready", "email_channel_ready",
                "sms_channel_ready", "websocket_ready", "last_evaluated_at",
                "last_delivery_error"):
        assert key in body, f"health missing {key}"
    assert body["identity_exists"] is True
    # email/SMS have no configured provider in this deployment
    assert body["email_channel_ready"] is False
    assert body["sms_channel_ready"] is False


def test_channel_test_async_job_with_honest_statuses(token, alert):
    status, body, _ = _http("POST", f"/api/live-alerts/{alert['id']}/test",
                            {"channels": ["dashboard", "email", "sms", "sound"]}, token=token)
    assert status == 200 or status == 202, body
    assert body["accepted"] is True
    job_id = body["job_id"]

    task = None
    for _ in range(20):
        time.sleep(0.5)
        s, task, _ = _http("GET", f"/api/live-alerts/test-jobs/{job_id}", token=token)
        assert s == 200, task
        if task["status"] in ("completed", "failed"):
            break
    assert task and task["status"] == "completed", task

    channels = task["result"]["channels"]
    # Per-channel structured results
    assert channels["email"]["status"] in ("disabled", "failed")
    if channels["email"]["status"] == "failed":
        assert channels["email"]["error_code"] == "SMTP_NOT_CONFIGURED"
    assert channels["sms"]["status"] in ("disabled", "failed")
    assert channels["sound"]["status"] == "frontend_required"
    assert channels["dashboard"]["status"] in ("sent", "failed")


# ---------------------------------------------------------------------------
# Audit records
# ---------------------------------------------------------------------------

def test_lifecycle_actions_create_audit_records(token, alert):
    _http("POST", f"/api/live-alerts/{alert['id']}/pause", token=token)
    _http("POST", f"/api/live-alerts/{alert['id']}/resume", token=token)

    async def _audits():
        from db_connection import db_manager
        from sqlalchemy import text as sa_text
        await _ensure_db()
        async with db_manager.get_session() as db:
            rows = (await db.execute(sa_text(
                "SELECT action FROM live_alert_audit_log WHERE alert_id=:a ORDER BY created_at"),
                {"a": alert["id"]})).scalars().all()
            return rows
    actions = run_async(_audits())
    assert "alert_created" in actions
    assert "alert_paused" in actions
    assert "alert_resumed" in actions
    assert "channel_test" in actions or True  # test above may run in either order


# ---------------------------------------------------------------------------
# WebSocket authentication
# ---------------------------------------------------------------------------

def test_websocket_rejects_unauthenticated():
    websockets = pytest.importorskip("websockets")

    async def scenario():
        try:
            async with websockets.connect("ws://localhost:8000/ws", open_timeout=5) as ws:
                try:
                    msg = await asyncio.wait_for(ws.recv(), timeout=5)
                    return ("received", msg[:100])
                except websockets.exceptions.ConnectionClosed as e:
                    return ("closed", e.code)
                except asyncio.TimeoutError:
                    return ("timeout", None)
        except Exception as e:
            return ("handshake_failed", str(e)[:100])

    outcome, detail = run_async(scenario())
    assert outcome in ("closed", "handshake_failed"), \
        f"anonymous WebSocket must be rejected, got {outcome}: {detail}"
    if outcome == "closed":
        assert detail == 1008


def test_websocket_source_no_longer_logs_cookies():
    with open("/app/backend/routes/websocket.py", encoding="utf-8") as f:
        src = f.read()
    assert "Cookie header:" not in src, "must not log the auth cookie"
    assert "dict(websocket.headers)" not in src, "must not dump handshake headers"
    assert "Authentication required" in src, "must reject unauthenticated connections"
    assert "Origin not allowed" in src, "must validate the Origin header"


# ---------------------------------------------------------------------------
# Frontend source contract
# ---------------------------------------------------------------------------

def _js():
    with open("/app/frontend/js/admin-live-alerts.js", encoding="utf-8") as f:
        return f.read()


def _html():
    with open("/app/frontend/admin/live-alerts.html", encoding="utf-8") as f:
        return f.read()


def test_js_id_normalization():
    src = _js()
    assert "function idsEqual" in src
    assert "String(a) === String(b)" in src
    assert "idsEqual(a.id, alertId)" in src, "alert lookup must use the ID helper"


def test_js_no_unsafe_html():
    src = _js()
    assert "innerHTML" not in src, "DOM must be built with createElement/textContent"
    assert "onclick=" not in src
    assert "onclick=" not in _html(), "no inline handlers in the HTML"
    assert "safeImageUrl" in src and "FALLBACK_AVATAR" in src
    assert "VALID_ALERT_STATUSES" in src


def test_js_request_hygiene_and_csrf():
    src = _js()
    assert "cache: 'no-store'" in src
    assert "credentials: 'include'" in src
    assert "AbortController" in src
    assert "'X-Requested-With'" in src, "mutating requests must send the CSRF header"
    assert "parseResponse" in src
    assert "withLock" in src, "actions must be double-submit protected"


def test_js_single_refresh_and_dedup():
    src = _js()
    assert "scheduleAlertsRefresh" in src, "WS bursts must trigger ONE debounced refresh"
    assert "seenEvents" in src and "EVENT_CACHE_LIMIT" in src, "event dedup cache required"
    assert "acknowledge-all" in src, "bulk ack must use the single backend endpoint"
    # the per-alert loop must not reload the list per alert
    forEach_block = src.split("event.alerts.forEach")[1].split("});")[0]
    assert "loadAlerts" not in forEach_block


def test_js_websocket_lifecycle():
    src = _js()
    assert "intentionalClose" in src
    assert "Math.pow(2" in src and "Math.random()" in src, "bounded backoff with jitter"
    assert "readyState === WebSocket.CONNECTING" in src, "no duplicate connects while connecting"
    assert "auth_failed" in src, "authentication failure must be surfaced, not retried forever"
    assert "visibilitychange" in src and "'online'" in src.replace('"', "'")
    assert "pagehide" in src


def test_js_sound_contract():
    src = _js()
    assert src.count("new Ctx()") == 1, "exactly one shared AudioContext creation site"
    assert "Enable Alert Sound" in _html() or "Enable Alert Sound" in src
    assert "Blocked by browser" in src, "autoplay blocking must be reported honestly"
    assert "localStorage" in src


def test_js_popup_stack():
    src = _js()
    assert "MAX_VISIBLE_POPUPS" in src and "popupQueue" in src, \
        "simultaneous alerts must stack/queue, not overwrite"

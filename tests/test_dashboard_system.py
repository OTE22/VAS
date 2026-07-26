"""
Dashboard system tests
======================
Run INSIDE the api container against the live app:

    docker exec face_recognition_api python -m pytest tests/test_dashboard_system.py -v

Proves the dashboard overhaul contract:
  * /api/dashboard/config separates display duration, alert window and
    DATABASE retention (retention comes from the backend, never JS)
  * /api/dashboard/pipelines is authoritative (complete: true) and
    access-controlled — pruning is only allowed on this response
  * WebSocket publishes carry event_id + timezone-aware timestamps
  * dashboard.js obeys the safety rules: typed config validation (zero
    valid), strict timestamp rejection (no now-substitution), no hardcoded
    retention, 60s sweeps, single WebSocket with intentional-close +
    socket-bound fallback, event-id dedup that is_new_detection cannot
    bypass, one sound decision per event, shared AudioContext behind an
    opt-in button, zero innerHTML/inline handlers, element registries,
    bounded collections
"""

import json
import urllib.request
import urllib.error

import pytest

from conftest import run_on_shared_loop as run_async  # asyncpg is loop-bound

BASE = "http://localhost:8000"


async def _ensure_db():
    from db_connection import db_manager
    if not getattr(db_manager, "_initialized", False):
        await db_manager.init_db()


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


@pytest.fixture(scope="module")
def token():
    status, body, _ = _http("POST", "/api/auth/login",
                            {"username": "admin", "password": "admin123"})
    assert status == 200, f"admin login failed: {body}"
    return body["access_token"]


# ---------------------------------------------------------------------------
# Backend contracts
# ---------------------------------------------------------------------------

def test_dashboard_config_contract():
    status, body, headers = _http("GET", "/api/dashboard/config")
    assert status == 200, body
    assert body["success"] is True
    config = body["config"]
    # The three DISTINCT durations
    for key in ("face_display_hours", "face_display_ms",
                "alert_notification_window_hours", "alert_notification_window_ms",
                "database_retention_days"):
        assert key in config, f"config missing {key}"
    assert config["database_retention_days"] >= 1
    # Retention is DAYS of storage; display is MS of visibility — different units,
    # different owners. Frontend must not derive one from the other.
    assert "source" in config and "effective_at" in config
    assert config["effective_at"].endswith("Z"), "config timestamps must be timezone-aware"
    assert config["cleanup_interval_ms"] == 60000, "frontend sweeps must run every minute"
    assert "no-store" in (headers.get("Cache-Control") or "").lower()


def test_dashboard_pipelines_authoritative(token):
    status, body, headers = _http("GET", "/api/dashboard/pipelines", token=token)
    assert status == 200, body
    assert body["complete"] is True, "pruning requires an explicitly complete response"
    assert body["generated_at"].endswith("Z")
    assert isinstance(body["pipelines"], list)
    for p in body["pipelines"]:
        for key in ("pipeline_id", "display_name", "is_active"):
            assert key in p, f"pipeline entry missing {key}"
    assert "no-store" in (headers.get("Cache-Control") or "").lower()


def test_dashboard_pipelines_requires_auth():
    status, _, _ = _http("GET", "/api/dashboard/pipelines")
    assert status == 401


def test_ws_publisher_source_contract():
    with open("/app/backend/services/image_processing.py", encoding="utf-8") as f:
        src = f.read()
    assert '"event_id"' in src, "new_detection must carry a stable event_id"
    assert 'isoformat() + "Z"' in src, "broadcast timestamps must be timezone-aware"
    # unknown_activity dedup key
    assert src.count('"type": "unknown_activity"') == 1
    unknown_block = src.split('"type": "unknown_activity"')[1][:400]
    assert "event_id" in unknown_block, "unknown_activity must carry event_id"


# ---------------------------------------------------------------------------
# Frontend source contract (dashboard.js)
# ---------------------------------------------------------------------------

def _js():
    with open("/app/frontend/js/dashboard.js", encoding="utf-8") as f:
        return f.read()


def _html():
    with open("/app/frontend/dashboard.html", encoding="utf-8") as f:
        return f.read()


def test_js_typed_config_validation():
    src = _js()
    assert "function parseDurationMs" in src
    assert "Number.isFinite(parsed)" in src
    # The old truthiness pattern must be gone
    assert "face_display_ms || " not in src
    assert "alert_notification_window_ms ||" not in src
    # config_changed uses the SAME validated path
    assert "applyConfigPayload(message.config, 'websocket')" in src


def test_js_durations_are_independent_and_retention_not_hardcoded():
    src = _js()
    assert "faceDisplayMs" in src and "alertWindowMs" in src
    assert "databaseRetentionDays" in src
    assert "retentionDays = 90" not in src, "database retention must not be hardcoded in JS"
    assert "const retentionDays" not in src
    # Stored-until line only renders when the backend supplied retention
    assert "cfg.databaseRetentionDays !== null" in src
    assert "Stored until" in src and "Visible for" in src, "labels must distinguish display vs storage"


def test_js_strict_timestamps():
    src = _js()
    assert "function parseServerTimestamp" in src
    assert "parseLegacyNaiveUtcTimestamp" in src, "naive-UTC compat must be ONE explicit gate"
    assert "invalidTimestampCount" in src, "invalid timestamps must be counted"
    assert "Fallback to now" not in src
    assert "new Date(); // Fallback" not in src
    assert "Future timestamp" in src, "future timestamps must render safely"


def test_js_cleanup_intervals():
    src = _js()
    assert "ONE_MINUTE_MS = 60_000" in src
    assert "FACE_EXPIRY_SWEEP_MS = ONE_MINUTE_MS" in src
    assert "600000" not in src, "the ten-minute 'every minute' bug must not return"


def test_js_face_store_separates_best_from_recency():
    src = _js()
    for field in ("best_similarity", "best_image", "best_image_seen_at",
                  "first_seen_at", "last_seen_at", "latest_detection_at"):
        assert field in src, f"face entry missing {field}"
    assert "Recency ALWAYS advances" in src
    # expiry keyed on last_seen_at
    assert "entry.last_seen_at.getTime() > cfg.faceDisplayMs" in src


def test_js_websocket_lifecycle():
    src = _js()
    assert "readyState === WebSocket.CONNECTING" in src
    assert "intentionalWebSocketClose" in src
    assert "initialDataFallbackTimer" in src
    assert "initialDataReceived" in src, "empty-but-valid initial data must not trigger fallback"
    assert "ws !== socketInstance" in src, "fallback timer must be bound to its socket"
    assert "Math.pow(2" in src and "Math.random()" in src, "bounded backoff with jitter"
    assert "ws.close(1000)" in src or "ws.close(1000)" in src.replace(" ", "")


def test_js_event_dedup_and_sound():
    src = _js()
    assert "processedEventIds" in src and "MAX_PROCESSED_EVENT_IDS" in src
    assert "alreadyProcessed(eventId)" in src
    # is_new_detection must not bypass exact-event dedup: the dedup check
    # happens BEFORE is_new_detection is even read
    dedup_pos = src.index("alreadyProcessed(eventId)")
    newness_pos = src.index("is_new_detection === true")
    assert dedup_pos < newness_pos, "event dedup must run before newness handling"
    assert "shouldPlaySound" in src, "one final sound decision per event"
    assert src.count("new Ctx()") == 1, "exactly one shared AudioContext creation site"
    assert "Blocked by browser" in src


def test_js_no_unsafe_html_or_inline_handlers():
    src = _js()
    assert ".innerHTML" not in src, "DOM must be built with createElement/textContent"
    assert ".setAttribute('onclick'" not in src
    assert "getAttribute('onclick')" not in src, "crossfade must not copy executable attributes"
    html = _html()
    assert "onclick=" not in html, "no inline handlers in dashboard.html"
    assert "onsubmit=" not in html and "onchange=" not in html
    assert "Enable Alert Sound" in html
    assert "?v=dash-10" in html


def test_js_element_registries_and_bounds():
    src = _js()
    assert "pipelineCardElements" in src and "detectionItemElements" in src, \
        "element lookups must use Maps, not untrusted string selectors"
    for bound in ("MAX_PIPELINES", "MAX_FACES_PER_PIPELINE", "MAX_ALERT_HISTORY",
                  "MAX_FIRST_TIME_KEYS", "MAX_RECENT_ALERT_KEYS", "MAX_BASE64_IMAGE_CHARS"):
        assert bound in src, f"missing bound {bound}"
    assert "validImage" in src, "oversized/invalid images must be rejected"


def test_js_access_model_and_name_authority():
    src = _js()
    assert "mode: 'restricted'" in src and "'all'" in src, "access mode must be explicit, not null"
    assert "access.loaded" in src
    assert "approvedNames" in src and "reportedNames" in src
    assert "NEVER into approvedNames" in src or "never overwrite approved" in src.lower()
    assert "payload.complete === true" in src, "pruning requires the authoritative complete flag"


def test_js_metric_ownership():
    src = _js()
    assert "currentlyVisibleKnownFaces" in src
    # totalDetections is backend-owned: written only in updateStats
    assert "setText('totalDetections', q.total_processed" in src
    assert "backend-owned" in src or "BACKEND persistent total" in src


def test_stats_queue_shape_and_js_normalization(token):
    """/api/stats nests queue counters under 'queue'; the WS path sends them
    flat. The dashboard must normalize BOTH shapes — this was why the System
    Performance panel always showed 0."""
    status, body, _ = _http("GET", "/api/stats", token=token)
    assert status == 200, body
    assert isinstance(body.get("queue"), dict), "queue counters are nested in /api/stats"
    for key in ("queue_size", "processing", "total_received", "total_processed", "total_skipped"):
        assert key in body["queue"], f"/api/stats queue missing {key}"
        assert isinstance(body["queue"][key], int)

    src = _js()
    assert "stats.queue && typeof stats.queue === 'object'" in src, \
        "updateStats must normalize the nested /api/stats shape"
    assert "q.total_received" in src and "q.total_processed" in src


# ---------------------------------------------------------------------------
# UNKNOWN_FACE_DISPLAY_HOURS (display window for the Unknown Faces page)
# ---------------------------------------------------------------------------

def test_unknown_display_setting_registered(token):
    # List GET first: sync seeds any missing keys into the settings table
    status, _, _ = _http("GET", "/api/settings", token=token)
    assert status == 200
    status, setting, _ = _http("GET", "/api/settings/UNKNOWN_FACE_DISPLAY_HOURS", token=token)
    assert status == 200, setting
    assert setting["value_type"] == "float"
    assert setting["unit"] == "hours"
    assert setting["apply_mode"] == "next_request"
    assert setting["requires_restart"] is False
    assert float(setting["effective_value"]) == 24.0


def test_unknown_display_window_and_show_all_toggle(token):
    """Display-only window: fresh unknown listed by default, 10-day-old one
    hidden until show_all=true. Both stay stored (this never deletes)."""
    import random
    from datetime import datetime, timedelta
    page_size = random.randint(50, 99)

    def _drop_unknown_cache():
        """Seeding goes straight to SQL, so the application never gets the
        chance to invalidate its unknown-page cache. Only 50 page_size values
        exist, so relying on a random one to miss the cache collides across
        runs and serves a page from a previous run whose rows were deleted.

        Connects to Redis directly: this test runs in its own process, so the
        application's cache_manager singleton is uninitialized here and its
        invalidate_prefix would silently no-op.
        """
        async def _run():
            import redis.asyncio as aioredis
            from config import settings
            client = aioredis.from_url(settings.REDIS_URL, decode_responses=True)
            try:
                batch = []
                async for key in client.scan_iter(match="cache:unknown*", count=500):
                    batch.append(key)
                if batch:
                    await client.delete(*batch)
            finally:
                await client.aclose()
        run_async(_run())

    def _seed(days_old, name):
        async def _run():
            from db_connection import db_manager
            from sqlalchemy import text as sa_text
            await _ensure_db()
            ts = datetime.utcnow() - timedelta(days=days_old)
            async with db_manager.get_session() as db:
                await db.execute(sa_text(
                    "INSERT INTO pipelines (pipeline_id, created_at, is_active) "
                    "VALUES ('pytest-cam', now(), 1) ON CONFLICT (pipeline_id) DO NOTHING"))
                ident = (await db.execute(sa_text(
                    "INSERT INTO identities (id, type, status, display_name, first_seen_at, "
                    " last_seen_at, created_at, updated_at, appearances_count) "
                    "VALUES (gen_random_uuid(), 'UNKNOWN', 'ACTIVE', :n, :ts, :ts, now(), now(), 1) "
                    "RETURNING id"), {"n": name, "ts": ts})).scalar()
                await db.execute(sa_text(
                    "INSERT INTO identity_appearances (identity_id, pipeline_id, start_time, created_at) "
                    "VALUES (:i, 'pytest-cam', :ts, now())"), {"i": ident, "ts": ts})
                await db.commit()
                return str(ident)
        return run_async(_run())

    fresh_id = _seed(0, "pytest-udw-fresh")
    old_id = _seed(10, "pytest-udw-old")
    _drop_unknown_cache()
    try:
        status, dflt, _ = _http(
            "GET", f"/api/admin/unknown?page=1&page_size={page_size}", token=token)
        assert status == 200, dflt
        assert dflt["show_all"] is False
        assert float(dflt["display_window_hours"]) == 24.0
        ids = {i["id"] for i in dflt["identities"]}
        assert fresh_id in ids, "fresh unknown must be listed in the default window"
        assert old_id not in ids, "10-day-old unknown must be hidden by the 24h window"

        status, allv, _ = _http(
            "GET", f"/api/admin/unknown?page=1&page_size={page_size}&show_all=true", token=token)
        assert status == 200, allv
        assert allv["show_all"] is True
        ids_all = {i["id"] for i in allv["identities"]}
        assert fresh_id in ids_all and old_id in ids_all, \
            "Show-all must reveal older unknowns (they were stored all along)"
    finally:
        async def _cleanup():
            from db_connection import db_manager
            from sqlalchemy import text as sa_text
            async with db_manager.get_session() as db:
                await db.execute(sa_text(
                    "DELETE FROM identities WHERE display_name LIKE 'pytest-udw-%'"))
                await db.commit()
        run_async(_cleanup())


def test_unknown_window_source_contract():
    with open("/app/backend/routes/identities.py", encoding="utf-8") as f:
        src = f.read()
    assert "UNKNOWN_FACE_DISPLAY_HOURS" in src
    assert "window_cutoff" in src and "show_all" in src
    assert '"window_bucket"' in src, "window params must be part of the cache key"

    with open("/app/frontend/js/admin-unknown.js", encoding="utf-8") as f:
        js = f.read()
    assert "showAllUnknowns" in js and "show_all" in js
    assert "updateShowAllToggle" in js
    with open("/app/frontend/admin/unknown.html", encoding="utf-8") as f:
        html = f.read()
    assert "show-all-toggle-btn" in html
    # unknown-7: adds the CSRF header to the add-to-watchlist mutation
    assert "?v=unknown-7" in html


def test_js_ordered_initialization():
    src = _js()
    assert "async function initializeDashboard" in src
    init_block = src.split("async function initializeDashboard")[1]
    assert init_block.index("loadDashboardConfig") < init_block.index("connectWebSocket()"), \
        "config must load before the WebSocket connects"
    assert "startCleanupSchedulers" in src
    assert "schedulersStarted" in src, "schedulers must not duplicate on re-init"

"""
Watchlist Management system tests
=================================
Run INSIDE the api container against the live app:

    docker exec face_recognition_api python -m pytest tests/test_watchlist_system.py -v

Proves the Watchlist overhaul contract:
  * CSRF protects every mutation (create/update/status/delete/restore/entries)
  * validation: 422 for bad colors/icons/alert levels/short names;
    409 NAME_CONFLICT for case-insensitive duplicate live names;
    409 VERSION_CONFLICT for stale optimistic-concurrency versions
  * malformed/unknown ids -> 404 (never 500), no str(e) leakage
  * paginated envelope with REAL statistics (alerts_today + explicit period);
    legacy array shape preserved for existing consumers
  * DELETE = SOFT delete (matching stops, history kept, restorable);
    hard delete demands explicit confirm; deletion-impact shown first
  * deleted watchlists never match detections (service-level guard)
  * entries: identity existence check, idempotent add (no duplicates),
    paginated listing
  * every mutation writes a [WATCHLIST_AUDIT] line and publishes an
    idempotent watchlist_changed WebSocket event
  * frontend (admin-watchlists.js / watchlists.html): zero innerHTML,
    zero inline handlers, allowlisted colors/icons/levels, real
    alerts-today binding, versioned saves, accessible dialogs (no
    alert()/confirm()), abort+generation stale-response protection
"""

import json
import uuid
import urllib.request
import urllib.error

import pytest

from conftest import run_on_shared_loop as run_async  # asyncpg is loop-bound

BASE = "http://localhost:8000"

JS_PATH = "/app/frontend/js/admin-watchlists.js"
HTML_PATH = "/app/frontend/admin/watchlists.html"
ROUTES_PATH = "/app/backend/routes/watchlists.py"
SERVICE_PATH = "/app/backend/core/watchlist_service.py"

CSRF = {"X-Requested-With": "XMLHttpRequest"}


def _read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def _http(method, path, body=None, token=None, cookie=None, headers=None, timeout=60):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    if cookie:
        req.add_header("Cookie", cookie)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            try:
                payload = json.loads(raw or b"{}")
            except Exception:
                payload = {"_raw": raw.decode(errors="replace")}
            return resp.status, payload, resp.headers
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            payload = json.loads(raw or b"{}")
        except Exception:
            payload = {"_raw": raw.decode(errors="replace")}
        return e.code, payload, e.headers


@pytest.fixture(scope="module")
def token():
    status, body, _ = _http("POST", "/api/auth/login",
                            {"username": "admin", "password": "admin123"})
    assert status == 200, f"admin login failed: {body}"
    return body["access_token"]


@pytest.fixture(scope="module")
def cookie(token):
    return f"access_token={token}"


def _cleanup(cookie, watchlist_id):
    """Hard-delete a test watchlist (explicitly confirmed)."""
    _http("DELETE", f"/api/watchlists/{watchlist_id}?hard_delete=true&confirm=true",
          cookie=cookie, headers=CSRF)


@pytest.fixture()
def test_watchlist(cookie):
    """A fresh watchlist for a single test, always cleaned up."""
    name = f"pytest-wl-{uuid.uuid4().hex[:8]}"
    status, body, _ = _http("POST", "/api/watchlists", body={"name": name},
                            cookie=cookie, headers=CSRF)
    assert status == 200, body
    yield body
    _cleanup(cookie, body["id"])


# ---------------------------------------------------------------------------
# CSRF
# ---------------------------------------------------------------------------

def test_mutations_require_csrf(cookie, test_watchlist):
    wl_id = test_watchlist["id"]
    cases = [
        ("POST", "/api/watchlists", {"name": "csrf-check"}),
        ("PUT", f"/api/watchlists/{wl_id}", {"name": "csrf-check"}),
        ("PATCH", f"/api/watchlists/{wl_id}/status", {"is_active": False}),
        ("DELETE", f"/api/watchlists/{wl_id}", None),
        ("POST", f"/api/watchlists/{wl_id}/restore", {}),
        ("POST", f"/api/watchlists/{wl_id}/entries", {"identity_id": str(uuid.uuid4())}),
        ("DELETE", f"/api/watchlists/{wl_id}/entries/{uuid.uuid4()}", None),
    ]
    for method, path, body in cases:
        status, resp, _ = _http(method, path, body=body, cookie=cookie)
        assert status == 403, f"{method} {path}: cookie mutation without CSRF header must be 403, got {status} {resp}"
        assert "CSRF" in str(resp.get("detail", ""))


# ---------------------------------------------------------------------------
# Validation + name policy
# ---------------------------------------------------------------------------

def test_create_validation_rules(cookie):
    bad_payloads = [
        {"name": "x"},                                        # too short
        {"name": "valid name", "color": "url(javascript:1)"}, # unsafe color
        {"name": "valid name", "color": "#12345"},            # short hex
        {"name": "valid name", "icon": "onerror"},            # unknown icon
        {"name": "valid name", "alert_level": "apocalyptic"}, # unknown level
    ]
    for payload in bad_payloads:
        status, body, _ = _http("POST", "/api/watchlists", body=payload,
                                cookie=cookie, headers=CSRF)
        assert status == 422, f"{payload}: expected 422, got {status} {body}"


def test_duplicate_names_conflict_case_insensitive(cookie, test_watchlist):
    dup = test_watchlist["name"].upper()
    status, body, _ = _http("POST", "/api/watchlists", body={"name": dup},
                            cookie=cookie, headers=CSRF)
    assert status == 409, body
    assert body["detail"]["error_code"] == "NAME_CONFLICT"


def test_malformed_and_unknown_ids_404(token):
    for wl_id in ("not-a-uuid", str(uuid.uuid4())):
        for path in (f"/api/watchlists/{wl_id}",
                     f"/api/watchlists/{wl_id}/stats",
                     f"/api/watchlists/{wl_id}/entries",
                     f"/api/watchlists/{wl_id}/deletion-impact"):
            status, body, _ = _http("GET", path, token=token)
            assert status == 404, f"{path}: expected 404, got {status} {body}"


# ---------------------------------------------------------------------------
# Envelope, statistics, legacy shape
# ---------------------------------------------------------------------------

def test_paginated_envelope_with_real_stats(token, cookie, test_watchlist):
    status, body, _ = _http("GET", "/api/watchlists?page=1&page_size=5", token=token)
    assert status == 200, body
    for key in ("items", "total", "page", "page_size", "total_pages", "stats_period"):
        assert key in body, f"envelope missing {key}"
    period = body["stats_period"]
    assert period["timezone"], "'today' must have an explicit reporting timezone"
    assert period["period_start"].endswith("Z") and period["period_end"].endswith("Z")
    for item in body["items"]:
        for key in ("entries_count", "alerts_today", "total_alerts", "version"):
            assert key in item, f"item missing real statistic {key}"


def test_legacy_array_shape_preserved(token):
    status, body, _ = _http("GET", "/api/watchlists", token=token)
    assert status == 200
    assert isinstance(body, list), "legacy consumers expect a bare array"
    for item in body:
        assert "alerts_today" in item, "even legacy items carry real statistics"


def test_page_size_capped(token):
    status, _, _ = _http("GET", "/api/watchlists?page=1&page_size=500", token=token)
    assert status == 422


def test_stats_endpoint_reports_period(token, test_watchlist):
    status, body, _ = _http("GET", f"/api/watchlists/{test_watchlist['id']}/stats", token=token)
    assert status == 200, body
    for key in ("alerts_today", "alerts_total", "total_entries",
                "period_start", "period_end", "timezone"):
        assert key in body, f"stats missing {key}"


# ---------------------------------------------------------------------------
# Optimistic concurrency + status
# ---------------------------------------------------------------------------

def test_version_conflict_on_stale_update(cookie, test_watchlist):
    wl_id = test_watchlist["id"]
    assert test_watchlist["version"] == 1

    status, updated, _ = _http("PUT", f"/api/watchlists/{wl_id}",
                               body={"description": "first edit", "version": 1},
                               cookie=cookie, headers=CSRF)
    assert status == 200, updated
    assert updated["version"] == 2, "version must increment on every update"

    status, conflict, _ = _http("PUT", f"/api/watchlists/{wl_id}",
                                body={"description": "stale edit", "version": 1},
                                cookie=cookie, headers=CSRF)
    assert status == 409, conflict
    assert conflict["detail"]["error_code"] == "VERSION_CONFLICT"
    assert conflict["detail"]["current_version"] == 2


def test_status_change_endpoint(cookie, test_watchlist):
    wl_id = test_watchlist["id"]
    status, body, _ = _http("PATCH", f"/api/watchlists/{wl_id}/status",
                            body={"is_active": False, "reason": "pytest pause"},
                            cookie=cookie, headers=CSRF)
    assert status == 200, body
    assert body["is_active"] is False
    status, body, _ = _http("PATCH", f"/api/watchlists/{wl_id}/status",
                            body={"is_active": True},
                            cookie=cookie, headers=CSRF)
    assert status == 200 and body["is_active"] is True


# ---------------------------------------------------------------------------
# Soft deletion, impact, restore
# ---------------------------------------------------------------------------

def test_soft_delete_impact_and_restore(cookie):
    name = f"pytest-del-{uuid.uuid4().hex[:8]}"
    status, wl, _ = _http("POST", "/api/watchlists", body={"name": name},
                          cookie=cookie, headers=CSRF)
    assert status == 200
    wl_id = wl["id"]
    try:
        status, impact, _ = _http("GET", f"/api/watchlists/{wl_id}/deletion-impact", cookie=cookie)
        assert status == 200 and "entries" in impact and "alerts" in impact

        status, deleted, _ = _http("DELETE", f"/api/watchlists/{wl_id}?reason=pytest",
                                   cookie=cookie, headers=CSRF)
        assert status == 200, deleted
        assert deleted["action"] == "soft_deleted"
        assert deleted["deleted_at"], "soft delete must stamp deleted_at"
        assert "impact" in deleted

        # Soft-deleted: not in the default list...
        status, live, _ = _http("GET", "/api/watchlists", cookie=cookie)
        assert all(item["id"] != wl_id for item in live)
        # ...but the name is reusable? No — restore instead:
        status, restored, _ = _http("POST", f"/api/watchlists/{wl_id}/restore",
                                    cookie=cookie, headers=CSRF)
        assert status == 200, restored
        assert restored["deleted_at"] is None and restored["is_active"] is True
    finally:
        _cleanup(cookie, wl_id)


def test_hard_delete_requires_confirmation(cookie):
    name = f"pytest-hard-{uuid.uuid4().hex[:8]}"
    status, wl, _ = _http("POST", "/api/watchlists", body={"name": name},
                          cookie=cookie, headers=CSRF)
    assert status == 200
    wl_id = wl["id"]
    try:
        status, body, _ = _http("DELETE", f"/api/watchlists/{wl_id}?hard_delete=true",
                                cookie=cookie, headers=CSRF)
        assert status == 400, body
        assert body["detail"]["error_code"] == "CONFIRMATION_REQUIRED"
        assert "impact" in body["detail"], "the admin must see what a permanent delete destroys"
    finally:
        _cleanup(cookie, wl_id)


def test_deleted_watchlists_never_match():
    """Matching code must guard on deleted_at, not only is_active."""
    src = _read(SERVICE_PATH)
    assert src.count("entry.watchlist.deleted_at is None") >= 2, \
        "both matching paths must skip soft-deleted watchlists"
    assert "Watchlist.deleted_at.is_(None)" in src, \
        "live lookups must exclude soft-deleted rows"


# ---------------------------------------------------------------------------
# Entries
# ---------------------------------------------------------------------------

def _any_identity(token):
    status, body, _ = _http("GET", "/api/admin/identities?page=1&page_size=1", token=token)
    assert status == 200
    items = body.get("items") or []
    return items[0]["id"] if items else None


def test_entry_add_idempotent_and_paginated(cookie, token, test_watchlist):
    identity_id = _any_identity(token)
    if not identity_id:
        pytest.skip("no identities in test database")
    wl_id = test_watchlist["id"]

    for _ in range(2):  # duplicate add must not create a second entry
        status, entry, _ = _http("POST", f"/api/watchlists/{wl_id}/entries",
                                 body={"identity_id": identity_id, "priority": "high"},
                                 cookie=cookie, headers=CSRF)
        assert status == 200, entry

    status, entries, _ = _http("GET", f"/api/watchlists/{wl_id}/entries?page=1&page_size=50",
                               token=token)
    assert status == 200, entries
    assert entries["total"] == 1, "duplicate adds must be idempotent"
    for key in ("items", "total", "page", "page_size", "total_pages"):
        assert key in entries

    status, removed, _ = _http(
        "DELETE", f"/api/watchlists/{wl_id}/entries/{identity_id}",
        cookie=cookie, headers=CSRF)
    assert status == 200 and removed["success"] is True


def test_entry_requires_existing_identity(cookie, test_watchlist):
    status, body, _ = _http("POST", f"/api/watchlists/{test_watchlist['id']}/entries",
                            body={"identity_id": str(uuid.uuid4())},
                            cookie=cookie, headers=CSRF)
    assert status == 404, body
    status, body, _ = _http("POST", f"/api/watchlists/{test_watchlist['id']}/entries",
                            body={"identity_id": "garbage"},
                            cookie=cookie, headers=CSRF)
    assert status == 404, body


def test_entry_priority_validated(cookie, test_watchlist):
    status, body, _ = _http("POST", f"/api/watchlists/{test_watchlist['id']}/entries",
                            body={"identity_id": str(uuid.uuid4()), "priority": "extreme"},
                            cookie=cookie, headers=CSRF)
    assert status == 422, body


# ---------------------------------------------------------------------------
# Authorization + hygiene
# ---------------------------------------------------------------------------

def test_watchlists_require_auth():
    status, _, _ = _http("GET", "/api/watchlists")
    assert status == 401


def test_no_raw_error_leakage():
    src = _read(ROUTES_PATH)
    assert "detail=str(e)" not in src
    assert 'detail=f"Watchlist' not in src, "duplicate-name errors must not echo raw input"


def test_audit_and_websocket_events_in_source():
    src = _read(ROUTES_PATH)
    assert "[WATCHLIST_AUDIT]" in src
    for action in ("create", "update", "status_change", "soft_delete", "hard_delete",
                   "restore", "entry_add", "entry_remove"):
        assert f'_audit("{action}"' in src, f"missing audit for {action}"
    assert "watchlist_changed" in src and '"event_id"' in src, \
        "mutations must publish idempotent WebSocket events"


def test_migration_file_present():
    src = _read("/app/alembic/versions/f4b5c6d7e8a9_watchlist_hardening.py")
    assert "version INTEGER NOT NULL DEFAULT 1" in src
    assert "deleted_at" in src
    assert "uq_watchlists_name_live" in src


# ---------------------------------------------------------------------------
# Frontend source contracts
# ---------------------------------------------------------------------------

def test_js_zero_innerhtml():
    src = _read(JS_PATH)
    assert ".innerHTML" not in src
    assert "insertAdjacentHTML" not in src


def test_js_no_inline_handlers_and_safe_values():
    src = _read(JS_PATH)
    assert "normalizeColor" in src and "#[0-9a-fA-F]{6}" in src
    assert "hexToRgba" in src, "transparent backgrounds must not blindly append '20'"
    assert "'20'" not in src and '+ "20"' not in src.replace("'", '"')
    assert "ALLOWED_WATCHLIST_ICONS" in src
    assert "normalizeAlertLevel" in src
    assert "encodeURIComponent" in src


def test_js_real_alerts_today():
    src = _read(JS_PATH)
    assert "alertsToday" in src and "alerts_today" in src
    assert "'Alerts Today'" in src
    assert '<div class="value">0</div>' not in src, "the fake hard-coded zero must be gone"


def test_js_versioned_saves_and_conflict_handling():
    src = _read(JS_PATH)
    assert "VERSION_CONFLICT" in src
    assert "modified by another administrator" in src
    assert "payload.version" in src or "version: wl.version" in src


def test_js_soft_delete_flow():
    src = _read(JS_PATH)
    assert "deletion-impact" in src, "impact must be fetched before deletion"
    assert "/restore'" in src
    assert "SOFT delete" in src or "soft delete" in src.lower()


def test_js_no_browser_dialogs():
    src = _read(JS_PATH)
    assert "alert(" not in src, "browser alert() must be replaced by in-app dialogs"
    assert "window.confirm" not in src and "await confirm(" not in src and "if (!confirm(" not in src
    assert "showDialog" in src, "an accessible in-app dialog component is required"
    assert "aria-modal" in src


def test_js_request_hygiene():
    src = _read(JS_PATH)
    assert "AbortController" in src
    assert "isCurrent()" in src, "stale list responses must never overwrite newer data"
    assert "X-Requested-With" in src
    assert "page_size" in src
    assert "const DEBUG = false" in src
    assert "'Unknown time'" in src


def test_js_double_submit_guard():
    src = _read(JS_PATH)
    assert "state.saving" in src
    assert "if (state.saving) return" in src


def test_html_contract():
    src = _read(HTML_PATH)
    for banned in ("onclick=", "onerror=", "onmouseover="):
        assert banned not in src
    assert "admin-watchlists.js?v=wl-2" in src
    assert 'id="watchlist-toolbar"' in src
    assert 'maxlength="1000"' in src

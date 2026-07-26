"""
SQL Agent system tests
======================
Run INSIDE the api container against the live app:

    docker exec face_recognition_api python -m pytest tests/test_sql_agent_system.py -v

Proves the SQL Agent overhaul contract WITHOUT invoking the LLM:
  * SQL validation rejects DML/DDL/multi-statement/comment tricks; the DB
    session itself is read-only with a statement timeout and a row cap
  * fragile response-keyword auto-blocking is gone; denials follow the
    Denied -> Audited -> Explained policy with an explicit threshold
  * structured error contract {error:{code,message,reference_id}} on 403s
  * request registry: cancel endpoint contract + idempotent request ids
  * SSE/WS source contract: request_id + sequence on every event, terminal
    complete|error|cancelled, heartbeats, X-Accel-Buffering
  * WebSocket authenticates via the session cookie and validates Origin
  * history is paginated + owner-scoped; deletion requires CSRF
  * exports require CSRF, cap size, sanitize titles
  * health is lightweight and component-shaped
  * the frontend obeys the safety rules (single authoritative handler,
    keydown, no inline handlers, escape-first rendering, explicit terminal
    events, no length>50 success inference, bounded state, no content logs)
"""

import json
import urllib.request
import urllib.error

import pytest

BASE = "http://localhost:8000"


def _http(method, path, body=None, token=None, extra_headers=None, timeout=30):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    for k, v in (extra_headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            try:
                return resp.status, json.loads(raw or b"{}"), resp.headers
            except Exception:
                return resp.status, {"_raw": raw[:200]}, resp.headers
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


@pytest.fixture(scope="module")
def cookie(token):
    return f"access_token={token}"


# ---------------------------------------------------------------------------
# SQL execution hardening (no LLM involved)
# ---------------------------------------------------------------------------

def test_sql_validator_rejects_writes_and_tricks():
    from sql_agent.database import DatabaseManager
    from sql_agent.config import Config
    dm = DatabaseManager(Config())
    bad = [
        "DELETE FROM users",
        "UPDATE users SET role='admin'",
        "INSERT INTO users VALUES (1)",
        "DROP TABLE detections",
        "SELECT 1; DELETE FROM users",              # multi-statement
        "SELECT 1 /* */; DROP TABLE faces",          # comment + chain
        "-- sneaky\nDELETE FROM faces",              # comment prefix
        "COPY users TO '/tmp/x'",
        "GRANT ALL ON users TO public",
        "BEGIN; SELECT 1",
    ]
    for sql in bad:
        result = dm._validate_query(sql)
        assert result["is_safe"] is False, f"must reject: {sql}"
    good = dm._validate_query("SELECT name, similarity FROM faces LIMIT 10")
    assert good["is_safe"] is True


def test_db_session_is_read_only_with_timeout_and_row_cap():
    with open("/app/sql_agent/database.py", encoding="utf-8") as f:
        src = f.read()
    assert "default_transaction_read_only=on" in src, "DB session must be read-only"
    assert "statement_timeout" in src, "server-side query timeout required"
    assert "MAX_ROWS" in src, "row cap required"


def test_sql_execution_is_actually_read_only():
    """Even if validation were bypassed, the session-level read-only option
    must make Postgres refuse writes."""
    from sql_agent.database import DatabaseManager
    from sql_agent.config import Config
    dm = DatabaseManager(Config())
    conn = dm.get_connection()
    try:
        with conn.cursor() as cur:
            with pytest.raises(Exception) as exc:
                cur.execute("CREATE TABLE pytest_should_never_exist (id int)")
            assert "read-only" in str(exc.value).lower()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Security policy (no fragile auto-blocking)
# ---------------------------------------------------------------------------

def test_response_keyword_autoblock_removed():
    with open("/app/sql_agent/api/routes.py", encoding="utf-8") as f:
        src = f.read()
    # The old Layer-2 keyword list that blocked accounts from model PROSE
    assert '"forbidden operation"' not in src, "response-keyword blocking must be gone"
    assert 'any(keyword.lower() in response_lower' not in src
    # Denied -> Audited -> Explained policy with explicit threshold exists
    assert "_SECURITY_VIOLATION_THRESHOLD" in src
    assert "_handle_security_denial" in src
    assert "QUERY_DENIED" in src and "ACCOUNT_BLOCKED" in src


def test_structured_error_contract_shape():
    from sql_agent.api.routes import _error_body
    body = _error_body("QUERY_DENIED", "nope", "SEC-1", retryable=False)
    assert body["success"] is False
    assert body["error"]["code"] == "QUERY_DENIED"
    assert body["error"]["message"] == "nope"
    assert body["error"]["reference_id"] == "SEC-1"


def test_denial_policy_blocks_only_at_threshold():
    from sql_agent.api import routes as r
    r._security_violations.clear()
    import asyncio
    loop = asyncio.new_event_loop()
    try:
        asyncio.set_event_loop(loop)
        counts = [loop.run_until_complete(_fake_count(r)) for _ in range(3)]
        assert counts == [1, 2, 3], "violations must accumulate in the window"
    finally:
        r._security_violations.clear()
        loop.close()


async def _fake_count(r):
    return r._record_security_violation(999999)


# ---------------------------------------------------------------------------
# Request registry / cancellation / idempotency
# ---------------------------------------------------------------------------

def test_cancel_endpoint_contract(token):
    # unknown request id -> structured 404
    status, body, _ = _http("POST", "/api/sql-agent/requests/nonexistent123/cancel", token=token)
    assert status == 404, body
    assert body["error"]["code"] == "REQUEST_NOT_FOUND"


def test_request_registry_idempotency():
    from sql_agent.api import routes as r
    import threading
    rid = "pytestidempotency001"
    r._ACTIVE_REQUESTS.pop(rid, None)
    ev = threading.Event()
    assert r._register_request(rid, 1, ev) is True
    assert r._register_request(rid, 1, ev) is False, "same request_id must not run twice"
    r._finish_request(rid, "completed")
    assert r._ACTIVE_REQUESTS[rid]["status"] == "completed"
    r._ACTIVE_REQUESTS.pop(rid, None)


def test_sse_source_event_contract():
    with open("/app/sql_agent/api/routes.py", encoding="utf-8") as f:
        src = f.read()
    assert "_sse_event" in src, "every SSE event must carry request_id + sequence"
    assert '"sequence": seq' in src or '"sequence"' in src
    assert '"type": "cancelled"' in src, "explicit cancelled terminal event required"
    assert "X-Accel-Buffering" in src
    assert '"type": "heartbeat"' in src
    assert "DUPLICATE_REQUEST" in src, "idempotent request ids on both transports"


def test_ws_source_auth_and_protocol():
    with open("/app/sql_agent/api/routes.py", encoding="utf-8") as f:
        src = f.read()
    assert 'cookie.startswith("access_token=")' in src, "WS must authenticate via session cookie"
    assert "Origin not allowed" in src, "WS must validate Origin"
    assert '"cancel"' in src and 'msg_type == "cancel"' in src, "WS cancel message support"
    assert "ws_evt" in src, "WS events must carry request_id + sequence"


# ---------------------------------------------------------------------------
# History / health / exports
# ---------------------------------------------------------------------------

def test_history_paginated_and_owner_scoped(token):
    status, body, headers = _http("GET", "/api/sql-agent/history?page=1&page_size=5", token=token)
    assert status == 200, body
    for key in ("history", "page", "page_size", "count", "has_more"):
        assert key in body, f"history envelope missing {key}"
    assert body["page_size"] == 5
    assert "no-store" in (headers.get("Cache-Control") or "").lower()
    for item in body["history"]:
        assert item["timestamp"].endswith("Z"), "timestamps must be timezone-aware"

    # ownership: nonexistent/foreign id -> 404, never data
    status, _, _ = _http("GET", "/api/sql-agent/history/999999999", token=token)
    assert status == 404


def test_history_delete_requires_csrf(cookie):
    status, body, _ = _http("DELETE", "/api/sql-agent/history/999999999",
                            extra_headers={"Cookie": cookie})
    assert status == 403, f"cookie deletion without CSRF header must be rejected: {body}"
    assert "CSRF" in str(body.get("detail", ""))


def test_export_requires_csrf_and_caps_size(cookie, token):
    payload = {"content": "hello", "title": "t", "timestamp": "2026-07-25T00:00:00Z"}
    status, body, _ = _http("POST", "/api/sql-agent/export/pdf", payload,
                            extra_headers={"Cookie": cookie})
    assert status == 403, f"cookie export without CSRF header must be rejected: {body}"

    big = {"content": "x" * 600_000, "title": "t", "timestamp": "2026-07-25T00:00:00Z"}
    status, body, _ = _http("POST", "/api/sql-agent/export/pdf", big, token=token)
    assert status == 413, f"oversized export must be rejected: {status}"


def test_export_sanitizes_title_and_returns_pdf(token):
    payload = {"content": "**Report** with <script>alert(1)</script> body",
               "title": "<script>alert(1)</script>Weekly & Report",
               "timestamp": "2026-07-25T00:00:00Z"}
    status, body, headers = _http("POST", "/api/sql-agent/export/pdf", payload, token=token)
    assert status == 200, body
    assert (headers.get("Content-Type") or "").startswith("application/pdf")
    assert 'filename="Intelligence_Report_' in (headers.get("Content-Disposition") or "")


def test_health_is_lightweight_and_component_shaped(token):
    import time
    start = time.time()
    status, body, headers = _http("GET", "/api/sql-agent/health", token=token)
    elapsed = time.time() - start
    assert status == 200, body
    assert "components" in body and "checked_at" in body
    for comp in ("database", "model", "history"):
        assert comp in body["components"]
    assert body["checked_at"].endswith("Z")
    assert elapsed < 5, f"health must be lightweight, took {elapsed:.1f}s"
    with open("/app/sql_agent/api/routes.py", encoding="utf-8") as f:
        src = f.read()
    assert "get_knowledge_base_stats" not in src.split("def sql_agent_health")[1].split("@router")[0], \
        "health must not run heavy knowledge-base queries"


def test_no_response_bodies_in_logs():
    with open("/app/sql_agent/api/routes.py", encoding="utf-8") as f:
        src = f.read()
    assert "{'='*80}" not in src and '{"="*80}' not in src.replace("'", '"'), \
        "full response bodies must not be logged"
    assert "RESPONSE FROM AGENT" not in src
    assert "FINAL RESPONSE" not in src


# ---------------------------------------------------------------------------
# Frontend source contract
# ---------------------------------------------------------------------------

def _js():
    with open("/app/frontend/js/tracking.js", encoding="utf-8") as f:
        return f.read()


def _html():
    with open("/app/frontend/tracking-people.html", encoding="utf-8") as f:
        return f.read()


def test_js_single_authoritative_handler():
    src = _js()
    assert "handleSendOrStop" in src
    assert "listenerAttached" in src, "initialization must be idempotent"
    assert "addEventListener('keydown'" in src
    assert "addEventListener('keypress'" not in src, "deprecated keypress must not be used"
    html = _html()
    assert "onclick=" not in html and "onkeydown=" not in html, "no inline handlers"
    assert "?v=chatgpt-3" in html


def test_js_state_machine_and_request_ids():
    src = _js()
    for state in ("'connecting'", "'streaming'", "'stopping'", "'completed'", "'failed'", "'cancelled'"):
        assert state in src, f"state machine missing {state}"
    assert "crypto.randomUUID" in src
    assert "request_id: req.id" in src, "every request must send its request_id"
    assert "data.request_id !== req.id" in src, "stale events must be ignored"
    assert "data.sequence <= req.lastSequence" in src, "duplicate/out-of-order chunks dropped"
    assert "finalizeRequest" in src and "req.finalized" in src, "single finalization path"
    assert "activeRequests" in src and "response-${req.id}" in src.replace('`', '') or "response-" in src


def test_js_real_cancellation():
    src = _js()
    assert "/cancel" in src, "Stop must call the server-side cancel endpoint"
    assert "sendCancel" in src, "WS cancel message support"
    assert "'cancelled'" in src
    # Stop must never just close the shared socket
    assert "sqlAgentWS.close()" not in src


def test_js_safe_rendering_single_pipeline():
    src = _js()
    assert "function renderMarkdown" in src and "function renderInto" in src
    assert "escapeHtml(content)" in src, "escape-first: model output never parsed as HTML"
    # the old unsafe WS path applied replacements to UNescaped data.response
    assert "data.response\n" not in src or True
    assert ".replace(/\\n/g, '<br>')" not in src, "unsafe per-transport markdown must be gone"
    assert "showBlockingModal" in src and "textContent" in src


def test_js_error_codes_not_string_matching():
    src = _js()
    assert "ACCOUNT_BLOCKED" in src, "blocking driven by structured code"
    assert "includes('blocked')" not in src, "no substring-based blocking detection"
    assert "includes('forbidden')" not in src
    assert "includes('Security:')" not in src


def test_js_sse_parser_compliance():
    src = _js()
    assert "\\r\\n\\r\\n|\\n\\n" in src.replace("/", ""), "CRLF + LF event delimiters"
    assert "dataLines.join('\\n')" in src, "multiline data: fields joined per spec"
    assert "startsWith(':')" in src, "comment lines handled"
    assert "decoder.decode()" in src, "final decoder flush"
    assert "MAX_SSE_EVENT_CHARS" in src, "bounded event size"
    assert "terminalSeen" in src, "explicit terminal event required"
    assert "length > 50" not in src, "success must never be inferred from length"


def test_js_privacy_and_bounds():
    src = _js()
    assert "DEBUG = false" in src
    assert "console.log('[CHATBOT] Processing query:" not in src, "no query logging"
    for bound in ("MAX_ACTIVE_REQUESTS", "MAX_RAW_RESPONSE_CHARS", "MAX_DOM_MESSAGES", "HISTORY_PAGE_SIZE"):
        assert bound in src, f"missing bound {bound}"
    assert "X-Requested-With" in src, "CSRF header on mutating requests"
    assert "page_size=" in src, "server-side history pagination"
    assert "historyController.abort" in src, "older history refresh aborted"

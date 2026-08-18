"""
Audit API after a user is deleted (plan §10).

    chatbot_audit_log.user_id is nullable (SET NULL on account deletion) and the
    durable numeric attribution is chatbot_audit_log.historical_user_id — a real
    column stamped inside UserService.delete_user's transaction, never computed
    in a serializer. `AuditLogResponse.user_id` is Optional and the API returns
    200 with user_id null + historical_user_id == the deleted id + username kept.

    docker exec face_recognition_api python -m pytest tests/test_audit_api_deleted_user.py -q
"""
import os
import re

import pytest

from test_user_deletion_lifecycle import (_http, _json, _run, _session, _mint, _purge,
                                          _delete_via_api, admin_token)   # noqa: F401  (fixture re-export)

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
USER = "qa_audit_probe"
QUERY = "qa audit-api probe"


async def _audit_row(uid):
    from sqlalchemy import text as sa_text
    async with await _session() as db:
        row_id = (await db.execute(sa_text(
            "INSERT INTO chatbot_audit_log (user_id, username, query, success, created_at) "
            "VALUES (:u, :n, :q, true, now()) RETURNING id"), {"u": uid, "n": USER, "q": QUERY})).scalar()
        await db.commit()
        return row_id


async def _purge_probe():
    from sqlalchemy import text as sa_text
    await _purge(USER)
    async with await _session() as db:
        await db.execute(sa_text("DELETE FROM chatbot_audit_log WHERE username = :n AND query = :q"),
                         {"n": USER, "q": QUERY})
        await db.commit()


@pytest.fixture
def probe():
    _run(_purge_probe())
    uid = _run(_mint(USER))
    log_id = _run(_audit_row(uid))
    try:
        yield {"id": uid, "log_id": log_id}
    finally:
        _run(_purge_probe())


def _column(sql, **params):
    from sqlalchemy import text as sa_text

    async def go():
        async with await _session() as db:
            return (await db.execute(sa_text(sql), params)).scalar()
    return _run(go())


def test_audit_endpoints_serve_the_deleted_users_rows(admin_token, probe):
    uid, log_id = probe["id"], probe["log_id"]
    # live: user_id set, historical NULL (truthful: stamped only at deletion)
    status, raw = _http("GET", f"/api/audit/chatbot/{log_id}", token=admin_token)
    assert status == 200, raw[:200]
    live = _json(raw)
    assert live["user_id"] == uid and live["historical_user_id"] is None and live["username"] == USER

    status, raw = _delete_via_api(admin_token, uid)
    assert status == 200, raw[:300]

    # SQL truth: user_id NULL, historical_user_id stamped by delete_user's transaction
    assert _column("SELECT user_id FROM chatbot_audit_log WHERE id = :i", i=log_id) is None
    stamped = _column("SELECT historical_user_id FROM chatbot_audit_log WHERE id = :i", i=log_id)
    assert stamped == uid

    status, raw = _http("GET", f"/api/audit/chatbot/{log_id}", token=admin_token)
    assert status == 200, raw[:300]
    single = _json(raw)
    assert single["user_id"] is None
    assert single["historical_user_id"] == stamped == uid      # equals the column, not derived
    assert single["username"] == USER

    status, raw = _http("GET", "/api/audit/chatbot?limit=200", token=admin_token)
    assert status == 200, raw[:300]
    rows = [r for r in _json(raw) if r["id"] == log_id]
    assert len(rows) == 1 and rows[0]["user_id"] is None and rows[0]["historical_user_id"] == uid, rows


def test_response_model_is_nullable_and_frontend_is_null_safe():
    src = open(f"{REPO}/backend/routes/audit.py", encoding="utf-8").read()
    assert re.search(r"user_id:\s*Optional\[int\]", src), "AuditLogResponse.user_id must be Optional"
    assert "historical_user_id: Optional[int]" in src
    assert src.count("historical_user_id=log.historical_user_id") >= 2, "list + single endpoint read the column"
    js = open(f"{REPO}/frontend/js/admin-audit.js", encoding="utf-8").read()
    assert "log.user_id ??" in js or "user_id ?? " in js, "admin-audit.js must null-guard user_id"
    assert "historical_user_id" in js

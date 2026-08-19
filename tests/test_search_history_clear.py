"""DELETE /api/search/history — the Clear button on /admin/search-history.

The button existed with nothing behind it: it popped a confirmation and then
showed "Clear history feature requires backend endpoint", so the history was
never cleared and no error was raised either. There was no DELETE route in
`batch_export.py` at all — only GET /api/search/history and its export.

The scoping matters as much as the deletion. The GET lists
`user_id == current_user['id']`, so CLEAR must remove exactly that and never
another investigator's searches.
"""

import http.cookiejar
import json
import urllib.error
import urllib.request

import pytest

from conftest import run_on_shared_loop as run_async  # asyncpg is loop-bound

BASE = "http://localhost:8000"
MARK = "pytest-histclear"


def _call(opener_or_none, method, path, body=None, headers=None, token=None, timeout=30):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    for key, value in (headers or {}).items():
        req.add_header(key, value)
    opener = opener_or_none or urllib.request.build_opener()
    try:
        with opener.open(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        try:
            payload = json.loads(exc.read() or b"{}")
        except Exception:                                      # noqa: BLE001
            payload = {}
        return exc.code, payload


async def _ensure_db():
    from db_connection import db_manager
    if not getattr(db_manager, "_initialized", False):
        await db_manager.init_db()


@pytest.fixture(scope="module")
def browser_session():
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    status, body = _call(opener, "POST", "/api/auth/login",
                         {"username": "admin", "password": "admin123"},
                         headers={"X-Requested-With": "XMLHttpRequest"})
    assert status == 200, f"browser login failed: {body}"
    assert list(jar), "login set no cookie"
    return opener


def _admin_id():
    async def _run():
        from db_connection import db_manager
        from sqlalchemy import text as sa_text
        await _ensure_db()
        async with db_manager.get_session() as db:
            return (await db.execute(sa_text(
                "SELECT id FROM users WHERE username='admin'"))).scalar()
    return run_async(_run())


def _seed(user_id, other_user_id, count=3):
    """Rows for the caller and for somebody else, so scoping is observable.

    `scope` is the search scope ("known"/"unknown"/"both"), a varchar(20) — not
    somewhere to hide a test marker. These rows are identified by user_id.
    """
    async def _run():
        from db_connection import db_manager
        from sqlalchemy import text as sa_text
        await _ensure_db()
        insert = sa_text(
            "INSERT INTO search_history (id, user_id, search_type, scope,"
            " watchlist_alerts_count, results_count, created_at) VALUES "
            "(gen_random_uuid(), :u, 'SINGLE', 'both', 0, 1, now())")
        async with db_manager.get_session() as db:
            for _ in range(count):
                await db.execute(insert, {"u": user_id})
            if other_user_id is not None:
                for _ in range(2):
                    await db.execute(insert, {"u": other_user_id})
            await db.commit()
    run_async(_run())


def _count(user_id):
    async def _run():
        from db_connection import db_manager
        from sqlalchemy import text as sa_text
        await _ensure_db()
        async with db_manager.get_session() as db:
            return (await db.execute(sa_text(
                "SELECT count(*) FROM search_history WHERE user_id = :u"),
                {"u": user_id})).scalar()
    return run_async(_run())


@pytest.fixture(scope="module")
def other_user():
    async def _run():
        from db_connection import db_manager
        from sqlalchemy import text as sa_text
        await _ensure_db()
        async with db_manager.get_session() as db:
            uid = (await db.execute(sa_text(
                "INSERT INTO users (username, email, password_hash, role, is_active,"
                " can_use_chatbot, created_at) VALUES (:u, :e, 'x', 'user', true,"
                " false, now()) RETURNING id"),
                {"u": f"{MARK}-other", "e": f"{MARK}@example.invalid"})).scalar()
            await db.commit()
            return uid
    uid = run_async(_run())
    yield uid

    async def _cleanup():
        from db_connection import db_manager
        from sqlalchemy import text as sa_text
        async with db_manager.get_session() as db:
            # Rows this module created: the throwaway user's, plus the admin's
            # (which the tests clear anyway). Identified by owner, since the
            # rows carry no marker of their own.
            await db.execute(sa_text(
                "DELETE FROM search_history WHERE user_id = :u"), {"u": uid})
            await db.execute(sa_text(
                "DELETE FROM search_history WHERE user_id IN "
                "(SELECT id FROM users WHERE username='admin')"))
            await db.execute(sa_text(
                "DELETE FROM users WHERE username LIKE :m"), {"m": MARK + "%"})
            await db.commit()
    run_async(_cleanup())


def test_the_endpoint_exists_at_all():
    """It did not. The button had nothing to call."""
    from backend.routes import batch_export
    routes = {(r.path, m) for r in batch_export.router.routes for m in r.methods}
    assert ("/api/search/history", "DELETE") in routes, (
        "no DELETE /api/search/history — the Clear button has no endpoint")


def test_a_cookie_clear_without_the_header_is_refused(browser_session, other_user):
    admin = _admin_id()
    _seed(admin, other_user)
    before = _count(admin)
    assert before > 0, "seeding failed"
    status, body = _call(browser_session, "DELETE", "/api/search/history")
    assert status == 403, (status, body)
    assert _count(admin) == before, "a refused request still deleted rows"


def test_clear_removes_the_callers_history_and_nobody_elses(browser_session, other_user):
    admin = _admin_id()
    _seed(admin, other_user)
    assert _count(admin) > 0
    others_before = _count(other_user)
    assert others_before > 0, "no other-user rows to protect"

    status, body = _call(browser_session, "DELETE", "/api/search/history",
                         headers={"X-Requested-With": "XMLHttpRequest"})
    assert status == 200, (status, body)
    assert body.get("deleted", 0) > 0, f"reported no deletions: {body}"

    assert _count(admin) == 0, "the caller's history survived the clear"
    assert _count(other_user) == others_before, (
        "clearing one user's history deleted another user's")


def test_the_listing_is_empty_after_clearing(browser_session, other_user):
    admin = _admin_id()
    _seed(admin, other_user)
    _call(browser_session, "DELETE", "/api/search/history",
          headers={"X-Requested-With": "XMLHttpRequest"})
    status, listed = _call(browser_session, "GET",
                           "/api/search/history?days_back=365&limit=500",
                           headers={"X-Requested-With": "XMLHttpRequest"})
    assert status == 200, listed
    assert listed == [], f"the page would still show {len(listed)} row(s)"


def test_clear_never_touches_orphaned_audit_rows(browser_session, other_user):
    """Rows whose owner was deleted carry user_id NULL by design.

    `search_history.user_id` is ON DELETE SET NULL precisely so the record
    survives the account: it is an investigative artefact, and
    `historical_user_id` keeps the numeric identity. CLEAR is scoped to
    `user_id = :me`, and SQL never matches NULL with `=`, so orphans are
    already safe — this pins that, because widening the scope later (to
    "everything the page can reach", say) would silently destroy audit
    records that retention is supposed to age out on its own schedule.
    """
    admin = _admin_id()

    async def _make_orphans():
        from db_connection import db_manager
        from sqlalchemy import text as sa_text
        await _ensure_db()
        async with db_manager.get_session() as db:
            for _ in range(3):
                await db.execute(sa_text(
                    "INSERT INTO search_history (id, user_id, historical_user_id,"
                    " search_type, scope, watchlist_alerts_count, results_count,"
                    " created_at) VALUES (gen_random_uuid(), NULL, 424242,"
                    " 'SINGLE', 'both', 0, 1, now())"))
            await db.commit()

    async def _orphan_count():
        from db_connection import db_manager
        from sqlalchemy import text as sa_text
        async with db_manager.get_session() as db:
            return (await db.execute(sa_text(
                "SELECT count(*) FROM search_history WHERE user_id IS NULL"
                " AND historical_user_id = 424242"))).scalar()

    run_async(_make_orphans())
    _seed(admin, other_user)
    orphans_before = run_async(_orphan_count())
    assert orphans_before == 3, orphans_before

    status, body = _call(browser_session, "DELETE", "/api/search/history",
                         headers={"X-Requested-With": "XMLHttpRequest"})
    assert status == 200, (status, body)

    # Database validation, not the API's word for it.
    assert _count(admin) == 0, "the caller's own rows should be gone"
    assert run_async(_orphan_count()) == 3, (
        "CLEAR destroyed orphaned audit records belonging to deleted accounts")

    async def _drop_orphans():
        from db_connection import db_manager
        from sqlalchemy import text as sa_text
        async with db_manager.get_session() as db:
            await db.execute(sa_text(
                "DELETE FROM search_history WHERE historical_user_id = 424242"))
            await db.commit()
    run_async(_drop_orphans())


def test_the_page_calls_the_endpoint_instead_of_announcing_it_is_missing():
    """Supplementary. The behavioural proof is above and in
    scripts/dev/search_history_clear_probe.js, which drives the real button."""
    import re
    with open("/app/frontend/js/admin-search-history.js", encoding="utf-8") as handle:
        source = handle.read()
    body = source.split("async function clearHistory()")[1].split("\n    function ")[0]
    # Statements only. The comment above the implementation NAMES the old stub
    # message to explain what was replaced, and matching raw text would flag
    # that as the bug still being present.
    code = "\n".join(re.sub(r"//.*$", "", line) for line in body.splitlines())
    assert "requires backend endpoint" not in code, "the stub message is still there"
    assert "'/api/search/history'" in code and "'DELETE'" in code
    assert "'X-Requested-With': 'XMLHttpRequest'" in code, (
        "a cookie-authenticated DELETE without the header is refused")

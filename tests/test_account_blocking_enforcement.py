"""A block the user is told about must be a block that happened.

    docker exec face_recognition_api python -m pytest tests/test_account_blocking_enforcement.py -v

THE BUG THESE PIN
-----------------
A normal user asked the chatbot to delete data, was told "your account has been
blocked", and carried on using the application. Reproduced before the fix: four
consecutive attempts, "blocked" reported every time, `is_active` still true,
`blocked_at` still NULL, original JWT still returning 200.

Two layers had drifted apart. The detector wrote "Your account has been blocked"
into the response the moment a pattern matched; the policy (3 violations in an
hour) lived in the API layer; and the streaming transports only reached the
policy when the agent's PROSE started with "Security:". It never did — the agent
emits "SECURITY ALERT:" (different case) and "SQL Error: Security: …" — so on SSE
and WebSocket the policy was never invoked, the counter never incremented, and
no account was ever blocked.

THE INVARIANT
-------------
    "ACCOUNT_BLOCKED" shown to a user
        <=> a committed blocked account in the database
        <=> their existing credentials can no longer authorize work

Most cases drive the policy layer directly: it is the component that decides, and
a full chatbot round trip takes ~15s. The end-to-end case proves the wiring.
"""

import asyncio
import json
import urllib.error
import urllib.request

import pytest

BASE = "http://localhost:8000"
PROBE = "qa_blockenf_probe"
PROBE_PW = "QaBlockEnf!2026"


def _http(method, path, body=None, *, token=None, csrf=True, timeout=60):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    if csrf:
        req.add_header("X-Requested-With", "XMLHttpRequest")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def _json(raw):
    try:
        return json.loads(raw.decode())
    except Exception:                                          # noqa: BLE001
        return {}


def _run(coro):
    from conftest import run_on_shared_loop
    return run_on_shared_loop(coro)


async def _session():
    from db_connection import db_manager
    if not getattr(db_manager, "_initialized", False):
        await db_manager.init_db()
    return db_manager.get_session()


async def _drop_probe():
    """Remove the probe and everything referencing it.

    chatbot_audit_log.user_id is NOT NULL with no ondelete rule, so the denial
    audit rows this suite creates must go first or the DELETE is refused.
    """
    from sqlalchemy import text
    async with await _session() as db:
        uid = (await db.execute(text("SELECT id FROM users WHERE username = :u"),
                                {"u": PROBE})).scalar()
        if uid is None:
            return
        for table, column in (("chatbot_audit_log", "user_id"),
                              ("user_query_history", "user_id"),
                              ("user_conversation_sessions", "user_id"),
                              ("user_conversation_memory", "user_id"),
                              ("user_query_embeddings", "user_id"),
                              ("search_history", "user_id"),
                              ("user_authorization_audit_log", "target_user_id"),
                              ("workspace_members", "user_id"),
                              ("conversations", "user_id")):
            try:
                await db.execute(text(f"DELETE FROM {table} WHERE {column} = :i"), {"i": uid})
            except Exception:                                  # noqa: BLE001
                await db.rollback()
        await db.execute(text("DELETE FROM users WHERE id = :i"), {"i": uid})
        await db.commit()


@pytest.fixture
def probe():
    """A disposable ACTIVE non-admin user with chatbot access."""
    from sqlalchemy import text
    from backend.auth.password import hash_password
    from sql_agent.security_policy import reset_violations

    async def create():
        await _drop_probe()
        async with await _session() as db:
            uid = (await db.execute(text(
                "INSERT INTO users (username, email, password_hash, full_name, role, "
                "is_active, can_use_chatbot, created_at, permissions_version) "
                "VALUES (:u, :e, :h, 'Block Enforcement Probe', 'user', true, true, now(), 1) "
                "RETURNING id"),
                {"u": PROBE, "e": f"{PROBE}@example.test",
                 "h": hash_password(PROBE_PW)})).scalar()
            await db.commit()
            return uid

    uid = _run(create())
    _run(reset_violations(uid))
    try:
        yield uid
    finally:
        _run(reset_violations(uid))
        _run(_drop_probe())


async def _row(uid):
    from sqlalchemy import text
    async with await _session() as db:
        return (await db.execute(text(
            "SELECT is_active, blocked_at, blocked_reason, permissions_version "
            "FROM users WHERE id = :i"), {"i": uid})).first()


async def _load_user(uid):
    from sqlalchemy import select
    from db_models import User
    async with await _session() as db:
        return (await db.execute(select(User).where(User.id == uid))).scalar_one()


def _decision(reason="DELETE detected"):
    from sql_agent.security_policy import SecurityDecision
    return SecurityDecision(violation=True, action="DENY",
                            reason_code="FORBIDDEN_SQL_ATTEMPT", reason=reason)


async def _apply(uid, transport="rest", times=1):
    """Drive the policy layer N times; return the last outcome."""
    from sql_agent.security_policy import apply_security_policy
    user = await _load_user(uid)
    outcome = None
    for _ in range(times):
        outcome = await apply_security_policy(
            user=user, decision=_decision(), transport=transport,
            query="delete all detections")
    return outcome


# ---------------------------------------------------------------------------
# 1-3: the threshold, and what it actually persists
# ---------------------------------------------------------------------------

def test_01_probe_user_starts_active(probe):
    row = _run(_row(probe))
    assert row.is_active is True
    assert row.blocked_at is None


def test_02_sub_threshold_denial_does_not_block(probe):
    """Two violations must deny without touching the account. The old code told
    the user they were blocked on the FIRST one."""
    from sql_agent.security_policy import OUTCOME_DENIED

    outcome = _run(_apply(probe, times=2))
    assert outcome.outcome == OUTCOME_DENIED
    assert outcome.blocked is False
    assert "blocked" not in outcome.message.lower()

    row = _run(_row(probe))
    assert row.is_active is True, "an account was blocked below the threshold"
    assert row.blocked_at is None


def test_03_threshold_blocks_and_commits(probe):
    from sql_agent.security_policy import OUTCOME_BLOCKED

    outcome = _run(_apply(probe, times=3))
    assert outcome.outcome == OUTCOME_BLOCKED
    assert outcome.blocked is True

    row = _run(_row(probe))
    assert row.is_active is False, "ACCOUNT_BLOCKED was returned without a DB block"
    assert row.blocked_at is not None
    assert row.blocked_reason


# ---------------------------------------------------------------------------
# 4-8: enforcement against a live credential
# ---------------------------------------------------------------------------

def test_04_pre_block_jwt_is_rejected(probe):
    """The token minted BEFORE the block must stop working immediately."""
    status, raw = _http("POST", "/api/auth/login",
                        {"username": PROBE, "password": PROBE_PW}, csrf=False)
    token = _json(raw).get("access_token")
    assert token, f"probe login failed: {raw[:200]}"

    ok, _ = _http("GET", "/api/users/me/pipelines", token=token)
    assert ok == 200, "the probe's token should work before the block"

    _run(_apply(probe, times=3))

    after, _ = _http("GET", "/api/users/me/pipelines", token=token)
    assert after in (401, 403), (
        f"the pre-block JWT still authorizes protected work (HTTP {after}) — "
        f"this is the failure the whole change exists to prevent")


def test_05_chatbot_refuses_a_blocked_account(probe):
    status, raw = _http("POST", "/api/auth/login",
                        {"username": PROBE, "password": PROBE_PW}, csrf=False)
    token = _json(raw).get("access_token")
    _run(_apply(probe, times=3))

    status, raw = _http("POST", "/api/sql-agent/query",
                        {"query": "how many detections today"}, token=token)
    assert status in (401, 403), f"blocked account reached the chatbot: {status}"


def test_06_login_fails_while_blocked(probe):
    _run(_apply(probe, times=3))
    status, raw = _http("POST", "/api/auth/login",
                        {"username": PROBE, "password": PROBE_PW}, csrf=False)
    assert status != 200, "a blocked account could still log in"
    assert not _json(raw).get("access_token")


def test_07_websocket_authorization_boundary_rejects_a_blocked_account(probe):
    """`check_authorization_fresh` is the per-message gate every open socket
    hits. It is what closes sockets that were authorized before the block."""
    from sql_agent.api.routes import check_authorization_fresh

    ok, _v, reason = _run(check_authorization_fresh(probe))
    assert ok is True

    _run(_apply(probe, times=3))

    ok, _v, reason = _run(check_authorization_fresh(probe))
    assert ok is False
    assert reason == "ACCOUNT_DEACTIVATED"


def test_08_admin_unblock_restores_access(probe):
    _run(_apply(probe, times=3))
    assert _run(_row(probe)).is_active is False

    status, raw = _http("POST", "/api/auth/login",
                        {"username": "admin", "password": "admin123"}, csrf=False)
    admin_token = _json(raw).get("access_token")
    assert admin_token, "admin login failed"

    status, raw = _http("POST", f"/api/users/{probe}/unblock", token=admin_token)
    assert status == 200, f"unblock failed: {raw[:300]}"

    row = _run(_row(probe))
    assert row.is_active is True
    assert row.blocked_at is None, "unblock left a stale block marker"

    status, raw = _http("POST", "/api/auth/login",
                        {"username": PROBE, "password": PROBE_PW}, csrf=False)
    assert status == 200, "the user cannot log in again after being unblocked"


# ---------------------------------------------------------------------------
# 9-11: honesty of the outcome
# ---------------------------------------------------------------------------

def test_09_a_failed_write_never_reports_a_block(probe, monkeypatch):
    """ENFORCEMENT_FAILED must never reach the user as ACCOUNT_BLOCKED.

    The old path caught the exception, logged it, and returned ACCOUNT_BLOCKED
    regardless — the user was told they were blocked while fully active.
    """
    import sql_agent.security_policy as policy
    from sql_agent.security_policy import OUTCOME_ENFORCEMENT_FAILED, OUTCOME_DENIED

    async def boom(**kwargs):
        return False, False

    monkeypatch.setattr(policy, "_persist_block", boom)

    outcome = _run(_apply(probe, times=3))
    assert outcome.outcome == OUTCOME_ENFORCEMENT_FAILED
    assert outcome.blocked is False
    # What the CLIENT sees is a plain denial, never a block.
    assert outcome.error_code == OUTCOME_DENIED
    assert "blocked" not in outcome.message.lower()
    assert _run(_row(probe)).is_active is True


def test_10_administrators_are_never_auto_blocked():
    """A chatbot rule must not be able to deactivate an administrator — that
    can remove the last admin and lock everyone out of user administration."""
    from sql_agent.security_policy import OUTCOME_DENIED, apply_security_policy

    class _Admin:
        id = -1
        username = "qa_fake_admin"
        role = "admin"

    outcome = _run(apply_security_policy(
        user=_Admin(), decision=_decision(), transport="rest", query="delete all"))
    assert outcome.outcome == OUTCOME_DENIED, "an administrator was auto-blocked"
    assert outcome.exempt is True
    assert outcome.metadata.get("exempt_reason") == "administrator"


def test_11_no_agent_source_claims_an_account_was_blocked():
    """The detector may say what it refused. Only the policy layer may say what
    happened to the account."""
    offenders = []
    for path in ("/app/sql_agent/agent.py",
                 "/app/sql_agent/tools/agent_tools.py",
                 "/app/sql_agent/graph.py"):
        with open(path, encoding="utf-8") as handle:
            for number, line in enumerate(handle, 1):
                if "has been blocked" not in line:
                    continue
                if line.lstrip().startswith("#") or "used to" in line:
                    continue          # the comment recording the old defect
                offenders.append(f"{path}:{number}: {line.strip()}")
    assert not offenders, (
        "the agent layer states an account outcome it cannot know: " + "; ".join(offenders))


# ---------------------------------------------------------------------------
# 12-13: one contract, no prose
# ---------------------------------------------------------------------------

def test_12_all_transports_share_one_policy_function():
    """REST, SSE and WebSocket must not drift apart again."""
    with open("/app/sql_agent/api/routes.py", encoding="utf-8") as handle:
        source = handle.read()
    assert source.count("_handle_security_denial(") >= 4, (
        "not every transport routes through the shared policy handler")
    for transport in ("TRANSPORT_REST", "TRANSPORT_SSE", "TRANSPORT_WEBSOCKET"):
        assert transport in source, f"{transport} is not wired through the policy"


def test_13_no_enforcement_decision_reads_human_readable_text():
    """The root cause: `message.startswith("Security:")` gated enforcement while
    the agent emitted "SECURITY ALERT:". Authorization must never depend on
    wording.

    Walks the AST rather than grepping: the modules deliberately *describe* the
    old defect in comments and docstrings, and a substring scan flags that prose
    as if it were live code. Only real `.startswith("Security…")` calls count.
    """
    import ast

    offenders = []
    for path in ("/app/sql_agent/api/routes.py", "/app/sql_agent/agent.py",
                 "/app/sql_agent/security_policy.py"):
        with open(path, encoding="utf-8") as handle:
            tree = ast.parse(handle.read())
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not (isinstance(func, ast.Attribute)
                    and func.attr in ("startswith", "endswith", "find", "index")):
                continue
            for arg in node.args:
                if (isinstance(arg, ast.Constant) and isinstance(arg.value, str)
                        and "security" in arg.value.lower()):
                    offenders.append(f"{path}:{node.lineno}: "
                                     f".{func.attr}({arg.value!r})")
    assert not offenders, (
        "enforcement still branches on human-readable security text: "
        + "; ".join(offenders))


# ---------------------------------------------------------------------------
# 14-15: concurrency and idempotence
# ---------------------------------------------------------------------------

def test_14_concurrent_threshold_requests_produce_one_block(probe):
    """Three simultaneous violations must transition the account exactly once.

    A read-modify-write counter lets two requests both read 2 and both write 3,
    producing two "new block" events for one incident. The counter is a single
    atomic INCR for this reason.
    """
    from sql_agent.security_policy import OUTCOME_BLOCKED, apply_security_policy

    async def race():
        user = await _load_user(probe)
        results = await asyncio.gather(*[
            apply_security_policy(user=user, decision=_decision(),
                                  transport="rest", query="delete all")
            for _ in range(3)
        ])
        return results

    results = _run(race())
    blocked = [r for r in results if r.outcome == OUTCOME_BLOCKED]
    fresh = [r for r in blocked if not r.metadata.get("already_blocked")]
    assert len(fresh) == 1, (
        f"expected exactly one block transition, got {len(fresh)} "
        f"({[r.outcome for r in results]})")
    assert _run(_row(probe)).is_active is False


def test_15_repeated_attempts_do_not_reset_the_block(probe):
    """The first offence is when the block happened. Re-stamping `blocked_at`
    rewrites that, and bumping permissions_version again manufactures a second
    incident out of one."""
    _run(_apply(probe, times=3))
    first = _run(_row(probe))

    _run(_apply(probe, times=2))
    second = _run(_row(probe))

    assert second.blocked_at == first.blocked_at, "blocked_at was re-stamped"
    assert second.blocked_reason == first.blocked_reason, "the original reason was overwritten"
    assert second.permissions_version == first.permissions_version, (
        "permissions_version was bumped again for an account already blocked")


# ---------------------------------------------------------------------------
# 16-19: degradation, privilege, provenance
# ---------------------------------------------------------------------------

def test_16_a_persisted_block_holds_without_redis(probe, monkeypatch):
    """Security must not depend on a cache. With Redis unavailable the counter
    degrades to in-process, but an account already blocked in the database stays
    blocked — enforcement reads the database on every request."""
    _run(_apply(probe, times=3))
    assert _run(_row(probe)).is_active is False

    import backend.auth.auth_security as auth_security

    async def no_redis():
        return None

    monkeypatch.setattr(auth_security, "_redis", no_redis)

    ok, _v, reason = _run(
        __import__("sql_agent.api.routes", fromlist=["x"]).check_authorization_fresh(probe))
    assert ok is False and reason == "ACCOUNT_DEACTIVATED"

    status, _ = _http("POST", "/api/auth/login",
                      {"username": PROBE, "password": PROBE_PW}, csrf=False)
    assert status != 200, "a blocked account logged in while Redis was down"


def test_17_admin_violation_is_audited_at_high_severity(caplog):
    from sql_agent.security_policy import apply_security_policy

    class _Admin:
        id = -1
        username = "qa_fake_admin"
        role = "admin"

    with caplog.at_level("CRITICAL"):
        _run(apply_security_policy(user=_Admin(), decision=_decision(),
                                   transport="rest", query="delete all"))
    text = " ".join(r.getMessage() for r in caplog.records)
    assert "severity=HIGH" in text, "an exempted admin violation was not audited"
    assert "exempt=administrator" in text


def test_18_the_system_principal_is_never_auto_deactivated():
    from sql_agent.security_policy import OUTCOME_DENIED, apply_security_policy

    class _System:
        id = -2
        username = "system"
        role = "system"

    outcome = _run(apply_security_policy(
        user=_System(), decision=_decision(), transport="rest", query="delete all"))
    assert outcome.outcome == OUTCOME_DENIED
    assert outcome.exempt is True
    assert outcome.metadata.get("exempt_reason") == "system_principal"


def test_19_blocked_reason_is_a_server_side_code_not_model_prose(probe):
    """The LLM must not author security or audit content."""
    _run(_apply(probe, times=3))
    reason = _run(_row(probe)).blocked_reason
    assert reason.startswith("SECURITY_POLICY_THRESHOLD_EXCEEDED"), (
        f"blocked_reason is not a deterministic policy code: {reason!r}")
    for prose in ("SECURITY ALERT", "I ", "Sorry", "assistant"):
        assert prose not in reason, f"model prose leaked into blocked_reason: {reason!r}"


# ---------------------------------------------------------------------------
# 20-22: transport behaviour
# ---------------------------------------------------------------------------

def test_20_the_triggering_transport_is_told_to_close(probe):
    """The socket that earned the block hangs up; a denial does not."""
    from sql_agent.security_policy import OUTCOME_BLOCKED

    below = _run(_apply(probe, transport="websocket", times=2))
    assert below.close_connection is False, "a plain denial closed the connection"

    final = _run(_apply(probe, transport="websocket", times=1))
    assert final.outcome == OUTCOME_BLOCKED
    assert final.close_connection is True


def test_21_other_open_sockets_die_at_their_next_authorization_boundary(probe):
    """Deliberately precise: sockets are NOT revoked instantly. Each re-reads
    `is_active` per message, so an idle tab stays open until it next speaks.
    Claiming immediate global revocation would be false."""
    from sql_agent.api.routes import check_authorization_fresh

    ok, _v, _r = _run(check_authorization_fresh(probe))
    assert ok is True

    _run(_apply(probe, times=3))

    ok, _v, reason = _run(check_authorization_fresh(probe))
    assert ok is False, "a second socket would keep working after the block"
    assert reason == "ACCOUNT_DEACTIVATED"


def test_22_enforcement_failed_never_serializes_as_account_blocked():
    """The single most important serialization rule in this module."""
    from sql_agent.security_policy import (OUTCOME_BLOCKED, OUTCOME_DENIED,
                                           OUTCOME_ENFORCEMENT_FAILED, PolicyOutcome)

    failed = PolicyOutcome(outcome=OUTCOME_ENFORCEMENT_FAILED, message="denied",
                           reference_id="SEC-x", reason_code="X")
    assert failed.error_code == OUTCOME_DENIED
    assert failed.error_code != OUTCOME_BLOCKED
    assert failed.blocked is False

    real = PolicyOutcome(outcome=OUTCOME_BLOCKED, message="blocked",
                         reference_id="SEC-y", reason_code="X", blocked=True)
    assert real.error_code == OUTCOME_BLOCKED

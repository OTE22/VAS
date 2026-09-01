"""Who gets penalised when unsafe SQL appears.

Five places marked the USER for blocking because the MODEL wrote forbidden
SQL — LAYER 0, 2, 3, 4 and the query-modification path. The user asked an
ordinary question, the model produced something the guard refused, and the
violation was recorded against the account. Enough of those and the account
is blocked.

The denial is not in question and does not change: unsafe SQL is always
refused, always audited. What changes is attribution. An account violation is
a statement about a PERSON'S behaviour, and model output is not evidence of
it. Only `detect_malicious_intent`, which reads what the user actually typed,
attributes to the user.

    docker exec face_recognition_api python -m pytest tests/test_violation_attribution.py -v
"""

import ast
import inspect

import pytest

from sql_agent import security_policy


class _User:
    id = 7
    username = "someone"
    role = "user"
    is_superuser = False
    is_admin = False


def _recording(monkeypatch):
    """Capture whether a violation was recorded."""
    recorded = []

    async def _record(user_id):
        recorded.append(user_id)
        return 1

    async def _audit(*a, **k):
        return None

    monkeypatch.setattr(security_policy, "record_violation", _record)
    monkeypatch.setattr(security_policy, "_audit_denial", _audit)
    monkeypatch.setattr(security_policy, "_is_exempt", lambda u: (False, ""))
    return recorded


def _decision():
    return security_policy.SecurityDecision(
        violation=True, action="DENY", reason_code="FORBIDDEN_SQL_ATTEMPT",
        reason="forbidden operation")


# ------------------------------------------------------- the attribution

def test_model_written_sql_is_denied_without_penalising_the_user(monkeypatch):
    """THE fix. The query is refused; the account is not marked."""
    recorded = _recording(monkeypatch)

    outcome = _run(security_policy.apply_security_policy(
        user=_User(), decision=_decision(), transport="rest",
        query="SELECT ...", attributable=False))

    assert outcome.outcome == security_policy.OUTCOME_DENIED, "not denied"
    assert recorded == [], "the user was penalised for model output"


def test_a_user_written_violation_still_counts(monkeypatch):
    """THE negative control.

    If nothing accrues, the blocking mechanism is gone rather than corrected —
    which would be a worse bug than the one being fixed.
    """
    recorded = _recording(monkeypatch)

    outcome = _run(security_policy.apply_security_policy(
        user=_User(), decision=_decision(), transport="rest",
        query="delete everything", attributable=True))

    assert outcome.outcome == security_policy.OUTCOME_DENIED
    assert recorded == [7], "a real user violation stopped counting"


def test_attribution_defaults_to_the_user(monkeypatch):
    """A caller that says nothing must get the old, stricter behaviour."""
    recorded = _recording(monkeypatch)

    _run(security_policy.apply_security_policy(
        user=_User(), decision=_decision(), transport="rest", query="x"))

    assert recorded == [7]


def test_an_unattributable_denial_is_still_audited(monkeypatch):
    """Not penalising is not the same as not recording.

    A model that keeps producing forbidden SQL is a real problem; it is just
    not the user's.
    """
    audited = []

    async def _audit(*a, **k):
        audited.append(a)

    monkeypatch.setattr(security_policy, "_audit_denial", _audit)
    monkeypatch.setattr(security_policy, "_is_exempt", lambda u: (False, ""))

    async def _record(user_id):
        return 1
    monkeypatch.setattr(security_policy, "record_violation", _record)

    _run(security_policy.apply_security_policy(
        user=_User(), decision=_decision(), transport="rest", query="x",
        attributable=False))

    assert audited, "an unattributable denial vanished from the audit trail"


# --------------------------------------------- where attribution is set

def test_only_the_user_input_check_attributes_to_the_user():
    """Structural: read the source rather than trusting the comments.

    `detect_malicious_intent` reads what the user typed. Every other site
    that marks a block is looking at SQL the model produced.
    """
    import sql_agent.tools.agent_tools as module

    source = inspect.getsource(module)
    tree = ast.parse(source)

    user_attributions = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        body = ast.get_source_segment(source, node) or ""
        if '"security_block_actor"] = "user"' in body:
            user_attributions.append(node.name)

    assert user_attributions == ["detect_malicious_intent"], (
        f"model output is being attributed to the user in: "
        f"{user_attributions}")


def test_every_blocking_site_declares_an_actor():
    """A site that sets no actor would silently inherit the user default."""
    import sql_agent.tools.agent_tools as module

    source = inspect.getsource(module)
    blocks = source.count('state["security_block_user"] = True')
    actors = source.count('state["security_block_actor"]')

    assert actors >= blocks, (
        f"{blocks} blocking sites but only {actors} declare an actor")


def _run(coro):
    """Run one coroutine on the suite's shared loop."""
    from conftest import run_on_shared_loop
    return run_on_shared_loop(coro)

"""What one turn remembers between its own actions.

THE ReAct invariant, and the one that was missing:

    action N -> real tool result -> observation N on the turn state
             -> decision N+1 can see observation N

Multi-action turns already existed, but each action re-entered
`run_tool_loop` with a FRESH message list. So an agent could resolve Iron Man
in action 1 and, in action 2, no longer know who he was. Several actions
without shared observations is the same single shot run twice.

The record lives on the turn state because that is what survives graph
re-entry, and it is bounded on purpose: it rides in every subsequent prompt,
so an unbounded record is a context-explosion bug wearing a reasoning-loop
costume.

    docker exec face_recognition_api python -m pytest tests/test_turn_observations.py -v
"""

import json

import pytest

from sql_agent.tools import agent_loop


class _ScriptedLlm:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    def bind(self, **kwargs):
        return self

    def invoke(self, messages):
        self.calls.append(messages)
        return self.replies.pop(0) if self.replies else ""


def _native(name, arguments):
    class _Reply:
        content = ""
        additional_kwargs = {"tool_calls": [{
            "function": {"name": name, "arguments": json.dumps(arguments)}}]}
    return _Reply()


class _FakeDb:
    def __init__(self, rows=()):
        self.rows = list(rows)
        self.executed = []

    def execute_query(self, sql):
        self.executed.append(sql)
        return {"success": True, "rows": self.rows}


def _sent_text(llm):
    """Everything the model was shown, flattened."""
    return "\n".join(str(getattr(m, "content", ""))
                     for call in llm.calls for m in call)


# ------------------------------------------------- carried into the prompt

def test_a_second_action_is_told_what_the_first_one_found():
    """THE invariant. Without this the turn is not ReAct.

    Action 2 re-enters a fresh loop, so the only way it can know Iron Man was
    already resolved is the canonical record travelling on the state.
    """
    llm = _ScriptedLlm([_native("query_database", {"question": "track him"})])
    agent_loop.run_tool_loop(
        llm, user_text="and which camera saw him most?", context_block="",
        db=_FakeDb(), dialogue_state=None, artifact_index=[], max_steps=3,
        prior_observations=[
            {"sequence": 1, "tool": "resolve_person", "status": "resolved",
             "summary": "Iron Man"}])

    shown = _sent_text(llm)
    assert "Already done this turn" in shown
    assert "resolve_person" in shown
    assert "Iron Man" in shown


def test_a_look_up_already_done_is_not_offered_again():
    """Carrying the record forward has to also carry the DUPLICATE guard.

    Otherwise action 2 cheerfully repeats action 1's look-up, which is both
    slower and a way to spend the budget on nothing.
    """
    llm = _ScriptedLlm([
        _native("list_cameras", {}),
        _native("query_database", {"question": "how many cameras"}),
    ])
    db = _FakeDb([{"id": 1, "pipeline_id": "cam-1",
                   "location_name": "Gate", "is_active": True}])
    signature = ("list_cameras", json.dumps({}, sort_keys=True))

    call, trace, _fit = agent_loop.run_tool_loop(
        llm, user_text="how many cameras?", context_block="", db=db,
        dialogue_state=None, artifact_index=[], max_steps=3,
        prior_observations=[{"sequence": 1, "tool": "list_cameras",
                             "status": "ok", "signature": list(signature)}])

    assert len(db.executed) == 0, "a look-up from an earlier action ran again"
    assert any(e.get("repeated") for e in trace)


def test_nothing_is_claimed_when_the_turn_has_done_nothing_yet():
    """The negative control: a first action must not be told about a past."""
    llm = _ScriptedLlm([_native("query_database", {"question": "how many"})])
    agent_loop.run_tool_loop(
        llm, user_text="how many cameras?", context_block="", db=_FakeDb(),
        dialogue_state=None, artifact_index=[], max_steps=3,
        prior_observations=[])

    assert "Already done this turn" not in _sent_text(llm)


# ------------------------------------------------------------- bounded

def test_the_record_carried_forward_is_bounded():
    """It rides in every later prompt, so it cannot grow with the turn."""
    many = [{"sequence": n, "tool": "list_cameras", "status": "ok"}
            for n in range(50)]
    llm = _ScriptedLlm([_native("query_database", {"question": "x"})])
    agent_loop.run_tool_loop(
        llm, user_text="x", context_block="", db=_FakeDb(),
        dialogue_state=None, artifact_index=[], max_steps=3,
        prior_observations=many)

    shown = _sent_text(llm)
    listed = shown.count("list_cameras ->")
    assert listed <= agent_loop._MAX_TURN_OBSERVATIONS, listed


def test_an_observation_carries_no_rows_or_sql():
    """Status, counts and ids — never the result set.

    Re-injecting rows into every subsequent decision is how a bounded loop
    becomes an unbounded prompt.
    """
    llm = _ScriptedLlm([_native("query_database", {"question": "x"})])
    agent_loop.run_tool_loop(
        llm, user_text="x", context_block="", db=_FakeDb(),
        dialogue_state=None, artifact_index=[], max_steps=3,
        prior_observations=[{"sequence": 1, "tool": "query_database",
                             "status": "ok", "summary": "rows=247"}])

    shown = _sent_text(llm)
    assert "SELECT" not in shown.upper().replace("SELECT A TOOL", "")
    assert "rows=247" in shown, "the bounded summary should still be there"


def test_the_bound_is_declared_and_small():
    assert 1 <= agent_loop._MAX_TURN_OBSERVATIONS <= 20

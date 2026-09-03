"""What a turn may spend, and on what.

Every rejection used to cost a look-up. The counter WAS the loop —
`for step in range(max_steps)` — and all six rejection paths advanced it. In
FAST mode, where the budget is 1, that meant a single refusal ended the turn:
the model was appended a message explaining what was wrong and then never
invoked again to read it. A loop that cannot let the model correct itself is
a single shot with extra steps.

Three bounds now, because they bound three different things:

    lookups     work actually performed        max_steps
    rejections  chances to correct a proposal  _MAX_REJECTIONS
    iterations  the hard ceiling               max_steps + _MAX_REJECTIONS

A DUPLICATE call is charged to the look-up budget, not the rejection
allowance: repeating an identical call is not the model correcting itself, it
is the model looping, which is the thing the budget exists to stop.

    docker exec face_recognition_api python -m pytest tests/test_react_budget.py -v
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


def _run(llm, *, max_steps=3, user_text="track joey", db=None):
    return agent_loop.run_tool_loop(
        llm, user_text=user_text, context_block="", db=db or _FakeDb(),
        dialogue_state=None, artifact_index=[], max_steps=max_steps)


# --------------------------------------------- a rejection is not the end

def test_a_rejection_does_not_end_a_single_step_turn():
    """THE bug.

    With max_steps=1 the first proposal was rejected, the loop appended the
    reason, and `range(1)` was exhausted — so the model never saw it. The
    turn fell through to the single-shot planner having learned nothing.
    """
    llm = _ScriptedLlm([
        _native("query_database", {"question": "SELECT * FROM faces"}),  # bad
        # A paraphrase OF the message: one that shares nothing with it is
        # refused on its own account now (paraphrase_ignores_user).
        _native("query_database", {"question": "where joey was detected"}),
    ])
    call, trace, _fit = _run(llm, max_steps=1)

    assert call is not None, "one rejection still ended the turn"
    assert call["arguments"]["question"] == "where joey was detected"
    assert len(llm.calls) >= 2, "the model was never asked again"


def test_a_rejection_is_returned_as_a_structured_observation():
    """The model must be able to learn from it, so it must be legible.

    A reason_code rather than prose: the loop already tells the model in
    words, and the trace is what the rest of the turn reasons over.
    """
    llm = _ScriptedLlm([
        _native("query_database", {"question": "SELECT * FROM faces"}),
        _native("query_database", {"question": "how many faces"}),
    ])
    _call, trace, _fit = _run(llm)

    observations = [e["observation"] for e in trace if e.get("observation")]
    assert observations, "a rejection vanished without an observation"
    assert observations[0]["status"] == "rejected"
    assert observations[0]["reason_code"] == "INVALID_ARGUMENTS"


def test_a_rejected_proposal_never_disappears_silently():
    """Every rejection path must leave a trace entry carrying its reason."""
    llm = _ScriptedLlm([_native("query_database", {"question": "SELECT 1"})])
    _call, trace, _fit = _run(llm)

    rejected = [e for e in trace if e.get("rejected")]
    assert rejected
    assert all(e.get("observation", {}).get("reason_code") for e in rejected)


# ------------------------------------------------------------- the bounds

def test_the_turn_is_still_bounded_when_nothing_valid_is_proposed():
    """The control on the fix: correction opportunities are finite."""
    llm = _ScriptedLlm(
        [_native("query_database", {"question": "SELECT * FROM faces"})] * 20)
    call, _trace, _fit = _run(llm, max_steps=3)

    assert call is None
    assert len(llm.calls) <= 3 + agent_loop._MAX_REJECTIONS


def test_repeating_one_look_up_cannot_outlast_the_look_up_budget():
    """A duplicate is a loop, not a correction, so it buys no extra room.

    Charging duplicates to the rejection allowance would have handed the
    pathological case MORE budget than before.
    """
    llm = _ScriptedLlm([_native("list_cameras", {}) for _ in range(10)])
    db = _FakeDb([{"id": 1, "pipeline_id": "cam-1",
                   "location_name": "Gate", "is_active": True}])
    call, _trace, _fit = _run(llm, max_steps=3, db=db)

    assert call is None
    assert len(llm.calls) <= 3, "a repeated call bought extra iterations"


def test_a_duplicate_is_refused_rather_than_executed_again():
    llm = _ScriptedLlm([_native("list_cameras", {}) for _ in range(6)])
    db = _FakeDb([{"id": 1, "pipeline_id": "cam-1",
                   "location_name": "Gate", "is_active": True}])
    _call, trace, _fit = _run(llm, max_steps=3, db=db)

    assert any(e.get("repeated") for e in trace)
    assert len(db.executed) == 1, "the same look-up ran more than once"


def test_more_than_three_model_calls_are_possible_when_correcting():
    """The ceiling is a ceiling, not a target.

    Before, three was both. A turn that needs two look-ups and survives a
    rejection could not exist.
    """
    llm = _ScriptedLlm([
        _native("query_database", {"question": "SELECT * FROM faces"}),  # bad
        _native("list_cameras", {}),
        _native("resolve_person", {"name": "joey"}),
        _native("query_database", {"question": "track joey"}),
    ])
    db = _FakeDb([{"id": 1, "pipeline_id": "cam-1",
                   "location_name": "Gate", "is_active": True,
                   "name": "JOEY"}])
    call, _trace, _fit = _run(llm, max_steps=3, db=db)

    assert call is not None and call["name"] == "query_database"
    # The intent-fit check shares this llm, so count LOOP turns only.
    loop_calls = [c for c in llm.calls
                  if not any("You judge ONE thing" in str(getattr(m, "content", ""))
                             for m in c)]
    assert len(loop_calls) == 4, "the rejection still consumed a look-up"


def test_the_hard_ceiling_cannot_be_escaped():
    """Whatever the model does, the loop terminates by arithmetic."""
    llm = _ScriptedLlm([_native("nonexistent_tool", {}) for _ in range(50)])
    call, _trace, _fit = _run(llm, max_steps=3)

    assert call is None
    assert len(llm.calls) <= 3 + agent_loop._MAX_REJECTIONS


def test_the_rejection_allowance_is_bounded_and_small():
    """A knob nobody can see is a knob nobody can reason about."""
    assert 1 <= agent_loop._MAX_REJECTIONS <= 5

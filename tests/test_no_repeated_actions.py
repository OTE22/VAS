"""Doing the same work twice in one turn.

"track joey and give me the report in arabic" ran the query, correctly decided
the turn was not finished, and then ran THE SAME QUERY AGAIN before reporting.
The answer was right; half the time spent producing it was wasted.

Look-ups have been guarded against this from the start - a stable
`(tool, canonical_args)` signature, refused with a reason rather than
executed. Actions were not, because the commit branch returns before the
duplicate check is ever reached.

The rule is the same one, and it is deliberately narrow: identical arguments.
A turn that legitimately needs two queries ("compare Joey and Ali") asks two
DIFFERENT questions, so it is untouched.

    docker exec face_recognition_api python -m pytest tests/test_no_repeated_actions.py -v
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
        text = chr(10).join(str(getattr(m, "content", "")) for m in messages)
        # The intent-fit check shares this llm. Answer it without consuming a
        # scripted tool call, or the script silently shifts by one.
        if "You judge ONE thing" in text:
            class _Yes:
                content = "YES"
                additional_kwargs = {}
            return _Yes()
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

    def execute_query(self, sql):
        return {"success": True, "rows": self.rows}


def _signature(name, arguments):
    return [name, json.dumps(arguments, sort_keys=True)]


def _run(llm, prior=None, max_steps=3):
    return agent_loop.run_tool_loop(
        llm, user_text="track joey and give me the report in arabic",
        context_block="", db=_FakeDb(), dialogue_state=None,
        artifact_index=[], max_steps=max_steps,
        prior_observations=prior or [])


# ------------------------------------------------------------- THE waste

def test_the_same_query_is_not_run_twice_in_one_turn():
    """THE fix. The second action must build on the first, not repeat it."""
    question = {"question": "track joey"}
    llm = _ScriptedLlm([
        _native("query_database", question),        # already done
        _native("generate_document", {"format": "pdf"}),
    ])
    call, trace, _fit = _run(llm, prior=[{
        "sequence": 1, "tool": "query_database", "status": "ok",
        "signature": _signature("query_database", question)}])

    assert call is not None
    assert call["name"] == "generate_document", (
        "the turn ran the same query a second time")
    assert any(e.get("repeated") for e in trace)


def test_the_model_is_told_the_work_is_already_done():
    """A refusal it cannot learn from just becomes the next proposal."""
    question = {"question": "track joey"}
    llm = _ScriptedLlm([
        _native("query_database", question),
        _native("generate_document", {"format": "pdf"}),
    ])
    _call, trace, _fit = _run(llm, prior=[{
        "sequence": 1, "tool": "query_database", "status": "ok",
        "signature": _signature("query_database", question)}])

    repeated = [e for e in trace if e.get("repeated")]
    assert repeated
    assert repeated[0]["observation"]["reason_code"] == "DUPLICATE_TOOL_CALL"


# ---------------------------------------------------- the negative controls

def test_a_different_question_is_not_a_duplicate():
    """THE control.

    "compare Joey and Ali" genuinely needs two queries. Refusing on the tool
    NAME rather than the arguments would break every multi-query turn.
    """
    llm = _ScriptedLlm([_native("query_database", {"question": "track ali"})])
    call, _trace, _fit = _run(llm, prior=[{
        "sequence": 1, "tool": "query_database", "status": "ok",
        "signature": _signature("query_database", {"question": "track joey"})}])

    assert call is not None
    assert call["name"] == "query_database"
    assert call["arguments"]["question"] == "track ali"


def test_a_first_action_is_never_refused():
    """Nothing has been done, so nothing can be a repeat of it."""
    llm = _ScriptedLlm([_native("query_database", {"question": "track joey"})])
    call, _trace, _fit = _run(llm)

    assert call is not None and call["name"] == "query_database"


def test_a_failed_action_may_be_retried():
    """Only SUCCESSFUL work is already done.

    Refusing to retry something that failed would strand the turn on its
    first bad attempt - the opposite of self-correction.
    """
    question = {"question": "track joey"}
    llm = _ScriptedLlm([_native("query_database", question)])
    call, _trace, _fit = _run(llm, prior=[{
        "sequence": 1, "tool": "query_database", "status": "error",
        "signature": _signature("query_database", question)}])

    assert call is not None, "a failed action could not be retried"
    assert call["name"] == "query_database"

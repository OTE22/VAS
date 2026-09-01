"""What the SECOND action of a turn knows about the first.

"track joey and give me the report in arabic" answered:

    I couldn't reach that report to translate it.

The trace says why:

    committed to query_database                     -> 3 rows
    [REASONING] request not yet complete; acting again (1/3)
    [TOOL_LOOP] start ... context={... last_result=n ...}
    [TOOL_LOOP] proposed=query_database

Multi-action worked - the turn correctly decided it was not finished. But the
branch that re-enters the loop cleared `query_result` so the router would not
immediately re-observe, and nothing put the result anywhere the NEXT action
could see it. `last_result=n`: action two was blind, so it re-ran the query it
had just run, and the report it was asked for never existed.

The result reference was only ever built at END of turn. A turn that acts
twice needs it in between.

    docker exec face_recognition_api python -m pytest tests/test_multi_action_carries_result.py -v
"""

import pytest

from sql_agent import reasoning as r


class _Text:
    def __init__(self, content):
        self.content = content
        self.tool_calls = []


class _FakeLLM:
    def __init__(self, answers=None):
        self.answers = list(answers or [])
        self.model = "fake/test-model"

    def bind(self, **kwargs):
        return self

    def invoke(self, messages):
        text = "\n".join(str(getattr(m, "content", "")) for m in messages)
        if "You judge ONE thing" in text:
            return _Text(self.answers.pop(0) if self.answers else "DONE")
        return _Text("")


class _Memory:
    user_id = 1

    def build_result_reference(self, **kwargs):
        # The real one returns a bounded reference, never the rows.
        return {"row_count": len(kwargs.get("rows") or []),
                "question": kwargs.get("question"),
                "columns": ["name"]}


def _tools(monkeypatch, llm=None):
    import sql_agent.tools.agent_tools as module
    monkeypatch.setattr(module, "create_llm", lambda *a, **k: None)
    monkeypatch.setattr(module, "create_sql_llm", lambda *a, **k: None)
    monkeypatch.setattr(module, "DatabaseManager", lambda *a, **k: object())
    monkeypatch.setattr(module, "SQLKnowledgeBase", lambda *a, **k: None)
    tools = module.SQLAgentTools(conversation_memory=_Memory())
    tools.llm = llm or _FakeLLM(["MORE"])
    return tools


def _succeeded(**extra):
    state = {"normalized_input": "track joey and give me the report in arabic",
             "response_language": "en", "working_context": {},
             "planned_action": {"action": "query_database"},
             "generated_sql": "SELECT name FROM faces WHERE name ILIKE '%joey%'",
             "sql_purpose": "track joey",
             "query_result": {"success": True, "rows": [{"n": 1}] * 3,
                              "row_count": 3},
             "replan_count": 0, "execution_retries": 0, "actions_taken": 0,
             "reasoning_mode": r.ReasoningMode.CONTEXTUAL}
    state.update(extra)
    return state


def _multi(monkeypatch):
    from config import settings
    monkeypatch.setattr(settings, "SQL_AGENT_MAX_ACTIONS_PER_TURN", 3,
                        raising=False)


# ------------------------------------------------------------- THE bug

def test_the_next_action_can_see_the_result_of_the_last_one(monkeypatch):
    """Without this the second action re-runs the query and never reports."""
    _multi(monkeypatch)
    tools = _tools(monkeypatch)

    out = tools.observe_and_replan(_succeeded())

    assert out["reasoning_next"] == "plan_action"
    carried = (out.get("working_context") or {}).get("last_result")
    assert carried, "the second action starts with last_result=n, as it did"
    assert carried["row_count"] == 3


def test_the_carried_reference_holds_no_rows(monkeypatch):
    """It rides in the next prompt, so it is a reference, not a result set."""
    _multi(monkeypatch)
    tools = _tools(monkeypatch)

    carried = (tools.observe_and_replan(_succeeded())
               .get("working_context", {}).get("last_result")) or {}

    assert "rows" not in carried
    assert "sql" not in str(carried).lower() or "SELECT" not in str(carried)


def test_the_per_action_fields_are_still_cleared(monkeypatch):
    """The control: a stale result would route the next step back to observing.

    Carrying the reference forward must NOT mean carrying the raw state
    forward - that was the reason for clearing it in the first place.
    """
    _multi(monkeypatch)
    tools = _tools(monkeypatch)

    out = tools.observe_and_replan(_succeeded())

    assert out["query_result"] is None
    assert not out["generated_sql"]
    assert out["planned_action"] is None


def test_a_finished_request_carries_nothing_extra(monkeypatch):
    """A single-action turn is untouched by any of this."""
    _multi(monkeypatch)
    tools = _tools(monkeypatch, _FakeLLM(["DONE"]))

    out = tools.observe_and_replan(_succeeded())

    # Finished, so it must not act again. WHICH terminal node narrates is a
    # separate question - a successful query goes to the one that has its
    # rows, which is the whole point of the routing fix.
    assert out["reasoning_next"] != "plan_action", "it acted again"


def test_a_failed_action_carries_no_result(monkeypatch):
    """Nothing was produced, so there is nothing for the next step to use."""
    _multi(monkeypatch)
    tools = _tools(monkeypatch)

    out = tools.observe_and_replan(_succeeded(
        query_result={"success": False, "error": "boom", "rows": [],
                      "row_count": 0}))

    assert not (out.get("working_context") or {}).get("last_result")


def test_carrying_the_result_never_breaks_the_turn(monkeypatch):
    """Losing the hand-off must not fail the action that earned it."""
    _multi(monkeypatch)

    class _Broken(_Memory):
        def build_result_reference(self, **kwargs):
            raise RuntimeError("memory unavailable")

    import sql_agent.tools.agent_tools as module
    monkeypatch.setattr(module, "create_llm", lambda *a, **k: None)
    monkeypatch.setattr(module, "create_sql_llm", lambda *a, **k: None)
    monkeypatch.setattr(module, "DatabaseManager", lambda *a, **k: object())
    monkeypatch.setattr(module, "SQLKnowledgeBase", lambda *a, **k: None)
    tools = module.SQLAgentTools(conversation_memory=_Broken())
    tools.llm = _FakeLLM(["MORE"])

    out = tools.observe_and_replan(_succeeded())
    assert out["reasoning_next"] == "plan_action"

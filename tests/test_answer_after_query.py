"""After a query SUCCEEDED this turn, the loop's "answer directly" is final:
the reading's second opinion must not re-plan data work that is done.

Opik 01a07857-1b27 (2026-09-06): a correct two-row similarity comparison was
followed by a re-planned, narrowed query and the user got one row.
"""
from types import SimpleNamespace

import pytest

from sql_agent.tools import agent_loop, planner
from sql_agent.tools.agent_tools import SQLAgentTools as T


def _tools(monkeypatch, read_calls):
    tools = T.__new__(T)
    tools.llm = object()
    tools.db = SimpleNamespace(sql_policy=None)
    tools.conversation_memory = None
    monkeypatch.setattr(agent_loop, "run_tool_loop",
                        lambda *a, **k: ({"name": "answer_directly", "arguments": {}}, []))
    monkeypatch.setattr(tools, "_apply_model_tool_call",
                        lambda state, call, trace, candidates: planner.PlannedAction("chat", confidence=0.9, source="tool_loop"))
    monkeypatch.setattr(tools, "_read_the_turn",
                        lambda *a, **k: read_calls.append(1) or SimpleNamespace(wants="data", confidence=0.95))
    monkeypatch.setattr(tools, "_recent_turn_texts", lambda limit=8: [])
    return tools


def _state(observations):
    return {"normalized_input": "Compare the average similarity of IRON MAN with JOEY",
            "working_context": {}, "artifact_index": [], "identity_index": [],
            "observations": observations, "user_id": 1, "conversation_id": "c"}


def test_answer_directly_after_a_successful_query_narrates_the_held_result(monkeypatch):
    read_calls = []
    tools = _tools(monkeypatch, read_calls)
    state = tools.plan_action(_state([{"sequence": 1, "tool": "query_database", "status": "ok",
                                       "summary": "rows=2", "signature": "q1"}]))
    assert read_calls == [], "the second-opinion reading must not run over a finished query"
    assert state.get("repeat_refused") is True
    assert (state.get("planned_action") or {}).get("source") == "repeat_refused"


def test_answer_directly_before_any_query_still_gets_the_second_opinion(monkeypatch):
    read_calls = []
    tools = _tools(monkeypatch, read_calls)
    monkeypatch.setattr(tools, "_plan_from_reading",
                        lambda state, reading, candidates: planner.PlannedAction("query_database", confidence=0.95, source="interpreter"))
    state = tools.plan_action(_state([]))
    assert read_calls == [1], "with no query done, the reading still gets its say"
    assert not state.get("repeat_refused")

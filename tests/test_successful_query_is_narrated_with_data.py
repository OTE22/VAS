"""Who narrates a turn that WORKED.

`track joey` retrieved 3 real detection rows and answered "Joey has 1 query
pattern tracked". Another run answered "there are no tracking records for
him". The SQL was correct both times, the rows were there both times, and the
reasoning layer observed rows=3 both times:

    [STEP_5] Query executed successfully - 3 rows returned
    [REASONING] observation={action=query_database success=True rows=3}
    [STEP_7] Calling LLM for final response generation (chat mode)   <- WRONG

STEP_7 is the CHAT node. It answers from the conversation and is never handed
the result rows, so with real data sitting in state it will happily invent a
number or deny the data exists.

It was reached because raising SQL_AGENT_MAX_ACTIONS_PER_TURN above 1 sends
SUCCESSFUL actions through the observer, and the observer's ANSWER path ended
in a bare `return "chat_response"` - written when only FAILURES arrived there,
where the chat node is the right answer. Success now falls through the same
door.

A turn that produced data must be narrated by the node that receives it.

    docker exec face_recognition_api python -m pytest tests/test_successful_query_is_narrated_with_data.py -v
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


def _tools(monkeypatch, llm=None):
    import sql_agent.tools.agent_tools as module
    monkeypatch.setattr(module, "create_llm", lambda *a, **k: None)
    monkeypatch.setattr(module, "create_sql_llm", lambda *a, **k: None)
    monkeypatch.setattr(module, "DatabaseManager", lambda *a, **k: object())
    monkeypatch.setattr(module, "SQLKnowledgeBase", lambda *a, **k: None)
    tools = module.SQLAgentTools(conversation_memory=None)
    tools.llm = llm or _FakeLLM(["DONE"])
    return tools


def _succeeded(action="query_database", **extra):
    state = {"normalized_input": "track joey", "response_language": "en",
             "working_context": {}, "planned_action": {"action": action},
             "generated_sql": "SELECT f.name FROM faces f WHERE f.name ILIKE '%joey%'",
             "query_result": {"success": True,
                              "rows": [{"name": "JOEY"}] * 3, "row_count": 3},
             "replan_count": 0, "execution_retries": 0, "actions_taken": 0,
             "reasoning_mode": r.ReasoningMode.CONTEXTUAL}
    state.update(extra)
    return state


def _multi(monkeypatch, ceiling=3):
    from config import settings
    monkeypatch.setattr(settings, "SQL_AGENT_MAX_ACTIONS_PER_TURN", ceiling,
                        raising=False)


# --------------------------------------------------------------- THE bug

def test_a_finished_successful_query_is_narrated_with_its_data(monkeypatch):
    """THE fix. Rows exist, so the node that HAS them must do the talking."""
    _multi(monkeypatch)
    tools = _tools(monkeypatch, _FakeLLM(["DONE"]))

    out = tools.observe_and_replan(_succeeded())

    assert out["reasoning_next"] != "chat_response", (
        "a successful query was handed to the node that cannot see its rows")
    assert out["reasoning_next"] == "enrich_co_appearance"


def test_the_data_path_is_reachable_from_the_observer(monkeypatch):
    """A target the graph does not accept silently becomes chat_response."""
    from sql_agent import graph as graph_module

    assert "enrich_co_appearance" in graph_module._OBSERVATION_TARGETS


def test_this_matches_what_a_single_action_turn_already_did(monkeypatch):
    """The ceiling must not change WHO narrates, only how many actions run.

    At ceiling 1 the router sent a successful query straight to
    enrich_co_appearance. Raising it silently rerouted the same turn to the
    chat node - a behaviour change nobody asked for.
    """
    _multi(monkeypatch, ceiling=1)
    tools = _tools(monkeypatch)
    at_one = tools.observe_and_replan(_succeeded())["reasoning_next"]

    _multi(monkeypatch, ceiling=3)
    tools = _tools(monkeypatch, _FakeLLM(["DONE"]))
    at_three = tools.observe_and_replan(_succeeded())["reasoning_next"]

    assert at_one == at_three == "enrich_co_appearance"


# ---------------------------------------------------------- the controls

def test_a_failure_still_goes_to_the_chat_node(monkeypatch):
    """THE control. chat_response is right when there is nothing to show."""
    _multi(monkeypatch)
    tools = _tools(monkeypatch)

    out = tools.observe_and_replan(_succeeded(
        query_result={"success": False, "error": "violates check constraint",
                      "rows": [], "row_count": 0}))

    assert out["reasoning_next"] == "chat_response"


def test_a_chat_turn_still_goes_to_the_chat_node(monkeypatch):
    """A greeting has no data path to take."""
    _multi(monkeypatch)
    tools = _tools(monkeypatch)

    out = tools.observe_and_replan(_succeeded(
        action="chat", query_result=None))

    assert out["reasoning_next"] == "chat_response"


def test_an_unfinished_request_still_acts_again(monkeypatch):
    """Multi-action must survive the fix."""
    _multi(monkeypatch)
    tools = _tools(monkeypatch, _FakeLLM(["MORE"]))

    out = tools.observe_and_replan(_succeeded())

    assert out["reasoning_next"] == "plan_action"

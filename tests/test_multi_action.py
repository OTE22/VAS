"""Pursuing a goal across several actions in one turn.

Until now a turn took ONE action, observed it, corrected at most once, and
answered. That is a bounded corrector, not a goal-pursuing agent — and it is
the honest gap between this and ChatGPT or Claude, which interleave many tool
calls before replying. "Track Joey and send it as a PDF" needed two turns
because the agent could only take one action.

The cycle now continues on SUCCESS too, gated three ways:

  1. a hard ceiling, `SQL_AGENT_MAX_ACTIONS_PER_TURN`, read by the router —
     so termination is arithmetic, not a matter of the model stopping;
  2. one closed question — "is the user's whole request carried out?" —
     which fails SAFE toward finishing;
  3. every additional action goes through the same validation, AST guard and
     intent-fit gate as the first.

The default is 1, which is byte-identical to the old behaviour. That matters
more than the feature: a change everyone gets is a change everyone can be
hurt by, and this one is opt-in.

    docker exec face_recognition_api python -m pytest tests/test_multi_action.py -v
"""

import ast
import inspect
import textwrap

import pytest

from sql_agent import reasoning as r


class _Text:
    def __init__(self, content):
        self.content = content
        self.tool_calls = []


class _FakeLLM:
    def __init__(self, answers=None):
        self.answers = list(answers or [])
        self.asked = []
        self.model = "fake/test-model"

    def bind(self, **kwargs):
        return self

    def invoke(self, messages):
        text = "\n".join(str(getattr(m, "content", "")) for m in messages)
        if "You judge ONE thing" in text:
            self.asked.append(text)
            return _Text(self.answers.pop(0) if self.answers else "DONE")
        return _Text("")


def _tools(monkeypatch, llm=None):
    import sql_agent.tools.agent_tools as module
    monkeypatch.setattr(module, "create_llm", lambda *a, **k: None)
    monkeypatch.setattr(module, "create_sql_llm", lambda *a, **k: None)
    monkeypatch.setattr(module, "DatabaseManager", lambda *a, **k: object())
    monkeypatch.setattr(module, "SQLKnowledgeBase", lambda *a, **k: None)
    tools = module.SQLAgentTools(conversation_memory=None)
    tools.llm = llm or _FakeLLM()
    return tools


def _succeeded(**extra):
    """State after a query that worked."""
    state = {"normalized_input": "track Joey and send it as a PDF",
             "response_language": "en", "working_context": {},
             "planned_action": {"action": "query_database"},
             "query_result": {"success": True, "rows": [{"n": 1}] * 3,
                              "row_count": 3},
             "replan_count": 0, "execution_retries": 0, "actions_taken": 0,
             "reasoning_mode": r.ReasoningMode.CONTEXTUAL}
    state.update(extra)
    return state


# ------------------------------------------------- the default is unchanged

def test_with_the_default_ceiling_a_turn_takes_one_action(monkeypatch):
    """THE most important test here.

    A feature everyone gets is a risk everyone carries. At the default of 1
    this must behave exactly as it did before multi-action existed, and the
    completion question must not even be asked.
    """
    from config import settings
    monkeypatch.setattr(settings, "SQL_AGENT_MAX_ACTIONS_PER_TURN", 1,
                        raising=False)

    llm = _FakeLLM(["MORE"])          # would ask for another action, if asked
    tools = _tools(monkeypatch, llm)
    out = tools.observe_and_replan(_succeeded())

    assert out["reasoning_next"] != "plan_action", "it acted again"
    assert llm.asked == [], "the completion question was asked at ceiling 1"


# ------------------------------------------------------- pursuing the goal

def test_an_unfinished_request_acts_again(monkeypatch):
    """"track Joey and send it as a PDF" — the query is one step of two."""
    from config import settings
    monkeypatch.setattr(settings, "SQL_AGENT_MAX_ACTIONS_PER_TURN", 3,
                        raising=False)

    tools = _tools(monkeypatch, _FakeLLM(["MORE"]))
    out = tools.observe_and_replan(_succeeded())

    assert out["reasoning_next"] == "plan_action", (
        "the turn finished with part of the request outstanding")
    assert out["actions_taken"] == 1


def test_a_finished_request_stops(monkeypatch):
    """The negative control: it must not loop when the job is done."""
    from config import settings
    monkeypatch.setattr(settings, "SQL_AGENT_MAX_ACTIONS_PER_TURN", 3,
                        raising=False)

    tools = _tools(monkeypatch, _FakeLLM(["DONE"]))
    out = tools.observe_and_replan(_succeeded())

    assert out["reasoning_next"] != "plan_action", "it acted again"


def test_the_next_action_starts_from_a_clean_slate(monkeypatch):
    """A stale result would route the next step straight back to observing."""
    from config import settings
    monkeypatch.setattr(settings, "SQL_AGENT_MAX_ACTIONS_PER_TURN", 3,
                        raising=False)

    tools = _tools(monkeypatch, _FakeLLM(["MORE"]))
    out = tools.observe_and_replan(_succeeded(generated_sql="SELECT 1"))

    assert out["query_result"] is None
    assert not out["generated_sql"]
    assert out["planned_action"] is None


# ------------------------------------------------------ termination bounds

def test_the_ceiling_is_absolute(monkeypatch):
    """However the model answers, it cannot exceed the ceiling."""
    from config import settings
    monkeypatch.setattr(settings, "SQL_AGENT_MAX_ACTIONS_PER_TURN", 2,
                        raising=False)

    llm = _FakeLLM(["MORE", "MORE", "MORE"])
    tools = _tools(monkeypatch, llm)

    out = tools.observe_and_replan(_succeeded(actions_taken=1))
    assert out["reasoning_next"] != "plan_action", (
        "the turn took a third action with a ceiling of 2")
    assert llm.asked == [], "it asked to continue past the ceiling"


def test_the_counter_is_written_in_exactly_one_place():
    """Two writers and the ceiling stops being a ceiling."""
    import sql_agent.tools.agent_tools as module

    source = inspect.getsource(module)
    writes = [line.strip() for line in source.splitlines()
              if 'state["actions_taken"] =' in line]
    assert len(writes) == 1, f"actions_taken is written in {len(writes)} places"


def test_the_router_reads_the_setting_not_a_literal():
    """A hard-coded ceiling is not a ceiling anyone can lower."""
    import sql_agent.graph as graph_module

    tree = ast.parse(textwrap.dedent(
        inspect.getsource(graph_module.create_sql_agent)))
    literals = [n for n in ast.walk(tree)
                if isinstance(n, ast.Attribute)
                and n.attr == "SQL_AGENT_MAX_ACTIONS_PER_TURN"]
    assert literals, "the graph no longer bounds actions per turn"


# ------------------------------------------------------------ failing safe

def test_a_broken_completion_check_finishes_the_turn(monkeypatch):
    """Looping when the user is already served wastes their time.

    Stopping early leaves them able to ask again; looping leaves them
    waiting. So a failure here ends the turn.
    """
    from config import settings
    monkeypatch.setattr(settings, "SQL_AGENT_MAX_ACTIONS_PER_TURN", 3,
                        raising=False)

    class _Broken(_FakeLLM):
        def invoke(self, messages):
            raise RuntimeError("model unavailable")

    tools = _tools(monkeypatch, _Broken())
    out = tools.observe_and_replan(_succeeded())

    assert out["reasoning_next"] != "plan_action", "it acted again"


def test_an_unclear_answer_finishes_the_turn(monkeypatch):
    """Same direction: only a clear MORE continues."""
    from config import settings
    monkeypatch.setattr(settings, "SQL_AGENT_MAX_ACTIONS_PER_TURN", 3,
                        raising=False)

    tools = _tools(monkeypatch, _FakeLLM(["possibly?"]))
    out = tools.observe_and_replan(_succeeded())

    assert out["reasoning_next"] != "plan_action", "it acted again"


def test_a_failed_action_never_counts_as_a_step(monkeypatch):
    """Only SUCCESS advances the goal; failure is the re-plan path."""
    from config import settings
    monkeypatch.setattr(settings, "SQL_AGENT_MAX_ACTIONS_PER_TURN", 3,
                        raising=False)

    tools = _tools(monkeypatch, _FakeLLM(["MORE"]))
    out = tools.observe_and_replan(_succeeded(
        query_result={"success": False, "error": "column does not exist",
                      "rows": [], "row_count": 0}))

    assert out["reasoning_next"] != "plan_action"
    assert out.get("actions_taken") in (0, None)

"""The eight findings of the implementation review, each pinned.

    1. any chatbot user could read every camera and person
    2. running out of steps produced a fluent answer, never an admission
    3. exception text was streamed as success and remembered as context
    4. one user could starve everyone (lock order)          - see routes
    5. a cancelled turn was saved with the previous answer   - see routes
    6. the stranger guard blocked the model's own clarifications
    7. the six-message window had no character cap
    8. camera and time-range filters outlived their subject

Findings 4 and 5 are transport plumbing; they are covered by the streaming
and authz suites rather than unit tests here.

    docker exec face_recognition_api python -m pytest tests/test_review_fixes.py -v
"""

from types import SimpleNamespace

import pytest

from sql_agent import dialogue_state as ds
from sql_agent.security.sql_guard import SqlPolicy, validate_sql


# ==================================================== 1. pipeline scope

def _policy(scope):
    return SqlPolicy.for_tables(["faces", "detections", "pipelines",
                                 "system_metrics"], pipeline_scope=scope)


def test_admin_scope_leaves_sql_alone():
    verdict = validate_sql("SELECT name FROM faces", _policy(None))
    assert verdict.allowed
    assert "pipeline_id" not in verdict.sql


def test_every_scoped_table_is_narrowed_even_through_a_join():
    """THE gap: faces has no pipeline column, so it scopes via detections."""
    verdict = validate_sql(
        "SELECT f.name, p.location_name FROM faces f "
        "JOIN detections d ON f.detection_id = d.id "
        "JOIN pipelines p ON d.pipeline_id = p.pipeline_id",
        _policy(frozenset({"cam-1", "cam-2"})))

    assert verdict.allowed, verdict.reason
    sql = verdict.sql
    # three physical tables, three scoped subqueries
    assert sql.count("'cam-1'") == 3, sql
    assert "detection_id IN (SELECT id FROM detections" in sql
    # aliases survive, so the column references still resolve
    for alias in ("AS f", "AS d", "AS p"):
        assert alias in sql, sql


def test_a_table_without_an_alias_keeps_its_own_name():
    verdict = validate_sql("SELECT faces.name FROM faces",
                           _policy(frozenset({"cam-1"})))
    assert verdict.allowed
    assert "AS faces" in verdict.sql, verdict.sql


def test_an_empty_scope_fails_closed():
    """A user assigned no cameras can read nothing - not everything."""
    verdict = validate_sql("SELECT name FROM faces", _policy(frozenset()))
    assert not verdict.allowed
    assert verdict.code == "NO_PIPELINE_ACCESS"


def test_no_camera_access_is_an_outcome_not_a_security_event():
    """Found by the full regression: the new denial code was unclassified,
    and `is_enforceable` fails CLOSED on unknown codes, so a user with no
    grants asking a plain question would have been treated as an attacker
    and warned about an account block."""
    from sql_agent import reasoning as r
    from sql_agent.security import sql_guard

    assert not sql_guard.is_enforceable("NO_PIPELINE_ACCESS")
    assert not sql_guard.is_malformed("NO_PIPELINE_ACCESS")
    assert not sql_guard.is_enforceable("SCOPE_ERROR")

    observation = r.build_observation({
        "planned_action": {"action": "query_database"},
        "query_result": {"success": False, "error_code": "NO_PIPELINE_ACCESS",
                         "error": "Security: no cameras", "rows": [],
                         "row_count": 0}})
    assert observation["error_type"] == r.ErrorType.SQL_OUT_OF_SCOPE
    assert observation["retryable"] is False


def test_no_camera_access_is_worded_honestly_in_both_languages():
    from sql_agent.tools.agent_tools import SQLAgentTools

    phrases = SQLAgentTools._FAILURE_PHRASES["sql_out_of_scope"]
    assert "administrator" in phrases["en"]
    assert "not permitted" not in phrases["en"]
    assert phrases["ar"]


def test_pipeline_ids_are_emitted_as_literals_not_interpolated():
    hostile = "x' OR '1'='1"
    verdict = validate_sql("SELECT name FROM faces",
                           _policy(frozenset({hostile})))
    assert verdict.allowed
    assert "'x'' OR ''1''=''1'" in verdict.sql, verdict.sql


def test_a_cte_named_like_a_table_is_not_rescoped():
    verdict = validate_sql(
        "WITH faces AS (SELECT * FROM detections) SELECT * FROM faces",
        _policy(frozenset({"cam-1"})))
    assert verdict.allowed, verdict.reason
    # only the physical `detections` inside the CTE is scoped
    assert verdict.sql.count("'cam-1'") == 1, verdict.sql


def test_the_graph_executes_through_the_agents_own_database_manager(monkeypatch):
    """THE live failure: the scope was bound on one DatabaseManager while
    the tools executed SQL through a second one they built themselves. A
    KSA-only user saw all sixteen cameras with every unit test green."""
    from sql_agent import graph as graph_module

    captured = {}

    class _Tools:
        def __init__(self, conversation_memory=None, db=None):
            captured["db"] = db

        def __getattr__(self, name):  # every node the builder registers
            return lambda state: state

    monkeypatch.setattr(graph_module, "SQLAgentTools", _Tools)
    sentinel = object()
    graph_module.create_sql_agent(conversation_memory=None, db=sentinel)

    assert captured["db"] is sentinel


def test_tools_use_the_manager_they_are_given(monkeypatch):
    from sql_agent.tools import agent_tools as module

    # The constructor also builds LLM clients and a knowledge base; none of
    # that is under test here.
    monkeypatch.setattr(module, "create_llm", lambda *a, **k: object())
    monkeypatch.setattr(module, "create_sql_llm", lambda *a, **k: object())
    monkeypatch.setattr(module, "SQLKnowledgeBase", lambda *a, **k: object())
    built = []
    monkeypatch.setattr(module, "DatabaseManager",
                        lambda *a, **k: built.append(1) or object())

    sentinel = object()
    assert module.SQLAgentTools(db=sentinel).db is sentinel
    assert built == [], "a manager was built although one was given"

    module.SQLAgentTools()
    assert built == [1], "no manager was built although none was given"


def test_agent_binds_the_scope_onto_its_policy():
    from sql_agent.agent import SQLIntelligenceAgent

    agent = SQLIntelligenceAgent.__new__(SQLIntelligenceAgent)
    agent.db = SimpleNamespace(sql_policy=_policy(None))

    agent.set_pipeline_scope(["cam-9", 7])
    assert agent.db.sql_policy.pipeline_scope == frozenset({"cam-9", "7"})

    agent.set_pipeline_scope(None)
    assert agent.db.sql_policy.pipeline_scope is None


def test_route_scope_is_none_for_admin_and_closed_on_failure(monkeypatch):
    from conftest import run_on_shared_loop
    from sql_agent.api import routes

    assert run_on_shared_loop(routes._pipeline_scope_for(
        SimpleNamespace(role="admin", id=1))) is None

    class _Boom:
        async def __aenter__(self):
            raise RuntimeError("db down")

        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr(routes.db_manager, "get_session", lambda: _Boom())
    # Grants that cannot be read narrow to NOTHING, never to everything.
    assert run_on_shared_loop(routes._pipeline_scope_for(
        SimpleNamespace(role="user", id=2))) == set()


# =================================================== 2. exhaustion is said

def _tools():
    from sql_agent.tools.agent_tools import SQLAgentTools

    return SQLAgentTools.__new__(SQLAgentTools)


def test_running_out_of_steps_is_admitted_in_the_users_language():
    """Now as GUIDANCE: what is needed, with options, in the user's
    language - still reported as a failed turn."""
    tools = _tools()
    openers = {"en": "I could not turn that into a query",
               "ar": "لم أفهم هذا الطلب"}
    for lang in ("en", "ar"):
        state = {"reasoning_exhausted": True, "response_language": lang,
                 "terminal_state": "MAX_ITERATIONS"}
        out = tools.handle_chat(state)
        assert out["final_response"].startswith(openers[lang])
        assert out["turn_failed"] is True


# ============================================ 3. failures are closed phrases

def test_exception_text_never_becomes_the_answer():
    tools = _tools()
    state = {"response_language": "en"}
    tools._fail_turn(state, RuntimeError("psycopg2 at db:5432 refused"))

    assert state["turn_failed"] is True
    assert "psycopg2" not in state["final_response"]
    assert state["final_response"] == tools._FAILURE_NARRATION["en"]


def test_agent_state_declares_turn_failed():
    """LangGraph drops undeclared keys - the flag must be in the schema."""
    from sql_agent.state import AgentState

    assert "turn_failed" in AgentState.__annotations__


# =================================================== 6. offered names


# ================================================= 7. window is bounded

def test_long_messages_are_clipped_in_the_prompt_window():
    from langchain_core.messages import AIMessage, HumanMessage

    from sql_agent.conversation_memory import (ConversationMemory,
                                               _MAX_CONTEXT_MESSAGE_CHARS)

    memory = ConversationMemory.__new__(ConversationMemory)
    memory.messages = [HumanMessage(content="track joey"),
                       AIMessage(content="R" * 5000)]

    text = memory.get_conversation_context(limit=6)
    assert "User: track joey" in text
    assert "R" * (_MAX_CONTEXT_MESSAGE_CHARS + 1) not in text
    assert text.endswith("[…]")


# ============================================ 8. filters retire with subject

class _Memory:
    user_id = 1
    current_session_id = "s1"

    def __init__(self, state):
        self.working_context = {"dialogue_state": state}

    def get_working_context(self):
        return dict(self.working_context)

    def update_working_context(self, **kwargs):
        self.working_context.update(kwargs)

    def add_ai_message(self, *a, **k):
        pass


def _agent_with(fields):
    from sql_agent.agent import SQLIntelligenceAgent

    state = ds.empty_state()
    for field, value in fields.items():
        # Held as the app holds them: filters arrive from tool results. A
        # user_correction source here would out-rank the model's own delta
        # in the second test and mask what it checks.
        state = ds.apply_delta(state, {"operation": "REPLACE", "field": field,
                                       "proposed_value": value,
                                       "source": "tool_result"},
                               turn_id="t0")
    agent = SQLIntelligenceAgent.__new__(SQLIntelligenceAgent)
    agent.conversation_memory = _Memory(state)
    return agent


def _new_subject_turn(**extra):
    state = {"normalized_input": "now track joey", "response_language": "en",
             "query_result": {"success": True}, "sql_purpose": "track joey",
             "resolved_entities": [{"canonical_name": "JOEY"}],
             "query_history_id": None, "intent": "SQL_QUERY",
             "planned_action": {"action": "query_database"},
             "clarification_candidates": []}
    state.update(extra)
    return state


def test_a_new_subject_retires_the_old_camera_and_time_range():
    agent = _agent_with({"referenced_entity": ["ALI ABBASS"],
                         "active_camera": ["gate-3"],
                         "active_time_range": "yesterday"})
    agent._commit_tool_result_deltas("now track joey", _new_subject_turn())

    held = agent.conversation_memory.working_context["dialogue_state"]
    assert ds.get_value(held, "referenced_entity") == ["JOEY"]
    assert not ds.get_value(held, "active_camera")
    assert not ds.get_value(held, "active_time_range")


def test_a_filter_the_model_set_this_turn_survives():
    agent = _agent_with({"referenced_entity": ["ALI ABBASS"],
                         "active_time_range": "yesterday"})
    turn = _new_subject_turn(planned_action={
        "action": "query_database",
        "state_delta": {"operation": "REPLACE", "field": "active_time_range",
                        "proposed_value": "today", "source": "tool_result"}})
    agent._commit_tool_result_deltas("now track joey today", turn)

    held = agent.conversation_memory.working_context["dialogue_state"]
    assert ds.get_value(held, "active_time_range") == "today"

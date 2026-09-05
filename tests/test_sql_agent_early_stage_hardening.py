"""Regression coverage for the SQL agent's pre-execution workflow."""

import inspect
from pathlib import Path

import pytest
from pydantic import ValidationError


REPO = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("text", [
    "show the latest software update",
    "show the drop in detections this week",
    "summarize the commit history",
    "which administrator can grant access?",
])
def test_analytical_words_are_not_attributed_as_write_intent(text):
    from sql_agent.tools.agent_tools import _detect_explicit_write_intent

    assert _detect_explicit_write_intent(text) == []


@pytest.mark.parametrize("text,operation", [
    ("DELETE FROM detections", "DELETE operation"),
    ("delete all", "DELETE operation"),
    ("update users set is_active = false", "UPDATE operation"),
    ("drop table detections", "DROP operation"),
    ("احذف كل السجلات", "DELETE operation"),
])
def test_explicit_write_requests_are_still_detected_across_supported_languages(
        text, operation):
    from sql_agent.tools.agent_tools import _detect_explicit_write_intent

    assert operation in _detect_explicit_write_intent(text)


def test_all_transports_share_a_bounded_query_contract():
    from sql_agent.api import routes

    assert routes._bounded_query("  hello  ") == ("hello", None)
    assert routes._bounded_query(42)[1]
    assert routes._bounded_query("x" * (routes.SQL_AGENT_MAX_QUERY_CHARS + 1))[1]

    with pytest.raises(ValidationError):
        routes.SQLAgentQueryRequest(
            query="x" * (routes.SQL_AGENT_MAX_QUERY_CHARS + 1))


def test_validated_sse_request_uses_model_fields_without_dict_get():
    from sql_agent.api import routes

    request = routes.SQLAgentQueryRequest(
        query="how many cameras?", conversation_id="conversation-1")

    assert routes._request_value(request, "conversation_id") == "conversation-1"
    source = inspect.getsource(routes.sql_agent_query_stream)
    assert 'request.get("conversation_id")' not in source


def test_sse_error_generator_captures_exception_text_before_yielding():
    from sql_agent.api import routes

    source = inspect.getsource(routes.sql_agent_query_stream)
    assert "error_message = str(e)" in source
    assert "'message': str(e)" not in source


def test_nginx_caps_sql_agent_request_bodies_at_the_edge():
    for name in ("nginx.conf", "nginx.prod.conf"):
        text = (REPO / name).read_text(encoding="utf-8")
        assert "location = /api/sql-agent/query" in text
        stream = text.index("location ~ ^/api/sql-agent/query/stream$")
        assert "client_max_body_size 32k" in text[stream:stream + 500]


def test_the_reading_is_the_only_way_a_turn_is_planned():
    """The single-shot planner fallback is gone: plan_action reads the turn
    through the interpreter and never consults the SQL model to plan."""
    from sql_agent.tools.agent_tools import SQLAgentTools

    source = inspect.getsource(SQLAgentTools.plan_action)
    assert "_read_the_turn(" in source and "_plan_from_reading(" in source
    assert "self.sql_llm" not in source
    assert "run_tool_loop" not in source
    assert "deterministic_request_plan" not in source


def test_cancelled_rest_workers_keep_isolation_until_they_exit():
    from sql_agent.api import routes
    from sql_agent.agent import SQLIntelligenceAgent

    route_source = inspect.getsource(routes.sql_agent_query)
    query_source = inspect.getsource(SQLIntelligenceAgent.query)
    assert "asyncio.shield(worker_task)" in route_source
    assert "_drain_cancelled_turn" in route_source
    assert "cancel_event=cancel_event" in query_source


def test_rest_request_registry_has_a_catch_all_terminal_transition():
    from sql_agent.api import routes

    source = inspect.getsource(routes.sql_agent_query)
    assert 'entry.get("status") == "running"' in source
    assert '_finish_request(rest_request_id, "failed")' in source


def test_rest_artifact_response_state_exists_without_authentication():
    from sql_agent.api import routes

    source = inspect.getsource(routes.sql_agent_query)
    initialization = source.index("artifact_block = None")
    auth_branch = source.index(
        "if AUTH_AVAILABLE and current_user and not security_violation_detected")
    assert initialization < auth_branch


def test_sensitive_turn_contents_are_not_written_to_operational_logs():
    agent_tools = (REPO / "sql_agent" / "tools" / "agent_tools.py").read_text(
        encoding="utf-8")
    agent = (REPO / "sql_agent" / "agent.py").read_text(encoding="utf-8")
    database = (REPO / "sql_agent" / "database.py").read_text(encoding="utf-8")
    history = (REPO / "sql_agent" / "services" /
               "user_query_history_service.py").read_text(encoding="utf-8")

    forbidden = (
        "logger.info(state[\"final_response\"])",
        "LLM RAW RESPONSE:\\n{result}",
        "Generated SQL:\\n{state['generated_sql']}",
        "Input: {state['normalized_input']}",
        "Language check - Input:",
        "Sample data:",
        "ex['question']",
        "Query preview:",
        "Executing query ({len(sql)} chars):",
        "User input: {initial_state.get('original_input'",
    )
    combined = "\n".join((agent_tools, agent, database, history))
    for marker in forbidden:
        assert marker not in combined

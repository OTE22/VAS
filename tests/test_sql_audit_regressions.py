"""Regressions measured in Opik 01a085ae-31f2 and 01a085b0-b5d2."""
from types import SimpleNamespace

from sql_agent import reasoning
from sql_agent.diagnostics import redact_diagnostic
from sql_agent.mcp.tools import MCPToolset, ToolContext
from sql_agent.security.sql_guard import SqlPolicy
from sql_agent.tools.agent_tools import SQLAgentTools as Tools


def test_global_empty_result_does_not_resolve_a_remembered_person():
    state = {"intent": "SQL", "planned_action": {"action": "query_database"},
             "generated_sql": "SELECT timestamp FROM detections WHERE timestamp >= CURRENT_DATE",
             "sql_validation_status": "VALID",
             "working_context": {"dialogue_state": {"fields": {
                 "referenced_entity": {"value": ["ali abbass"]}}}},
             "query_result": {"success": True, "rows": [], "row_count": 0}}
    observation = reasoning.build_observation(state)
    assert observation["success"] is True
    assert observation["unresolved_entity"] is None
    state["generated_sql"] = "SELECT name FROM faces WHERE name = 'JOEY'"
    observation = reasoning.build_observation(state)
    assert observation["unresolved_entity"] == "JOEY"


def test_cte_alias_qualified_output_is_explained():
    sql = "WITH events AS (SELECT timestamp FROM detections) SELECT MIN(e.timestamp) AS earliest FROM events e ORDER BY e.earliest"
    facts = Tools._sql_scope_facts(sql)
    assert "'e.earliest' is not exposed by CTE events" in facts
    assert "without the source qualifier" in facts
    assert "'e.timestamp' is not exposed" not in facts


def test_rejected_repeat_keeps_original_sql_and_diagnostics():
    sql = "SELECT " + ", ".join(f"{i} AS col_{i}" for i in range(300))
    error = 'column "missing" does not exist\nLINE 1: ' + sql + '\nHINT: Expose the column in the CTE.'
    state = {"generated_sql": sql}
    Tools._attach_correction_hint(state, {"error_type": reasoning.ErrorType.SQL_EXECUTION_ERROR_CORRECTABLE,
                                         "sanitized_detail": error})
    assert state["sql_correction_hint"]["sql"] == sql
    assert state["sql_correction_hint"]["reason"] == error
    state["generated_sql"] = ""
    Tools._attach_correction_hint(state, {"error_type": reasoning.ErrorType.SQL_INVALID,
                                         "sanitized_detail": "No SQL to validate"})
    prompt = Tools._correction_hint(state)
    assert sql in prompt and error in prompt


def test_sql_tool_keeps_multiline_error_and_redacts_credentials():
    error = 'column "missing" does not exist\nLINE 1: ' + 'x' * 1000 + '\nHINT: Qualify the output. password=do-not-expose-this'
    db = SimpleNamespace(sql_policy=SqlPolicy(allowed_tables=frozenset({"detections"})),
                         execute_query=lambda sql: {"success": False, "error": error})
    out = MCPToolset(db=db).call("database.execute_readonly", {"sql": "SELECT missing FROM detections"}, ToolContext())
    assert out["status"] == "error"
    assert 'x' * 1000 in out["detail"]["error"]
    assert "\nHINT: Qualify the output." in out["error"]
    assert "do-not-expose-this" not in str(out)
    assert out["error"] == redact_diagnostic(error)


def test_failed_story_is_not_a_successful_turn():
    tools = Tools.__new__(Tools)
    tools._note_matched_camera = lambda state: None
    state = {"intent": "SQL", "query_result": {"success": False},
             "observation": {"error_type": reasoning.ErrorType.SQL_INVALID}}
    result = tools.generate_story_response(state)
    assert result["turn_failed"] is True
    assert result["final_response"]


def test_log_summary_never_contains_repair_sql_or_subject():
    line = reasoning.reasoning_trace(conversation_id="c", turn_id="t", mode="contextual",
        observation={"error_type": "sql_invalid"},
        decision={"decision": "REPLAN", "reason": "LINE 1: SELECT secret_name FROM faces"},
        next_action="generate_sql")
    assert "secret_name" not in line and "SELECT" not in line


def test_narrator_receives_every_row_it_is_told_to_report():
    from langchain_core.runnables import RunnableLambda
    tools = Tools.__new__(Tools)
    tools._note_matched_camera = lambda state: None
    tools._companion_answer = lambda state, rows: None
    tools._direct_prompt = lambda *args: None
    tools._context_section = lambda state: ""
    tools._language_directive = lambda state: ""
    tools._fidelity_directive = lambda state: ""
    tools._enforce_literals = lambda state, text, callback: text
    captured = []
    tools.llm = RunnableLambda(lambda prompt: captured.append(prompt.to_string()) or "Complete report.")
    rows = [{"event_id": index, "marker": f"evidence_{index:03d}"} for index in range(101)]
    state = {"intent": "SQL", "normalized_input": "List the stored events.",
             "generated_sql": "SELECT id FROM detections", "query_result": {
                 "success": True, "rows": rows, "row_count": len(rows)}}
    tools.generate_story_response(state)
    assert len(captured) == 1
    assert "101 total rows" in captured[0]
    assert "evidence_100" in captured[0], "the last row must not disappear after row 100"


def test_timestamp_span_is_computed_from_returned_evidence():
    rows = [{"seen_at": "2026-08-17 13:17:11.983636"},
            {"seen_at": "2026-08-17 13:30:41.555521"}]
    facts = Tools._row_facts(rows, "SELECT timestamp AS seen_at FROM detections")
    assert "809.572 seconds (13 minutes 29.572 seconds)" in facts
    assert "not continuous presence or dwell time" in facts

"""Defects found by reading the agent's Opik traces (2026-09-06), pinned.

Each test names the trace that showed the defect. They are cheap, offline,
and describe the failure in the terms the trace did — so a regression reads
as "the thing we saw in Opik is back".

    docker exec face_recognition_api python -m pytest tests/test_trace_found_regressions.py -v
"""
import json

import pytest

from sql_agent import reasoning
from sql_agent.reasoning import ErrorType
from sql_agent.tools import contracts
from sql_agent.tools import tool_executors
from sql_agent.tools.sql_tools import prepare_sql_from_llm_response


# ---------------------------------------------------------------------------
# Trace 01a074f0-0cdf-7c64-a6d3-15b7cb4e17b5 — "How many cameras are there?"
# list_cameras -> INVALID_RESULT on every call; planner asked the USER for a
# list of cameras. Cause: pipelines.is_active is INTEGER, contract is bool.
# ---------------------------------------------------------------------------

class _Db:
    def __init__(self, rows):
        self._rows = rows

    def execute_query(self, sql):
        assert "FROM pipelines" in sql
        return {"success": True, "rows": list(self._rows)}


PIPELINES_AS_STORED = [
    {"id": 4756, "pipeline_id": "18354c35-6441", "location_name": "MAD5AL AMEN  (1)", "is_active": 1},
    {"id": 4278, "pipeline_id": "1971528f-d514", "location_name": "WEZARET DEFA3", "is_active": 0},
    {"id": 4687, "pipeline_id": "607a5eb3-c103", "location_name": None, "is_active": None},
]


def test_list_cameras_passes_its_contract_with_the_integer_column_as_stored():
    result = tool_executors._list_cameras(_Db(PIPELINES_AS_STORED))
    validated = contracts.validate_result("list_cameras", result)

    assert "error" not in validated, validated
    assert validated["count"] == 3
    # the contract dumps with exclude_none, so a NULL is an absent key
    assert [c.get("active") for c in validated["cameras"]] == [True, False, None]
    assert [c["camera"] for c in validated["cameras"]] == [
        "18354c35-6441", "1971528f-d514", "607a5eb3-c103"]


def test_list_cameras_still_passes_when_the_driver_returns_booleans():
    rows = [dict(r, is_active=bool(r["is_active"]) if r["is_active"] is not None else None)
            for r in PIPELINES_AS_STORED]
    validated = contracts.validate_result("list_cameras", tool_executors._list_cameras(_Db(rows)))
    assert "error" not in validated
    # the contract dumps with exclude_none, so a NULL is an absent key
    assert [c.get("active") for c in validated["cameras"]] == [True, False, None]


# ---------------------------------------------------------------------------
# Trace 01a074f1-6a3a-7daf-b25f-e1d440a006b0 — repair loop re-submitted the
# REJECTED SQL three times because the model echoed the rejected envelope
# before the corrected one and the parser took the first object.
# ---------------------------------------------------------------------------

REJECTED = "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'"
CORRECTED = "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"


def test_a_correction_that_echoes_the_rejected_query_first_yields_the_correction():
    response = (json.dumps({"sql": REJECTED, "purpose": "This query is rejected due to access "
                                                        "restrictions. An alternative query is provided below."})
                + "\n\nSince access to the information_schema schema is not permitted, we can "
                  "use the pg_tables system catalog.\n\n"
                + json.dumps({"sql": CORRECTED, "purpose": "List all tables in the public schema"}))
    prepared = prepare_sql_from_llm_response.invoke(response)
    assert prepared["success"]
    assert prepared["sql"] == CORRECTED


def test_a_single_compliant_envelope_is_unchanged():
    prepared = prepare_sql_from_llm_response.invoke(
        json.dumps({"sql": CORRECTED, "purpose": "p"}))
    assert prepared["sql"] == CORRECTED and prepared["purpose"] == "p"


def test_a_trailing_object_without_sql_does_not_hide_the_query():
    response = (json.dumps({"sql": CORRECTED, "purpose": "p"})
                + "\n" + json.dumps({"note": "done"}))
    assert prepare_sql_from_llm_response.invoke(response)["sql"] == CORRECTED


def test_nested_braces_inside_the_sql_string_are_not_a_second_object():
    sql = "SELECT '{\"k\": 1}'::jsonb AS j FROM detections"
    prepared = prepare_sql_from_llm_response.invoke(json.dumps({"sql": sql, "purpose": "p"}))
    assert prepared["sql"] == sql


def test_the_no_query_envelope_still_reports_the_reason():
    prepared = prepare_sql_from_llm_response.invoke(
        json.dumps({"sql": "", "purpose": "no such table"}))
    assert not prepared["sql"]
    assert "no such table" in (prepared.get("purpose") or prepared.get("error") or "")


# ---------------------------------------------------------------------------
# Trace 01a074f0-19fd-7f39-8a86-6e32d1a66633 — "How many identities are known
# and how many unknown?" The model's UNION had mismatched arms; Postgres said
# so; the error fell through to PERMANENT and the user got a bare apology
# without a single rewrite attempt.
# ---------------------------------------------------------------------------

def test_a_union_arm_mismatch_is_a_rewritable_mistake():
    error = ("each UNION query must have the same number of columns\n"
             "LINE 1: ... (SELECT * FROM known_identities UNION ALL SELECT * FROM unk...")
    assert reasoning.classify_execution_error(error) == ErrorType.SQL_EXECUTION_ERROR_CORRECTABLE


@pytest.mark.parametrize("error", [
    "permission denied for table users",
    "canceling statement due to statement timeout",
])
def test_the_new_sign_does_not_widen_what_is_correctable(error):
    assert reasoning.classify_execution_error(error) != ErrorType.SQL_EXECUTION_ERROR_CORRECTABLE


# ---------------------------------------------------------------------------
# The hard battery (2026-09-06). Traces 01a0750a-16f2 (longest gap), 01a0750d-cd18
# (median), 01a0750e-8251 (seen exactly once), 01a0750b-bb52 (two periods):
# query-shape mistakes Postgres names precisely, all answered with an apology.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("error", [
    'missing FROM-clause entry for table "d"',
    "aggregate function calls cannot be nested",
    "aggregate functions are not allowed in WHERE",
    "window functions are not allowed in WHERE",
    "subquery must return only one column",
])
def test_query_shape_mistakes_are_rewritable(error):
    assert reasoning.classify_execution_error(error) == ErrorType.SQL_EXECUTION_ERROR_CORRECTABLE


def _failed(error, **extra):
    state = {"planned_action": {"action": "query_database"},
             "sql_validation_status": "VALID",
             "generated_sql": "SELECT 1 FROM faces",
             "query_result": {"success": False, "error": error, "rows": [], "row_count": 0},
             "working_context": {}, "normalized_input": "how many people were seen once"}
    state.update(extra)
    return state


def test_an_aggregate_in_where_is_a_mistake_not_a_refusal():
    """'aggregate functions are not allowed in WHERE' contains 'not allowed'
    and was narrated as 'That operation is not permitted — I can only read
    data'. It is the model misplacing an aggregate."""
    observation = reasoning.build_observation(
        _failed("aggregate functions are not allowed in WHERE\nLINE 1: ... HAVING COUNT(f.id)"))
    assert observation["error_type"] == ErrorType.SQL_EXECUTION_ERROR_CORRECTABLE
    assert observation["retryable"]


@pytest.mark.parametrize("error", [
    "security: write statements are forbidden",
    "cannot execute INSERT in a read-only transaction",
    "operation not allowed on this connection",
])
def test_genuine_refusals_are_still_refusals(error):
    observation = reasoning.build_observation(_failed(error))
    assert observation["error_type"] == ErrorType.SQL_FORBIDDEN


# ---------------------------------------------------------------------------
# Traces 01a07506-76e5 and 01a0750a-86cb: `p.is_active = TRUE` twice, because
# the schema text said the column was boolean and four curated examples
# compared it with TRUE. The column is INTEGER (db_models.Pipeline).
# ---------------------------------------------------------------------------

def test_the_schema_text_tells_the_truth_about_is_active():
    from sql_agent import database as database_module
    source = open(database_module.__file__, encoding="utf-8").read()
    assert '"column_name": "is_active", "data_type": "integer"' in source
    assert '"column_name": "is_active", "data_type": "boolean"' not in source


def test_no_curated_example_compares_is_active_with_a_boolean():
    from sql_agent import knowledge_base as kb_module
    source = open(kb_module.__file__, encoding="utf-8").read().lower()
    assert "is_active = true" not in source and "is_active = false" not in source
    assert "is_active = 1" in source

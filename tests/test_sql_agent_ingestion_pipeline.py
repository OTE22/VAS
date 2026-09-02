"""Contracts for request ingestion and SQL canonicalization.

No network, model, or database connection is used.
"""

import json

import pytest

from sql_agent.input_pipeline import QueryInputError, ingest_query
from sql_agent.tools.sql_tools import prepare_sql_from_llm_response


def test_ingestion_preserves_raw_text_and_builds_one_canonical_query():
    raw = "  Show\r\n  Jos\u00e9\t at   camera 3  "
    envelope = ingest_query(raw, max_chars=100)

    assert envelope.raw_text == raw
    assert envelope.normalized_text == "Show Jos\u00e9 at camera 3"
    assert envelope.security_text == "show jos\u00e9 at camera 3"


def test_ingestion_normalizes_unicode_without_rewriting_meaning():
    decomposed = "Cafe\u0301"
    envelope = ingest_query(decomposed)

    assert envelope.raw_text == decomposed
    assert envelope.normalized_text == "Caf\u00e9"


def test_ingestion_preserves_spacing_inside_a_quoted_filter_value():
    envelope = ingest_query(" find   person  'Ali   Abbass'  today ")

    assert envelope.normalized_text == "find person 'Ali   Abbass' today"


def test_security_form_closes_compatibility_and_zero_width_bypasses():
    envelope = ingest_query("ＤＥＬＥＴＥ\u200b FROM detections")

    assert envelope.normalized_text.startswith("ＤＥＬＥＴＥ")
    assert envelope.security_text == "delete from detections"


@pytest.mark.parametrize("value", ["", "   ", 42, "hello\x00world"])
def test_invalid_requests_never_enter_planning(value):
    with pytest.raises(QueryInputError):
        ingest_query(value)


def test_input_and_output_language_are_separate_concerns():
    explicit = ingest_query("show detections in Arabic")
    arabic = ingest_query("اعرض الاكتشافات")

    assert explicit.input_language == "en"
    assert explicit.response_language == "ar"
    assert arabic.input_language == arabic.response_language == "ar"


def test_sql_extraction_does_not_change_whitespace_inside_literals():
    sql = "SELECT name FROM faces WHERE name = 'Ali   Abbass update'"
    prepared = prepare_sql_from_llm_response.invoke(json.dumps({
        "sql": sql,
        "purpose": "find the exact enrolled name",
    }))

    assert prepared["success"]
    assert prepared["sql"] == sql


def test_sql_repair_envelope_accepts_fixed_sql():
    fixed = "SELECT COUNT(*) FROM detections"
    prepared = prepare_sql_from_llm_response.invoke(json.dumps({
        "fixed_sql": fixed,
        "fixes_applied": ["closed parenthesis"],
        "is_valid": True,
    }))

    assert prepared["success"]
    assert prepared["sql"] == fixed


def test_ast_canonical_sql_is_the_value_kept_for_execution():
    from sql_agent.config import Config
    from sql_agent.database import DatabaseManager
    from sql_agent.tools.agent_tools import SQLAgentTools

    tools = SQLAgentTools.__new__(SQLAgentTools)
    tools.db = DatabaseManager(Config())
    tools.sql_llm = None
    state = {
        "generated_sql": "SELECT name FROM faces WHERE name = 'Ali   Abbass'",
        "schema_description": "faces(name)",
        "normalized_input": "find Ali   Abbass",
    }

    result = tools.validate_and_fix_sql(state)

    assert result["sql_validation_status"] == "VALID"
    assert result["validated_sql"] == result["generated_sql"]
    assert "LIMIT" in result["validated_sql"].upper()
    assert "Ali   Abbass" in result["validated_sql"]


def test_execution_preparation_refuses_changed_sql_after_authorization():
    from sql_agent.tools.agent_tools import SQLAgentTools

    tools = SQLAgentTools.__new__(SQLAgentTools)
    state = {
        "validated_sql": "SELECT name FROM faces LIMIT 10",
        "generated_sql": "SELECT name FROM faces LIMIT 20",
    }

    result = tools.prepare_sql_for_execution(state)

    assert not result["query_result"]["success"]
    assert result["query_result"]["error_code"] == "STALE_SQL_AUTHORIZATION"
    assert result["generated_sql"] == ""

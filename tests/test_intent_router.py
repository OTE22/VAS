"""Intent routing is observable and testable.

The label is derived from the reading and the committed action - the facts
the loop already validated - not from the user's words. No model, no DB.

    docker exec face_recognition_api python -m pytest tests/test_intent_router.py -v
"""
from sql_agent import intent


def test_greetings_and_general_questions_are_chat():
    assert intent.intent_of({"wants": "chat"}, {"action": "chat"}) == intent.CHAT
    assert intent.intent_of({"wants": "recall"}, {"action": "chat"}) == intent.CHAT
    assert intent.intent_of(None, None) == intent.CHAT


def test_row_questions_are_database_queries():
    reading = {"wants": "data", "shape": "report"}
    assert intent.intent_of(reading, {"action": "query_database"}) == intent.DATABASE_QUERY
    assert intent.intent_of({"wants": "data", "shape": "answer"}, {"action": "query_database"}) == intent.DATABASE_QUERY


def test_computed_figures_are_analytics_queries():
    reading = {"wants": "data", "shape": "summary"}
    assert intent.intent_of(reading, {"action": "query_database"}) == intent.ANALYTICS_QUERY
    assert intent.intent_of(reading, {"action": "modify_previous_query"}) == intent.ANALYTICS_QUERY


def test_documents_and_translations_are_report_requests():
    assert intent.intent_of({"wants": "document", "format": "pdf"}, {"action": "generate_document"}) == intent.REPORT_REQUEST
    assert intent.intent_of({"wants": "translation"}, {"action": "translate_artifact"}) == intent.REPORT_REQUEST
    # A data reading with a named format is still a report request.
    assert intent.intent_of({"wants": "data", "format": "pdf"}, {"action": "query_database"}) == intent.REPORT_REQUEST


def test_lookups_of_system_state_are_system_tool_requests():
    assert intent.intent_of({"wants": "chat"}, {"action": "chat"},
                            tools_called=["list_cameras"]) == intent.SYSTEM_TOOL_REQUEST
    assert intent.intent_of({"wants": "chat"}, {"action": "chat"},
                            tools_called=["list_cameras", "query_database"]) == intent.CHAT


def test_the_label_vocabulary_is_closed():
    assert set(intent.INTENTS) == {"CHAT", "DATABASE_QUERY", "ANALYTICS_QUERY",
                                   "REPORT_REQUEST", "SYSTEM_TOOL_REQUEST"}


def test_question_hash_is_stable_and_never_the_words():
    a = intent.question_hash("Where was JOEY  last seen?")
    b = intent.question_hash("where was joey last seen?")
    assert a == b and len(a) == 16 and "joey" not in a


def test_tables_come_from_the_ast_and_skip_ctes():
    sql = ("WITH per_day AS (SELECT DATE(d.timestamp) AS day_, COUNT(*) n FROM detections d GROUP BY 1) "
           "SELECT p.location_name, per_day.n FROM per_day JOIN pipelines p ON TRUE")
    assert intent.tables_in(sql) == ["detections", "pipelines"]
    assert intent.tables_in("not sql") == []


def test_turn_facts_carry_the_audit_fields_and_nothing_sensitive():
    state = {
        "interpretation": {"wants": "data", "shape": "summary"},
        "planned_action": {"action": "query_database"},
        "observations": [{"tool": "resolve_person", "status": "ok"},
                         {"tool": "query_database", "status": "ok"}],
        "generated_sql": "SELECT COUNT(*) FROM detections",
        "validated_sql": "SELECT COUNT(*) FROM detections LIMIT 500",
        "sql_validation_status": "VALID",
        "query_result": {"success": True, "row_count": 1, "rows": [{"count": 165}]},
        "retrieved_examples": [{}, {}],
        "normalized_input": "how many detections",
    }
    facts = intent.turn_facts(state)
    assert facts["intent"] == "ANALYTICS_QUERY"
    assert facts["tools"] == ["resolve_person", "query_database"]
    assert facts["tables"] == ["detections"]
    assert facts["validated_sql"].endswith("LIMIT 500")
    assert facts["authorization"] == "VALID"
    assert facts["row_count"] == 1
    assert facts["retrieved"] == 2
    assert "rows" not in facts and "normalized_input" not in facts

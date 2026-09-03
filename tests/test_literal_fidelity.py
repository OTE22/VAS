"""Names in a report are identifiers, and identifiers are copied.

The Arabic report for camera WEZARET DEFA3 called it "وزارة" and JOEY
"جوي". An operator searching the system with those strings finds nothing.
The general instruction ("copy names exactly") was being ignored, so the
narration is now TOLD which strings must appear, and checked afterwards:
any stored name it dropped or translated is appended as stored, in the
answer's language, without contradicting what was already streamed.

    docker exec face_recognition_api python -m pytest tests/test_literal_fidelity.py -v
"""

from sql_agent.tools.agent_tools import SQLAgentTools as T

ROWS = [
    {"name": "JOEY", "location_name": "WEZARET DEFA3", "detection_count": 3},
    {"name": "Unknown", "location_name": "WEZARET DEFA3", "detection_count": 46},
]


def test_identifiers_are_collected_from_the_rows_once_each():
    assert T._literals_in_rows(ROWS) == ["JOEY", "WEZARET DEFA3"]


def test_placeholders_and_non_identifier_columns_are_ignored():
    rows = [{"name": None, "location_name": "unknown", "detection_count": 5,
             "pipeline_id": "1971528f-d514"}]
    assert T._literals_in_rows(rows) == []


def test_the_users_spelling_does_not_satisfy_the_stored_name():
    report = "Two individuals were detected at camera 'wezaret', including JOEY."
    assert T._missing_literals(report, ["JOEY", "WEZARET DEFA3"]) == ["WEZARET DEFA3"]


def test_case_spacing_and_underscores_are_folded_but_letters_are_not():
    assert T._missing_literals("seen at wezaret_defa3 and joey", ["JOEY", "WEZARET DEFA3"]) == []
    assert T._missing_literals("تم رصد جوي في كاميرا وزارة", ["JOEY", "WEZARET DEFA3"]) == [
        "JOEY", "WEZARET DEFA3"]


def test_the_directive_lists_the_exact_strings():
    directive = T._fidelity_directive({"query_result": {"rows": ROWS}})
    assert "'JOEY'" in directive and "'WEZARET DEFA3'" in directive
    assert "never translate" in directive
    assert "You need not name all of them" in directive
    assert T._fidelity_directive({"query_result": {"rows": []}}) == ""


def test_a_translated_name_is_appended_as_stored_in_the_answers_language():
    sent = []
    state = {"query_result": {"rows": ROWS}, "response_language": "ar"}
    out = T._enforce_literals(state, "تم رصد جوي في كاميرا وزارة.", sent.append)

    assert out.endswith("الأسماء كما هي مسجلة في النظام: JOEY, WEZARET DEFA3")
    assert sent and sent[0]["type"] == "content" and "JOEY" in sent[0]["content"]


def test_a_faithful_report_is_left_alone():
    state = {"query_result": {"rows": ROWS}, "response_language": "en"}
    text = "JOEY was seen 3 times at WEZARET DEFA3."
    assert T._enforce_literals(state, text, None) == text


def test_a_translation_keeps_the_stored_names_or_appends_them():
    """The Arabic translation of the Iron Man report wrote "آيرون مان";
    the footer supplies the identifier as stored."""
    class _Db:
        def execute_query(self, sql):
            return {"success": True, "rows": [
                {"id": "1", "pipeline_id": "p1", "location_name": "WEZARET DEFA3", "is_active": 1}]}

    tools = T.__new__(T)
    tools.db = _Db()
    state = {"identity_index": [{"identity_id": "1", "display_name": "IRON MAN"}]}
    source = "IRON MAN was seen 8 times, last at WEZARET DEFA3."
    assert T._names_in_text(tools, source, state) == ["IRON MAN", "WEZARET DEFA3"]

    translated = "تم رصد آيرون مان 8 مرات، آخرها في WEZARET DEFA3."
    missing = T._missing_literals(translated, T._names_in_text(tools, source, state))
    assert missing == ["IRON MAN"]
    assert T._names_as_stored_footer(missing, "ar").endswith("IRON MAN")


def test_the_artifact_route_applies_the_same_footer_through_the_agent():
    from sql_agent.agent import SQLIntelligenceAgent

    class _Db:
        def execute_query(self, sql):
            return {"success": True, "rows": []}

    agent = SQLIntelligenceAgent.__new__(SQLIntelligenceAgent)
    agent.db = _Db()
    agent._identity_index = [{"identity_id": "1", "display_name": "IRON MAN"}]
    out = agent.keep_stored_names("IRON MAN was seen 8 times.", "تم رصد آيرون مان 8 مرات.", "ar")
    assert out.endswith("الأسماء كما هي مسجلة في النظام: IRON MAN")
    faithful = "تم رصد IRON MAN 8 مرات."
    assert agent.keep_stored_names("IRON MAN was seen 8 times.", faithful, "ar") == faithful
    assert agent.keep_stored_names("x", "", "ar") == ""


def test_large_result_sets_are_neither_instructed_nor_enforced():
    """Told that twelve names "must appear", the model wrote "pytest-cam was
    not found in the results" into a report about the three busiest
    cameras. A long list is a summary's raw material, not a checklist."""
    rows = [{"name": f"person_{i:02d}"} for i in range(12)]
    state = {"query_result": {"rows": rows}, "response_language": "en"}
    text = "Twelve people were seen; the busiest was person_00."
    assert T._enforce_literals(state, text, None) == text
    assert T._fidelity_directive(state) == ""

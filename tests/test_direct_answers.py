"""A one-fact question gets a sentence, and a slice is not a history.

Live:

    user: when joey last seen and where
    bot:  SECURITY INTELLIGENCE REPORT - JOEY ... 1. Executive Summary ...
          5. Statistical Summary * Total detections: 1 ...

The facts were right (WEZARET DEFA3, 2026-08-23 11:11:54); the form was a
six-section report for a one-line question, and "Total detections: 1" was
false: JOEY has three, the query fetched the latest by design, and the
report treated one row as the whole history.

    docker exec face_recognition_api python -m pytest tests/test_direct_answers.py -v
"""

import pytest

from sql_agent.tools.agent_tools import SQLAgentTools as T

ROW = {"name": "JOEY", "camera_name": "WEZARET DEFA3",
       "timestamp": "2026-08-23 11:11:54", "similarity": 1.0}


@pytest.mark.parametrize("text", [
    "when joey last seen and where", "Where was Joey seen last?",
    "how many detections yesterday", "Is joey enrolled?",
    "متى شوهد joey آخر مرة وأين", "كم عدد عمليات الرصد أمس", "هل joey مسجل",
])
def test_these_ask_for_one_fact(text):
    assert T._is_point_question(text)


@pytest.mark.parametrize("text", [
    "track joey", "show me all detections from today",
    "give me a report on joey", "تتبع joey", "أظهر لي كل عمليات الرصد",
])
def test_these_want_the_full_picture(text):
    assert not T._is_point_question(text)


def test_shape_is_direct_for_a_point_question_with_few_rows():
    state = {"normalized_input": "when joey last seen and where",
             "generated_sql": "SELECT ... ORDER BY d.timestamp DESC LIMIT 1"}
    assert T._answer_shape(state, 1) == "direct"


def test_shape_is_direct_for_a_top_one_query_however_it_was_phrased():
    state = {"normalized_input": "joey latest sighting please",
             "generated_sql": "SELECT ... ORDER BY d.timestamp DESC LIMIT 1"}
    assert T._answer_shape(state, 1) == "direct"


def test_shape_is_a_report_for_tracking_or_many_rows():
    assert T._answer_shape({"normalized_input": "track joey",
                            "generated_sql": "SELECT ..."}, 3) == "report"
    assert T._answer_shape({"normalized_input": "who was detected today",
                            "generated_sql": "SELECT ..."}, 40) == "report"


def test_a_limited_query_is_labelled_a_slice():
    note = T._limit_note({"generated_sql": "SELECT x FROM y ORDER BY t DESC LIMIT 1"})
    assert "LIMIT 1" in note and "not the whole history" in note
    assert T._limit_note({"generated_sql": "SELECT x FROM y"}) == ""
    assert T._limit_note({"generated_sql": "SELECT x FROM y LIMIT 500"}) == ""


def test_the_direct_prompt_forbids_report_furniture_and_carries_the_rows():
    tools = T.__new__(T)
    state = {"normalized_input": "when joey last seen and where",
             "generated_sql": "SELECT ... ORDER BY d.timestamp DESC LIMIT 1",
             "query_result": {"rows": [ROW]}, "response_language": "en"}
    prompt = tools._direct_prompt(state, [ROW], 1)
    text = "\n".join(str(m.content) for m in prompt.messages)

    assert "No headings" in text
    assert "WEZARET DEFA3" in text and "2026-08-23 11:11:54" in text
    assert "LIMIT 1" in text
    # A tracking request over an unlimited query is a report, however few
    # rows came back.
    assert tools._direct_prompt({**state, "normalized_input": "track joey",
                                 "generated_sql": "SELECT ... ORDER BY d.timestamp"},
                                [ROW] * 3, 3) is None

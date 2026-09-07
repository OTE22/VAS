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


# ---------------------------------------------------------------------------
# Fourth unseen battery (2026-09-06, Opik 01a07841-9405 / 01a07857-1b27):
# a direct read-out hands over EVERY row that qualified, and the identifier
# footer completes a shortened list with the rows' own figures.
# ---------------------------------------------------------------------------
_PER_CAMERA = [
    {"camera_name": "MAD5AL AMEN", "avg_processing_ms": "7252.8"},
    {"camera_name": "MAD5AL AMEN  (1)", "avg_processing_ms": "6915.3"},
    {"camera_name": "ITbano-baraket", "avg_processing_ms": "6214.0"},
    {"camera_name": "WEZARET DEFA3", "avg_processing_ms": "5998.6"},
    {"camera_name": "IT-DIRECTORY", "avg_processing_ms": "4866.2"},
    {"camera_name": "KSA", "avg_processing_ms": "3424.2"},
    {"camera_name": "Smoke Camera", "avg_processing_ms": "1925.1"},
]


def test_a_direct_read_out_of_seven_computed_rows_carries_all_seven():
    tools = T.__new__(T)
    state = {"normalized_input": "average processing time per camera, slowest first",
             "generated_sql": "SELECT ... GROUP BY p.pipeline_id ORDER BY avg_processing_ms DESC",
             "interpretation": {"shape": "summary"},
             "query_result": {"rows": _PER_CAMERA}, "response_language": "en"}
    prompt = tools._direct_prompt(state, _PER_CAMERA, len(_PER_CAMERA))
    assert prompt is not None, "seven computed rows under a summary reading are read out directly"
    text = "\n".join(str(m.content) for m in prompt.messages)
    for row in _PER_CAMERA:
        assert row["avg_processing_ms"] in text, f"{row['camera_name']} was not handed to the narrator"


def test_the_footer_for_dropped_identifiers_carries_their_rows():
    state = {"query_result": {"rows": _PER_CAMERA}}
    lines = T._rows_for_names(state, ["KSA", "Smoke Camera", "Nowhere Cam"])
    assert lines[0] == "KSA: avg_processing_ms 3424.2"
    assert lines[1] == "Smoke Camera: avg_processing_ms 1925.1"
    assert lines[2] == "Nowhere Cam", "a name with no row stays a bare name"
    footer = T._names_as_stored_footer(lines, "en")
    assert "KSA: avg_processing_ms 3424.2; Smoke Camera: avg_processing_ms 1925.1" in footer
    assert T._names_as_stored_footer(["KSA", "IT-DIRECTORY"], "en").endswith("KSA, IT-DIRECTORY")

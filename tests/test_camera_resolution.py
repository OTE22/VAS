"""A camera the user names is a camera, and an unknown one is said to be.

From a real transcript, using the prompt the UI itself suggested:

    user: Who was detected at camera MD5AL_3EIN_7LWE?
    bot:  What person were you referring to?
    user: all of them
    bot:  I searched the database and found no matching records.

Three things went wrong, none of them the model's alone to fix:

- the UI hard-coded a camera name that had not existed for months;
- the model passed "camera MD5AL_3EIN_7LWE" to resolve_person, found nobody,
  and asked about a person the user never mentioned;
- an empty result narrowed to a camera got the generic "no matching
  records", where a person would have been resolved and explained.

    docker exec face_recognition_api python -m pytest tests/test_camera_resolution.py -v
"""

from pathlib import Path

import pytest

from sql_agent import reasoning as r

REPO = Path(__file__).resolve().parents[1]


# ------------------------------------------------- the loop guard


# ------------------------------------------- the observation knows cameras

CAMERA_SQL = ("SELECT f.name FROM faces f JOIN detections d ON d.id = f.detection_id "
              "JOIN pipelines p ON p.pipeline_id = d.pipeline_id "
              "WHERE p.location_name ILIKE '%MD5AL_3EIN_7LWE%'")
PERSON_SQL = "SELECT * FROM faces WHERE name ILIKE '%joey%'"


def test_camera_filters_are_extracted_from_the_sql():
    assert r.filtered_cameras(CAMERA_SQL) == ["MD5AL_3EIN_7LWE"]
    assert r.filtered_cameras(PERSON_SQL) == []
    assert r.filtered_cameras("SELECT COUNT(*) FROM detections") == []


def _empty_turn(sql):
    return {"planned_action": {"action": "query_database"},
            "generated_sql": sql,
            "query_result": {"success": True, "rows": [], "row_count": 0}}


def test_an_empty_result_narrowed_to_a_camera_is_worth_a_second_look():
    observation = r.build_observation(_empty_turn(CAMERA_SQL))
    assert observation["error_type"] == r.ErrorType.EMPTY_RESULT
    assert observation["unresolved_entity"] == "MD5AL_3EIN_7LWE"
    assert observation["unresolved_kind"] == "camera"


def test_a_person_filter_still_wins_when_both_are_present():
    both = ("SELECT * FROM faces f JOIN detections d ON d.id = f.detection_id "
            "WHERE f.name ILIKE '%joey%' AND d.pipeline_id = 'cam-1'")
    observation = r.build_observation(_empty_turn(both))
    assert observation["unresolved_kind"] == "person"
    assert observation["unresolved_entity"] == "joey"


def test_zero_rows_with_no_filter_is_still_just_zero_rows():
    observation = r.build_observation(
        _empty_turn("SELECT COUNT(*) FROM detections"))
    assert observation["error_type"] != r.ErrorType.EMPTY_RESULT


# ------------------------------------------------- the resolution itself

class _Db:
    """Only what list_cameras reads. Mirrors the pipelines table shape."""

    def __init__(self, rows):
        self._rows = rows

    def execute_query(self, sql):
        assert "FROM pipelines" in sql
        return {"success": True, "rows": list(self._rows)}


def _tools(db):
    from sql_agent.tools.agent_tools import SQLAgentTools

    tools = SQLAgentTools.__new__(SQLAgentTools)
    tools.db = db
    return tools


CAMERAS = [
    {"id": "1", "pipeline_id": "18354c35", "location_name": "MAD5AL AMEN (1)",
     "is_active": 1},
    {"id": "2", "pipeline_id": "1971528f", "location_name": "WEZARET DEFA3",
     "is_active": 1},
    {"id": "3", "pipeline_id": "d0cc1871", "location_name": "KSA",
     "is_active": 1},
]


def test_an_unknown_camera_is_named_and_the_real_ones_offered():
    tools = _tools(_Db(CAMERAS))
    state = {"generated_sql": CAMERA_SQL}

    route = tools._resolve_entity_and_route(
        state, {"unresolved_entity": "MD5AL_3EIN_7LWE",
                "unresolved_kind": "camera"})

    assert route == "chat_response"
    assert state["camera_not_found"] == "MD5AL_3EIN_7LWE"
    assert state["known_cameras"] == ["MAD5AL AMEN (1)", "WEZARET DEFA3", "KSA"]

    answer = tools._empty_narration({**state, "response_language": "en"})
    assert "no camera named “MD5AL_3EIN_7LWE”" in answer
    assert "WEZARET DEFA3" in answer
    assert tools._empty_narration({**state, "response_language": "ar"})


def test_a_real_camera_with_nothing_recorded_says_so():
    tools = _tools(_Db(CAMERAS))
    state = {}

    route = tools._resolve_entity_and_route(
        state, {"unresolved_entity": "ksa", "unresolved_kind": "camera"})

    assert route == "chat_response"
    assert state["camera_without_data"] == "KSA"
    assert "exists, but has no detections" in tools._empty_narration(
        {**state, "response_language": "en"})


def test_a_misspelled_camera_is_corrected_and_run_again():
    """`wezaret` is contained in the stored 'WEZARET DEFA3': one near match,
    so the query is worth running again with the stored name."""
    tools = _tools(_Db(CAMERAS))
    state = {"generated_sql": "SELECT 1 FROM pipelines WHERE location_name = 'wezaret'"}

    route = tools._resolve_entity_and_route(
        state, {"unresolved_entity": "wezaret", "unresolved_kind": "camera"})

    assert route == "check_schema"
    assert "WEZARET DEFA3" in state["sql_correction_hint"]["reason"]
    assert state["generated_sql"] == ""


def test_a_corrected_camera_that_still_matches_nothing_is_an_answer():
    """Live: after the re-run with 'WEZARET DEFA3' returned nothing, the
    user was asked "Is that the name as it is enrolled?" - person wording
    about a camera that had just been resolved. It is an answer now, and
    it says what the query looked for."""
    from sql_agent import reasoning as r

    decision = r.decide_next(
        {"success": False, "error_type": r.ErrorType.EMPTY_RESULT,
         "retryable": True, "unresolved_entity": "WEZARET DEFA3",
         "unresolved_kind": "camera", "resolution_attempted": True},
        replan_count=0, max_replans=1, execution_retries=0,
        max_execution_retries=1)
    assert decision["decision"] == r.ANSWER

    tools = _tools(_Db(CAMERAS))
    state = {"camera_corrected_to": "WEZARET DEFA3",
             "sql_purpose": "people detected at camera wezaret today",
             "response_language": "en"}
    answer = tools._empty_narration(state)
    assert "No records matched for camera “WEZARET DEFA3”" in answer
    assert "today" in answer
    assert tools._empty_narration({**state, "response_language": "ar"})


def test_underscores_and_case_do_not_hide_a_match():
    tools = _tools(_Db(CAMERAS))
    state = {}
    tools._resolve_entity_and_route(
        state, {"unresolved_entity": "mad5al_amen_(1)", "unresolved_kind": "camera"})
    assert state["camera_without_data"] == "MAD5AL AMEN (1)"


def test_the_chat_node_answers_camera_outcomes_without_a_model():
    tools = _tools(_Db(CAMERAS))
    state = {"camera_not_found": "MD5AL_3EIN_7LWE", "known_cameras": ["KSA"],
             "response_language": "en", "normalized_input": "x"}
    out = tools.handle_chat(state)
    assert out["final_response"].startswith("There is no camera named")


def test_agent_state_declares_the_camera_keys():
    from sql_agent.state import AgentState

    for key in ("camera_not_found", "camera_without_data", "known_cameras"):
        assert key in AgentState.__annotations__, key


# --------------------------------------------------- the UI suggestion

def test_the_ui_no_longer_hard_codes_a_camera_name():
    html = (REPO / "frontend" / "tracking-people.html").read_text(encoding="utf-8")
    assert "MD5AL_3EIN_7LWE" not in html
    assert 'data-example="camera"' in html
    assert "example-camera-name" in html


def test_the_ui_fills_the_camera_example_from_the_scoped_pipelines_api():
    js = (REPO / "frontend" / "js" / "tracking.js").read_text(encoding="utf-8")
    assert "refreshCameraExample" in js
    assert "'/api/pipelines'" in js
    assert "example-camera-name" in js
    # called at start-up, not merely defined
    assert js.count("refreshCameraExample") >= 2

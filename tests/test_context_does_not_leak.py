"""A self-contained question takes nothing from earlier turns.

Live, in Arabic, right after a camera question:

    user: أظهر لي كل عمليات الرصد اليوم           (show me all detections today)
    bot:  لا توجد سجلات مطابقة للكاميرا «WEZARET DEFA3» ...
          looked for: Show all detection events at WEZARET DEFA3 today

The modification route was already refused for a new question; the model
then wrote a NEW query whose paraphrase carried the previous camera, and the
SQL generator was handed the transcript as well. Both doors are closed by
the same fact: the message does not point back.

And the stored camera name reaches the report: a successful query that
filtered on 'wezaret' now records that it matched 'WEZARET DEFA3', so the
narration is told to use it and held to it.

    docker exec face_recognition_api python -m pytest tests/test_context_does_not_leak.py -v
"""

from sql_agent.tools import agent_loop
from sql_agent.tools.agent_tools import SQLAgentTools as T


def _held(**fields):
    return {"fields": {k: {"value": v, "source": "tool_result"}
                       for k, v in fields.items()}}


# ------------------------------------------------- the paraphrase


class _Reply:
    def __init__(self, name, arguments):
        self.content = ""
        self.tool_calls = ([{"name": name, "args": arguments, "id": "t"}]
                           if name else [])


class _FakeLLM:
    def __init__(self, replies):
        self.replies = list(replies)
        self.model = "fake/test-model"

    def bind(self, **kwargs):
        return self

    def invoke(self, messages):
        return self.replies.pop(0) if self.replies else _Reply(None, {})


# ------------------------------------------------- the stored camera name

class _Db:
    def execute_query(self, sql):
        return {"success": True, "rows": [
            {"id": "1", "pipeline_id": "1971528f", "location_name": "WEZARET DEFA3",
             "is_active": 1},
            {"id": "2", "pipeline_id": "d0cc1871", "location_name": "KSA",
             "is_active": 1}]}


def test_a_successful_query_records_the_camera_it_matched():
    tools = T.__new__(T)
    tools.db = _Db()
    state = {"generated_sql": ("SELECT f.name FROM faces f JOIN detections d ON d.id = "
                               "f.detection_id JOIN pipelines p ON p.pipeline_id = "
                               "d.pipeline_id WHERE LOWER(p.location_name) LIKE '%wezaret%'"),
             "query_result": {"rows": [{"name": "JOEY"}]}}
    tools._note_matched_camera(state)

    assert state["camera_matched"] == "WEZARET DEFA3"
    assert T._turn_literals(state) == ["JOEY", "WEZARET DEFA3"]
    assert "'WEZARET DEFA3'" in T._fidelity_directive(state)


def test_a_report_that_only_says_the_users_spelling_gets_the_stored_name():
    state = {"camera_matched": "WEZARET DEFA3", "response_language": "en",
             "query_result": {"rows": [{"name": "JOEY"}]}}
    out = T._enforce_literals(state, "Two people were seen at camera 'wezaret', including JOEY.", None)
    assert out.endswith("Names as stored in the system: WEZARET DEFA3")


def test_a_tiny_literal_is_not_a_camera_name():
    """'1' or '%' in a camera predicate matched 'MAD5AL AMEN (1)' by
    containment, and a question that never named a camera got 'Names as
    stored in the system: MAD5AL AMEN (1)' appended."""
    tools = T.__new__(T)
    tools.db = _Db()
    state = {"generated_sql": "SELECT f.name FROM faces f JOIN detections d ON d.id = "
                              "f.detection_id JOIN pipelines p ON p.pipeline_id = "
                              "d.pipeline_id WHERE p.location_name LIKE '%1%'",
             "query_result": {"rows": [{"name": "JOEY"}]}}
    tools._note_matched_camera(state)
    assert "camera_matched" not in state


def test_no_camera_filter_records_nothing():
    tools = T.__new__(T)
    tools.db = _Db()
    state = {"generated_sql": "SELECT COUNT(*) FROM detections",
             "query_result": {"rows": [{"count": 5}]}}
    tools._note_matched_camera(state)
    assert "camera_matched" not in state

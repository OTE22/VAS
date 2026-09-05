"""Three more facts Python holds instead of the model.

    user: does joey was alone the last time shwe was seen
    bot:  I'm not aware of any information about a person named Joey or Shwe.

A message naming an ENROLLED person is a data request (the model answered
from memory and read the typo as a second person). "With whom" / "alone?"
is answered from the co-appearance enrichment Python computed - the model
had produced "JOEY was with her" from Joey's own row. And a clarification
about a person the look-up has just resolved ("Can you clarify what you
mean by Joey?") is refused.

    docker exec face_recognition_api python -m pytest tests/test_companions_and_known_names.py -v
"""

import pytest

from sql_agent.tools import agent_loop
from sql_agent.tools.agent_tools import SQLAgentTools as T

INDEX = [{"identity_id": "1", "display_name": "JOEY"},
         {"identity_id": "2", "display_name": "Ali Abbass"}]


# ------------------------------------------------- an enrolled name


class _Reply:
    def __init__(self, name, arguments):
        self.content = ""
        self.tool_calls = ([{"name": name, "args": arguments, "id": "t"}]
                           if name else [])


class _Text:
    def __init__(self, content):
        self.content = content
        self.tool_calls = []


class _FakeLLM:
    def __init__(self, replies, fit_answers=None):
        self.replies = list(replies)
        self.fit_answers = list(fit_answers or [])
        self.fit_prompts = []
        self.model = "fake/test-model"

    def bind(self, **kwargs):
        return self

    def invoke(self, messages):
        text = "\n".join(str(getattr(m, "content", "")) for m in messages)
        if "You judge ONE thing" in text:
            self.fit_prompts.append(text)
            return _Text(self.fit_answers.pop(0) if self.fit_answers else "NO")
        return self.replies.pop(0) if self.replies else _Reply(None, {})


# ------------------------------------------------- resolved, then asked

class _Db:
    def execute_query(self, sql):
        return {"success": True, "rows": [{"name": "JOEY"}]}


# ------------------------------------------------- companions

ROW = {"name": "JOEY", "camera_name": "WEZARET DEFA3",
       "timestamp": "2026-08-23 11:11:54.321125"}


def _state(text, companions, lang="en"):
    return {"normalized_input": text, "response_language": lang,
            "co_appearances": companions,
            "working_context": {"dialogue_state": {"fields": {
                "referenced_entity": {"value": ["JOEY"]}}}}}


@pytest.mark.parametrize("text", [
    "with whom she was", "does joey was alone the last time shwe was seen",
    "who was with joey", "مع من كانت", "هل كانت وحدها",
])
def test_these_ask_about_company(text):
    assert T._is_companion_question(text)


def test_nobody_else_is_said_plainly():
    tools = T.__new__(T)
    answer = tools._companion_answer(_state("with whom she was", []), [ROW])
    assert answer == ("No other identified person was detected with JOEY at "
                      "WEZARET DEFA3 on 2026-08-23 11:11:54 within the "
                      "co-appearance window.")


def test_companions_are_listed_and_the_subject_is_not_her_own_company():
    tools = T.__new__(T)
    companions = [{"camera_name": "WEZARET DEFA3", "person": "JOEY"},
                  {"camera_name": "WEZARET DEFA3", "person": "IRON MAN"}]
    answer = tools._companion_answer(_state("who was with joey", companions), [ROW])
    assert answer.startswith("IRON MAN was detected with JOEY")
    assert tools._companion_answer(_state("مع من كانت", companions, "ar"), [ROW]).startswith(
        "تم رصد IRON MAN مع JOEY")


def test_the_latest_detection_is_the_one_described():
    """Rows arrive in whatever order the SQL chose; 'the last time' means
    the latest, and the Arabic replay had described the earliest."""
    tools = T.__new__(T)
    rows = [{"name": "JOEY", "camera_name": "WEZARET DEFA3",
             "timestamp": "2026-08-20 20:23:26"},
            {"name": "JOEY", "camera_name": "WEZARET DEFA3",
             "timestamp": "2026-08-23 11:11:54"}]
    answer = tools._companion_answer(_state("with whom she was", []), rows)
    assert "2026-08-23 11:11:54" in answer and "2026-08-20" not in answer


def test_an_empty_or_pronoun_filter_asks_instead_of_not_found():
    tools = T.__new__(T)
    tools.db = _Db()
    state = {"identity_index": INDEX, "response_language": "en"}
    route = tools._resolve_entity_and_route(
        state, {"unresolved_entity": "she", "unresolved_kind": "person"})
    assert route == "chat_response"
    assert state.get("entity_not_found") is None
    assert state["reasoning_exhausted"] is True


class _CamDb:
    def execute_query(self, sql):
        if "COUNT(*)" in sql:
            return {"success": True, "rows": [{"n": 49}]}
        return {"success": True, "rows": [
            {"id": "1", "pipeline_id": "1971528f", "location_name": "WEZARET DEFA3",
             "is_active": 1}]}


def test_a_camera_with_detections_is_not_told_it_has_none():
    """"yes" to "Did you mean WEZARET DEFA3?" resumed the query, which
    matched nothing, and the answer was "exists, but has no detections
    recorded" - for a camera seen 49 times."""
    tools = T.__new__(T)
    tools.db = _CamDb()
    state = {"sql_purpose": "people detected at camera WEZARET DEFA3 today",
             "response_language": "en"}
    route = tools._resolve_entity_and_route(
        state, {"unresolved_entity": "WEZARET DEFA3", "unresolved_kind": "camera"})
    assert route == "chat_response"
    assert state["camera_has_data"] == ["WEZARET DEFA3", 49]
    assert state.get("camera_without_data") is None
    answer = tools._empty_narration(state)
    assert "Nothing matched that question for camera “WEZARET DEFA3”" in answer
    assert "49 detection(s) on record" in answer


def test_a_camera_label_in_the_name_column_is_resolved_as_a_camera():
    """"No person named 'WEZARET DEFA3' is enrolled" is not an answer."""
    tools = T.__new__(T)
    tools.db = _CamDb()
    state = {"identity_index": INDEX, "response_language": "en"}
    route = tools._resolve_entity_and_route(
        state, {"unresolved_entity": "WEZARET DEFA3", "unresolved_kind": "person"})
    assert route == "chat_response"
    assert state.get("entity_not_found") is None
    assert state["camera_has_data"][0] == "WEZARET DEFA3"


def test_company_is_reported_at_the_detection_it_was_seen_at():
    """The enrichment covers every detection of the subject; the sentence
    names the latest one. IRON MAN seen with JOEY three days earlier was
    being reported as company at the latest detection, and a companion at
    the latest detection was once reported as nobody."""
    tools = T.__new__(T)
    rows = [{"name": "JOEY", "camera_name": "WEZARET DEFA3", "timestamp": "2026-08-23 11:11:54"}]
    earlier = [{"camera_name": "WEZARET DEFA3", "person": "IRON MAN",
                "subject_seen_at": "2026-08-20 20:23:26"}]
    answer = tools._companion_answer(_state("with whom she was", earlier), rows)
    assert answer.startswith("No other identified person was detected with JOEY at WEZARET DEFA3 on 2026-08-23 11:11:54")
    assert answer.endswith("Earlier: IRON MAN with JOEY at WEZARET DEFA3 on 2026-08-20 20:23.")

    at_last = [{"camera_name": "WEZARET DEFA3", "person": "IRON MAN",
                "subject_seen_at": "2026-08-23 11:11:10"}]
    answer = tools._companion_answer(_state("with whom she was", at_last), rows)
    assert answer == "IRON MAN was detected with JOEY at WEZARET DEFA3 on 2026-08-23 11:11:54."
    arabic = tools._companion_answer(_state("مع من كانت", earlier, "ar"), rows)
    assert arabic.startswith("لم يتم رصد أي شخص معروف آخر مع JOEY") and "IRON MAN مع JOEY" in arabic


def test_the_enrichment_and_the_answer_share_one_subject():
    """The rows of a model-written query can start with someone else."""
    class _Db:
        def __init__(self):
            self.sql = ""

        def execute_query(self, sql):
            self.sql = sql
            return {"success": True, "rows": []}

    tools = T.__new__(T)
    tools.db = _Db()
    state = _state("with whom she was", None)
    state["query_result"] = {"rows": [
        {"name": "IRON MAN", "camera_name": "WEZARET DEFA3", "timestamp": "2026-08-23 11:11:10"},
        {"name": "JOEY", "camera_name": "WEZARET DEFA3", "timestamp": "2026-08-23 11:11:54"}]}
    tools.enrich_co_appearance(state)
    assert "LOWER('JOEY')" in tools.db.sql and "LOWER('IRON MAN')" not in tools.db.sql


def test_other_questions_are_left_to_the_narration():
    tools = T.__new__(T)
    assert tools._companion_answer(_state("when was joey last seen", []), [ROW]) is None
    assert tools._companion_answer({**_state("with whom", []), "co_appearances": None}, [ROW]) is None

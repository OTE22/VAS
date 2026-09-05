"""When the assistant cannot answer, it says what it understood and asks
for what is missing - with real options - instead of answering something
else.

Three dead ends became guidance:

- reasoning exhausted on a request: "I could not complete this request"
  is now a question naming a person, a camera or a time window to supply,
  with the cameras and people that exist;
- a model that insists on answering a data question from memory ends the
  loop and lands on the same guidance, so its prose never reaches the user;
- an empty result with nothing to resolve says WHEN data last exists.

    docker exec face_recognition_api python -m pytest tests/test_guidance.py -v
"""

from sql_agent.tools import agent_loop
from sql_agent.tools.agent_tools import SQLAgentTools as T


class _Db:
    def execute_query(self, sql):
        if "MAX(timestamp)" in sql:
            return {"success": True, "rows": [{"latest": "2026-08-23 11:11:54.321125"}]}
        return {"success": True, "rows": [
            {"id": "1", "pipeline_id": "p1", "location_name": "WEZARET DEFA3", "is_active": 1},
            {"id": "2", "pipeline_id": "p2", "location_name": "KSA", "is_active": 1}]}


def _tools():
    tools = T.__new__(T)
    tools.db = _Db()
    return tools


INDEX = [{"identity_id": "1", "display_name": "JOEY"},
         {"identity_id": "2", "display_name": "IRON MAN"}]


def test_guidance_names_what_is_needed_and_what_exists():
    text = _tools()._guidance({"response_language": "en", "identity_index": INDEX})
    assert "I could not turn that into a query" in text
    assert "track JOEY" in text and "camera WEZARET DEFA3" in text
    assert "Cameras available: WEZARET DEFA3, KSA" in text
    assert "Enrolled people include: JOEY, IRON MAN" in text
    assert text.endswith("Which would you like?")


def test_guidance_repeats_what_was_understood():
    state = {"response_language": "en", "identity_index": INDEX,
             "working_context": {"dialogue_state": {"fields": {
                 "referenced_entity": {"value": ["JOEY"]},
                 "active_time_range": {"value": "yesterday"}}}}}
    text = _tools()._guidance(state)
    assert "What I have so far: JOEY; yesterday." in text


def test_guidance_is_bilingual():
    text = _tools()._guidance({"response_language": "ar", "identity_index": INDEX})
    assert text.startswith("لم أفهم هذا الطلب")
    assert "تتبع JOEY" in text and "WEZARET DEFA3" in text
    assert text.endswith("ماذا تريد بالضبط؟")


def test_exhaustion_answers_with_guidance_not_an_apology():
    tools = _tools()
    state = {"reasoning_exhausted": True, "response_language": "en",
             "identity_index": INDEX, "normalized_input": "check it"}
    out = tools.handle_chat(state)
    assert out["final_response"].startswith("I could not turn that into a query")
    assert out["turn_failed"] is True


def test_an_empty_result_says_when_data_last_exists():
    tools = _tools()
    en = tools._empty_narration({"response_language": "en"})
    assert en.endswith("The most recent detection on record is 2026-08-23 11:11:54.")
    ar = tools._empty_narration({"response_language": "ar"})
    assert "2026-08-23 11:11:54" in ar


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



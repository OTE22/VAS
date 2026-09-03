"""Two more ways a data question became prose, from the same transcript.

    user: Who was detected at camera wezaret?
    bot:  Unfortunately, I don't have any information about the camera...

The loop model looked at the task state and answered directly, so no query
ran and the deterministic camera resolution never had a result to work on.
And on the first question the SQL model DID write the right query, but laid
it out on several lines inside the JSON string; strict JSON forbids raw
newlines there, so the envelope was refused three times and the turn ended
without ever executing.

    docker exec face_recognition_api python -m pytest tests/test_unqueried_answer_guard.py -v
"""

import pytest

from sql_agent.tools import agent_loop
from sql_agent.tools.sql_tools import prepare_sql_from_llm_response


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
            return _Text(self.fit_answers.pop(0) if self.fit_answers else "YES")
        return self.replies.pop(0) if self.replies else _Reply(None, {})


def _run(llm, user_text, **kwargs):
    call, trace, _fit = agent_loop.run_tool_loop(
        llm, user_text=user_text, context_block="", db=None,
        dialogue_state=None, artifact_index=None, **kwargs)
    return call, trace


# ----------------------------------------------- answering without a query

def test_a_data_question_answered_from_memory_is_refused_once():
    """known_request=True is how the caller says the user asked for data."""
    llm = _FakeLLM(replies=[
        _Reply("answer_directly", {"answer": "I have no information."}),
        _Reply("query_database", {"question": "detections at camera wezaret"}),
    ])
    call, trace = _run(llm, "Who was detected at camera wezaret?",
                       known_request=True)

    assert call["name"] == "query_database"
    assert any(e.get("rejected") == "answered data without a query"
               for e in trace)


def test_a_model_that_insists_is_allowed_through():
    """Refused once, not forever: the second proposal wins."""
    llm = _FakeLLM(replies=[
        _Reply("answer_directly", {"answer": "x"}),
        _Reply("answer_directly", {"answer": "x"}),
    ])
    call, _trace = _run(llm, "Who was detected at camera wezaret?",
                        known_request=True)
    assert call["name"] == "answer_directly"


def test_a_greeting_still_goes_straight_to_the_answer():
    """No extra model call, no refusal: is_a_request is never established."""
    llm = _FakeLLM(replies=[_Reply("answer_directly", {"answer": "Hello."})])
    call, trace = _run(llm, "hi")

    assert call["name"] == "answer_directly"
    assert llm.fit_prompts == [], "the fit question was asked for a greeting"
    assert not any(e.get("rejected") for e in trace)


def test_a_turn_judged_not_a_request_is_not_refused():
    """The fit check said NO to an action; answering is then correct."""
    llm = _FakeLLM(replies=[
        _Reply("generate_document", {"format": "pdf"}),
        _Reply("answer_directly", {"answer": "Hello."}),
    ], fit_answers=["NO"])
    call, trace = _run(llm, "hi")
    assert call["name"] == "answer_directly"
    assert not any(e.get("rejected") == "answered data without a query"
                   for e in trace)


# ------------------------------------- asking which camera, when told

def test_a_camera_the_user_named_is_queried_not_asked_about():
    """Live: "Which camera is wezaret?" with the camera list in hand. The
    query path resolves a misspelling itself; asking first quits early."""
    llm = _FakeLLM(replies=[
        _Reply("list_cameras", {}),
        _Reply("ask_clarifying_question", {"question": "Which camera is wezaret?"}),
        _Reply("query_database", {"question": "who was detected at camera wezaret"}),
    ])

    class _Db:
        def execute_query(self, sql):
            return {"success": True, "rows": [
                {"id": "1", "pipeline_id": "p1", "location_name": "WEZARET DEFA3",
                 "is_active": 1}]}

    call, trace, _fit = agent_loop.run_tool_loop(
        llm, user_text="Who was detected at camera wezaret?", context_block="",
        db=_Db(), dialogue_state=None, artifact_index=None)

    assert call["name"] == "query_database"
    assert any(e.get("rejected") == "asked about a named camera" for e in trace)


def test_after_a_query_ran_a_camera_clarification_is_allowed():
    """Once a query has run and come back empty, asking may be right."""
    # A look-up first: the older guard refuses any clarification proposed
    # before anything has been looked up in THIS pass, and that stays.
    llm = _FakeLLM(replies=[
        _Reply("list_cameras", {}),
        _Reply("ask_clarifying_question", {"question": "Which camera did you mean?"}),
    ])

    class _Db:
        def execute_query(self, sql):
            return {"success": True, "rows": []}

    call, _trace, _fit = agent_loop.run_tool_loop(
        llm, user_text="Who was detected at camera wezaret?", context_block="",
        db=_Db(), dialogue_state=None, artifact_index=None,
        prior_observations=[{"tool": "query_database", "status": "ok",
                             "sequence": 1}])
    assert call["name"] == "ask_clarifying_question"


def test_the_camera_token_is_read_from_the_users_words():
    from sql_agent.tools.agent_loop import camera_named_by_user

    assert camera_named_by_user("Who was detected at camera wezaret?") == "wezaret"
    assert camera_named_by_user("detections at cam KSA, yesterday") == "KSA"
    assert camera_named_by_user("track joey") is None


# ----------------------------------------------- the SQL envelope

MULTILINE = '''{"sql": "SELECT f.name, p.location_name
FROM faces f
JOIN detections d ON f.detection_id = d.id
JOIN pipelines p ON d.pipeline_id = p.pipeline_id
WHERE p.pipeline_id = 'MD5AL_3EIN_7LWE'", "purpose": "who was seen there"}'''


def test_sql_laid_out_on_several_lines_inside_the_json_is_accepted():
    prepared = prepare_sql_from_llm_response.invoke(MULTILINE)
    assert prepared["success"], prepared
    assert prepared["sql"].startswith("SELECT f.name")
    assert "MD5AL_3EIN_7LWE" in prepared["sql"]


ESCAPED_QUOTES = ('{"sql": "SELECT f.name FROM faces f JOIN detections d ON '
                  "f.detection_id = d.id JOIN pipelines p ON d.pipeline_id = "
                  "p.pipeline_id WHERE LOWER(p.pipeline_id) LIKE "
                  "LOWER(\\'%MD5AL_3EIN_7LWE%\\') AND f.name IS NOT NULL\", "
                  '"purpose": "who was seen there"}')


def test_backslash_escaped_single_quotes_are_not_a_refusal():
    """THE live failure, verbatim shape: the model wrote LOWER(\\'%x%\\'),
    which strict JSON rejects, and a correct query never ran."""
    assert "\\'" in ESCAPED_QUOTES
    prepared = prepare_sql_from_llm_response.invoke(ESCAPED_QUOTES)
    assert prepared["success"], prepared
    assert "LOWER('%MD5AL_3EIN_7LWE%')" in prepared["sql"]


def test_a_fenced_envelope_is_still_accepted():
    fenced = "```json\n" + MULTILINE + "\n```"
    assert prepare_sql_from_llm_response.invoke(fenced)["success"]


def test_prose_without_sql_is_still_refused():
    prepared = prepare_sql_from_llm_response.invoke(
        "I cannot answer that without more details about the camera.")
    assert not prepared["success"]

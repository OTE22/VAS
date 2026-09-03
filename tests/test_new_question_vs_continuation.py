"""A message that states its own question starts a new task.

Live, right after "Who was detected at camera wezaret?":

    user: Show me all detections from today
    bot:  No records matched for camera "WEZARET DEFA3". The query looked
          for: Filter the results to only include detections from today

The model re-ran the previous query with a change, so the camera came along
and the answer was about a camera the user never mentioned. Whether a message
points back at the previous task is a fact about its words - anaphora, a
connective, or a fragment - in either language, and Python reads it.

    docker exec face_recognition_api python -m pytest tests/test_new_question_vs_continuation.py -v
"""

import pytest

from sql_agent.tools import agent_loop
from sql_agent.tools.agent_loop import (camera_named_by_user, is_a_continuation,
                                        names_a_camera)


# ------------------------------------------------------------ the words

@pytest.mark.parametrize("text", [
    "Show me all detections from today",
    "Who was detected at camera wezaret?",
    "How many cameras are registered?",
    "track joey across all cameras yesterday",
    "أظهر لي كل عمليات الرصد اليوم",
    "من تم رصده في كاميرا wezaret؟",
])
def test_a_self_contained_message_is_a_new_question(text):
    assert not is_a_continuation(text)


@pytest.mark.parametrize("text", [
    "same but only today",
    "and yesterday?",
    "make that a PDF",
    "only camera 3",
    "the same camera, last week",
    "them too",
    "yes",
    "نفس الشيء لكن اليوم",
    "وأمس؟",
    "فقط الكاميرا الثالثة",
    "أيضاً الأسبوع الماضي",
])
def test_a_message_that_points_back_is_a_continuation(text):
    assert is_a_continuation(text)


def test_arabic_camera_words_are_recognised():
    assert names_a_camera("wezaret", "من تم رصده في كاميرا wezaret؟")
    assert names_a_camera("KSA", "عمليات الرصد بالكاميرا KSA أمس")
    assert camera_named_by_user("من تم رصده في كاميرا wezaret؟") == "wezaret"
    assert camera_named_by_user("عمليات الرصد في الكاميرا KSA") == "KSA"


# ------------------------------------------------------------ the loop

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


def _run(llm, user_text):
    call, trace, _fit = agent_loop.run_tool_loop(
        llm, user_text=user_text, context_block="", db=None,
        dialogue_state=None, artifact_index=None, known_request=True,
        has_result=True)
    return call, trace


def test_a_new_question_is_not_a_modification_of_the_last_one():
    llm = _FakeLLM([
        _Reply("modify_active_query", {"change": "only today"}),
        _Reply("query_database", {"question": "all detections from today"}),
    ])
    call, trace = _run(llm, "Show me all detections from today")

    assert call["name"] == "query_database"
    assert any(e.get("rejected") == "not a continuation" for e in trace)


def test_a_continuation_may_still_modify_the_last_query():
    llm = _FakeLLM([_Reply("modify_active_query", {"change": "only today"})])
    call, trace = _run(llm, "same but only today")

    assert call["name"] == "modify_active_query"
    assert not any(e.get("rejected") == "not a continuation" for e in trace)


def test_an_arabic_continuation_is_read_the_same_way():
    llm = _FakeLLM([_Reply("modify_active_query", {"change": "اليوم فقط"})])
    call, _trace = _run(llm, "نفس الشيء لكن اليوم")
    assert call["name"] == "modify_active_query"


def test_the_refusal_happens_once():
    """A model that insists after being told is allowed through."""
    llm = _FakeLLM([
        _Reply("modify_active_query", {"change": "only today"}),
        _Reply("modify_active_query", {"change": "only today"}),
    ])
    call, _trace = _run(llm, "Show me all detections from today")
    assert call["name"] == "modify_active_query"


# ------------------------------------------------------------ the wording

def test_an_empty_modified_query_is_described_as_the_whole_question():
    from sql_agent.tools.agent_tools import SQLAgentTools

    purpose = SQLAgentTools._modified_purpose(
        "people detected at camera wezaret", "only today")
    assert purpose == "people detected at camera wezaret, changed: only today"
    assert SQLAgentTools._modified_purpose(None, "only today").startswith(
        "the previous question")

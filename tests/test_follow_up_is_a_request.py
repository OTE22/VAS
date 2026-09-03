"""A follow-up to a data task is a request, whatever it looks like alone.

Live:

    user: when joey last seen and where
    bot:  Joey was last seen on WEZARET DEFA3 at 2026-08-23 11:11:54.
    user: with whom she was
    bot:  I'm ready to assist you. What's on your mind

The loop model proposed the right query; the intent-fit gate, judging the
four words on their own, said "not a request" and the turn fell to a
greeting. The message points back (she, whom) and the task it points back
at asked for data. That is a fact, and Python holds it now.

    docker exec face_recognition_api python -m pytest tests/test_follow_up_is_a_request.py -v
"""

import pytest

from sql_agent.tools import agent_loop
from sql_agent.tools.agent_loop import is_a_continuation


@pytest.mark.parametrize("text", [
    "with whom she was", "who was with him", "where did they go next",
    "and who else was there", "her last camera", "مع من كانت",
    "أين ذهب بعدها", "ومن كان معه",
])
def test_pronouns_and_relatives_point_back(text):
    assert is_a_continuation(text)


@pytest.mark.parametrize("text", [
    "hello there", "show me all detections from today", "item count per camera",
    "who was detected at camera KSA", "track joey",
])
def test_whole_words_only(text):
    """'he' must not fire on 'hello', nor 'it' on 'item'."""
    assert not is_a_continuation(text)


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


def _held(**fields):
    return {"fields": {k: {"value": v, "source": "tool_result"}
                       for k, v in fields.items()}}


def test_a_follow_up_to_a_held_task_is_not_judged_and_not_refused():
    llm = _FakeLLM([_Reply("query_database",
                           {"question": "who was seen with JOEY at WEZARET DEFA3"})],
                   fit_answers=["NO"])          # the model would have said NO
    call, trace, fit = agent_loop.run_tool_loop(
        llm, user_text="with whom she was", context_block="", db=None,
        dialogue_state=_held(referenced_entity=["JOEY"]), artifact_index=None)

    assert call["name"] == "query_database"
    assert llm.fit_prompts == [], "the gate was consulted about a fact"
    assert fit is True


def test_a_follow_up_with_nothing_held_is_still_judged():
    """No task to continue: the words alone decide, as before."""
    llm = _FakeLLM([_Reply("query_database", {"question": "with whom"}),
                    _Reply("answer_directly", {"answer": "?"})],
                   fit_answers=["NO"])
    call, _trace, _fit = agent_loop.run_tool_loop(
        llm, user_text="with whom she was", context_block="", db=None,
        dialogue_state=None, artifact_index=None)
    assert call["name"] == "answer_directly"
    assert len(llm.fit_prompts) == 1

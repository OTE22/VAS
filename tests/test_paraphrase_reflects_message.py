"""The paraphrase handed to the SQL specialist is of THIS message.

Live:

    user: What are the most active pipelines?
    user: hi
    user: can you track joey
    bot:  SURVEILLANCE INTELLIGENCE REPORT ... top 3 pipelines ...

The loop model paraphrased "can you track joey" as "What are the most active
pipelines?" - the previous question, verbatim - and nothing checked that the
paraphrase had anything to do with the message. The deterministic "track X"
rule did not fire either, because of the polite "can you".

    docker exec face_recognition_api python -m pytest tests/test_paraphrase_reflects_message.py -v
"""

import pytest

from sql_agent.tools import agent_loop
from sql_agent.tools.agent_loop import paraphrase_ignores_user
from sql_agent.tools.planner import deterministic_request_plan


# ------------------------------------------------- the deterministic rule

@pytest.mark.parametrize("text", [
    "track joey", "can you track joey", "could you please track Joey?",
    "please track iron man", "Hey track joey",
    "هل يمكنك تتبع joey", "من فضلك تتبع joey", "تتبع joey", "ممكن تعقب joey؟",
])
def test_politeness_does_not_hide_a_track_command(text):
    plan = deterministic_request_plan(text)
    assert plan is not None and plan.action == "query_database", text


@pytest.mark.parametrize("text", [
    "track him", "can you track them", "track joey and make a pdf",
    "who was detected at camera KSA",
])
def test_the_rule_still_declines_what_it_cannot_settle(text):
    assert deterministic_request_plan(text) is None


# ------------------------------------------------- the paraphrase check

def test_the_previous_question_is_not_a_paraphrase_of_this_one():
    assert paraphrase_ignores_user("What are the most active pipelines?",
                                   "can you track joey")


@pytest.mark.parametrize("question,text", [
    ("all detection events from today", "Show me all detections from today"),
    ("movements of joey across cameras", "can you track joey"),
    ("people detected at camera wezaret", "من تم رصده في كاميرا wezaret؟"),
    ("total detections per camera", "how many detections per camera"),
])
def test_a_real_paraphrase_shares_a_content_word(question, text):
    assert not paraphrase_ignores_user(question, text)


def test_an_arabic_message_without_latin_words_is_not_judged():
    """Its content words are translated into the English paraphrase, so
    nothing can be required - and nothing is."""
    assert not paraphrase_ignores_user("all detections today",
                                       "أظهر لي كل عمليات الرصد اليوم")


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


def test_the_loop_sends_the_model_back_to_the_users_words():
    llm = _FakeLLM([
        _Reply("query_database", {"question": "What are the most active pipelines?"}),
        _Reply("query_database", {"question": "track joey across all cameras"}),
    ])
    call, trace, _fit = agent_loop.run_tool_loop(
        llm, user_text="can you track joey", context_block="", db=None,
        dialogue_state=None, artifact_index=None, known_request=True)

    assert call["arguments"]["question"] == "track joey across all cameras"
    assert any(e.get("rejected") == "paraphrase ignores the message" for e in trace)


def test_a_model_that_insists_on_the_wrong_question_is_not_let_through():
    """Facts do not change with insistence; the rejection budget ends it."""
    wrong = _Reply("query_database", {"question": "What are the most active pipelines?"})
    llm = _FakeLLM([wrong, wrong, wrong, wrong, wrong])
    call, trace, _fit = agent_loop.run_tool_loop(
        llm, user_text="can you track joey", context_block="", db=None,
        dialogue_state=None, artifact_index=None, known_request=True)
    assert call is None
    assert sum(1 for e in trace
               if e.get("rejected") == "paraphrase ignores the message") >= 3


# ------------------------------------------------- the fidelity directive

def test_a_long_identifier_list_is_not_forced_into_the_report():
    from sql_agent.tools.agent_tools import SQLAgentTools as T

    rows = [{"location_name": f"cam {i}"} for i in range(9)]
    assert T._fidelity_directive({"query_result": {"rows": rows}}) == ""
    short = [{"location_name": "KSA"}, {"name": "JOEY"}]
    directive = T._fidelity_directive({"query_result": {"rows": short}})
    assert "You need not name all of them" in directive
    assert "must appear" not in directive

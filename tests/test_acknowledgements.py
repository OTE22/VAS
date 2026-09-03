"""An acknowledgement gets an acknowledgement, from no model.

Live:

    user: thank you
    bot:  The report on Iron Man has been translated into Arabic.
    user: ok
    bot:  It seems you're providing context about our conversation. Is
          there something specific you'd like to discuss or ask about?

The first is the chat model parroting the previous completion line from the
transcript it was handed; the second is it reading the FACTS block as the
user's words. Neither message asked for anything, so neither needs a model,
a transcript or a facts block.

Also here: native tool calling was demoted for the life of the process after
one prose reply. It is re-probed after ten minutes.

    docker exec face_recognition_api python -m pytest tests/test_acknowledgements.py -v
"""

import pytest

from sql_agent.tools import agent_loop
from sql_agent.tools.agent_loop import is_acknowledgement, is_thanks


@pytest.mark.parametrize("text", [
    "ok", "OK.", "okay", "thank you", "thanks!", "Thank you very much",
    "great, thanks", "noted", "cheers", "yes", "شكرا", "شكراً جزيلاً",
    "تمام", "حسناً", "طيب", "ماشي", "جزاك الله خير",
])
def test_these_ask_for_nothing(text):
    assert is_acknowledgement(text)


@pytest.mark.parametrize("text", [
    "ok track joey", "thanks, now show me yesterday", "yes the second one?",
    "what are the most active pipelines", "شكرا، والآن الأمس؟", "hi there",
    "ok ok ok ok ok ok",
])
def test_these_do_not(text):
    assert not is_acknowledgement(text)


def test_thanks_is_told_apart_from_ok():
    assert is_thanks("thank you") and is_thanks("شكرا")
    assert not is_thanks("ok") and not is_thanks("تمام")


# ------------------------------------------------------- the chat node

def _tools():
    from sql_agent.tools.agent_tools import SQLAgentTools

    return SQLAgentTools.__new__(SQLAgentTools)


class _MustNotBeCalled:
    def invoke(self, *a, **k):
        raise AssertionError("a model was consulted for an acknowledgement")


def test_the_reply_is_fixed_and_needs_no_model():
    tools = _tools()
    tools.llm = _MustNotBeCalled()
    for text, lang, expected in [("thank you", "en", "You're welcome."),
                                 ("ok", "en", "Noted."),
                                 ("شكرا", "ar", "على الرحب والسعة."),
                                 ("تمام", "ar", "تمام.")]:
        state = {"acknowledgement": True, "normalized_input": text,
                 "response_language": lang}
        assert tools.handle_chat(state)["final_response"] == expected


def test_ingest_marks_an_acknowledgement_and_plans_chat():
    from sql_agent.tools.agent_tools import SQLAgentTools

    tools = SQLAgentTools.__new__(SQLAgentTools)
    state = {"original_input": "thank you"}
    tools.ingest_query(state)
    assert state["acknowledgement"] is True
    assert state["turn_is_a_request"] is False
    assert state["planned_action"]["source"] == "acknowledgement"

    state = {"original_input": "track joey"}
    tools.ingest_query(state)
    assert state["acknowledgement"] is False


# ------------------------------------------------- native calling re-probe

def test_a_demoted_model_is_probed_again_after_the_window(monkeypatch):
    class _Reply:
        content = ""
        tool_calls = [{"name": "answer_directly", "args": {"answer": "hi"}, "id": "t"}]

    class _LLM:
        model = "fake/reprobe"

        def bind(self, **kwargs):
            return self

        def invoke(self, messages):
            return _Reply()

    agent_loop._NATIVE_SUPPORT["fake/reprobe"] = False
    agent_loop._NATIVE_DEMOTED_AT["fake/reprobe"] = 0.0     # long ago

    call, trace, _fit = agent_loop.run_tool_loop(
        _LLM(), user_text="hello there", context_block="", db=None,
        dialogue_state=None, artifact_index=None)

    assert call["name"] == "answer_directly"
    # the demotion was lifted for the probe, and native calling worked
    assert agent_loop._NATIVE_SUPPORT.get("fake/reprobe") is not False

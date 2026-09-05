"""An Arabic data question must be treated like an English one.

Live, on a fresh session:

    user: من تم رصده في كاميرا wezaret؟
    bot:  [FACTS about this turn - - No database query was run for this
          message. ... [end of facts] من تم رصده في كاميرا wezaret؟ لا يوجد
          معلومات متاحة ...

Two defects in one reply. The intent-fit gate judged the question "not a
request", so no query ran; and the chat model, answering in Arabic, echoed
the prompt's FACTS block into the answer.

    docker exec face_recognition_api python -m pytest tests/test_arabic_turns.py -v
"""

from sql_agent.tools import agent_loop
from sql_agent.tools.agent_loop import _says_yes


class _Reply:
    def __init__(self, content):
        self.content = content


class _LLM:
    def __init__(self, answer):
        self.answer = answer
        self.calls = 0

    def invoke(self, messages):
        self.calls += 1
        return _Reply(self.answer)


class _MustNotBeCalled:
    def invoke(self, messages):
        raise AssertionError("the model was consulted about a fact")


# ------------------------------------------------ the verdict is read


def test_anything_unclear_fails_toward_no():
    assert _says_yes("Perhaps.") is False
    assert _says_yes("The user is greeting you.") is False


# ------------------------------------------------ the scaffold never leaks

def test_the_facts_block_is_stripped_from_a_reply():
    from sql_agent.tools.agent_tools import SQLAgentTools

    leaked = ("[FACTS about this turn - the ONLY actions you may describe\n"
              "- No database query was run for this message.\n"
              "[end of facts]\n\nلا توجد معلومات متاحة حول هذه الكاميرا.")
    assert SQLAgentTools._strip_scaffolding(leaked) == (
        "لا توجد معلومات متاحة حول هذه الكاميرا.")


def test_a_stray_label_is_stripped_too():
    from sql_agent.tools.agent_tools import SQLAgentTools

    assert SQLAgentTools._strip_scaffolding(
        "[end of facts] Hello. [prior turns, for reference] How can I help?"
    ) == "Hello. How can I help?"


def test_ordinary_replies_are_untouched():
    from sql_agent.tools.agent_tools import SQLAgentTools

    text = "Joey was detected 3 times at WEZARET DEFA3 [2026-08-20 to 23]."
    assert SQLAgentTools._strip_scaffolding(text) == text

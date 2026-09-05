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


# ------------------------------------------------- the deterministic rule


# ------------------------------------------------- the paraphrase check


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


# ------------------------------------------------- the fidelity directive

def test_a_long_identifier_list_is_not_forced_into_the_report():
    from sql_agent.tools.agent_tools import SQLAgentTools as T

    rows = [{"location_name": f"cam {i}"} for i in range(9)]
    assert T._fidelity_directive({"query_result": {"rows": rows}}) == ""
    short = [{"location_name": "KSA"}, {"name": "JOEY"}]
    directive = T._fidelity_directive({"query_result": {"rows": short}})
    assert "You need not name all of them" in directive
    assert "must appear" not in directive

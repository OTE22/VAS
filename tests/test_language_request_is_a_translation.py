""""Make the report in Arabic" is a translation of the report you have.

Live:

    user: can you make the report in arabic
    bot:  لقد أعددت can you make the report in arabic بصيغة PDF.

The model ran a NEW query for those words, made a PDF of the irrelevant
result, and titled it with the request. The conversation held the report;
nothing checked the model's choice against it.

    docker exec face_recognition_api python -m pytest tests/test_language_request_is_a_translation.py -v
"""

import pytest

from sql_agent.tools import agent_loop
from sql_agent.tools.agent_loop import wants_translation


@pytest.mark.parametrize("text,lang", [
    ("can you make the report in arabic", "ar"),
    ("translate it to arabic", "ar"),
    ("the same in English please", "en"),
    ("arabic version of that report", "ar"),
    ("اجعل التقرير بالعربية", "ar"),
    ("التقرير بالإنجليزية", "en"),
    ("هذا بالعربي", "ar"),
])
def test_a_language_request_about_the_report_is_a_translation(text, lang):
    assert wants_translation(text) == lang


@pytest.mark.parametrize("text", [
    "track joey in arabic",            # a new request with an output language
    "who was detected at camera KSA",
    "in arabic",                        # points at nothing
    "تتبع joey بالعربية",
])
def test_a_new_request_with_an_output_language_is_left_alone(text):
    assert wants_translation(text) is None


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


@pytest.mark.parametrize("proposed,args", [
    ("query_database", {"question": "can you make the report in arabic"}),
    ("generate_document", {"format": "pdf", "language": "ar"}),
])
def test_the_models_choice_is_replaced_by_a_translation(proposed, args):
    llm = _FakeLLM([_Reply(proposed, args)])
    call, _trace, _fit = agent_loop.run_tool_loop(
        llm, user_text="can you make the report in arabic", context_block="",
        db=None, dialogue_state=None, artifact_index=None, known_request=True,
        has_result=True)

    assert call["name"] == "translate_document"
    assert call["arguments"]["language"] == "ar"


def test_a_document_is_titled_by_its_subject_never_by_the_request():
    from sql_agent.tools.agent_tools import SQLAgentTools as T

    wc = {"dialogue_state": {"fields": {"referenced_entity": {"value": ["IRON MAN"]}}}}
    assert T._subject_title({"response_language": "en"}, wc) == "IRON MAN - tracking report"
    assert T._subject_title({"response_language": "ar"}, wc) == "تقرير تتبع - IRON MAN"
    assert T._subject_title({}, {"dialogue_state": {"fields": {}}}) == ""
    assert T._usable_title("can you make the report in arabic") == ""
    assert T._usable_title("اجعل التقرير بالعربية") == ""
    assert T._usable_title("track iron man") == "track iron man"


def test_with_nothing_to_translate_the_model_keeps_its_choice():
    llm = _FakeLLM([_Reply("query_database", {"question": "report in arabic"})])
    call, _trace, _fit = agent_loop.run_tool_loop(
        llm, user_text="can you make the report in arabic", context_block="",
        db=None, dialogue_state=None, artifact_index=None, known_request=True,
        has_result=False)
    assert call["name"] == "query_database"

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


def test_a_document_is_titled_by_its_subject_never_by_the_request():
    from sql_agent.tools.agent_tools import SQLAgentTools as T

    wc = {"dialogue_state": {"fields": {"referenced_entity": {"value": ["IRON MAN"]}}}}
    assert T._subject_title({"response_language": "en"}, wc) == "IRON MAN - tracking report"
    assert T._subject_title({"response_language": "ar"}, wc) == "تقرير تتبع - IRON MAN"
    assert T._subject_title({}, {"dialogue_state": {"fields": {}}}) == ""
    assert T._usable_title("can you make the report in arabic") == ""
    assert T._usable_title("اجعل التقرير بالعربية") == ""
    assert T._usable_title("track iron man") == "track iron man"



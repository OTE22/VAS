"""A translation is something the USER asks for.

    user: report for tracking joey
    bot:  الرقم 1: آخر ظهور لجوزي ... المكتبة المرئية: WEZARET DEFA3

The message was English, named an enrolled person and asked for a report;
the model proposed translate_document(language="ar") and the loop committed
it, because only the "did they ask for a language" direction was ever
checked. The reply's language is the message's own unless the message asks
for another.

    docker exec face_recognition_api python -m pytest tests/test_translation_must_be_asked_for.py -v
"""

from sql_agent.tools import agent_loop
from sql_agent.tools.agent_tools import SQLAgentTools as T
from sql_agent.tools.planner import PlannedAction

INDEX = [{"identity_id": "1", "display_name": "JOEY"}]


class _Reply:
    def __init__(self, name, arguments):
        self.content = ""
        self.tool_calls = [{"name": name, "args": arguments, "id": "t"}] if name else []


class _FakeLLM:
    def __init__(self, replies):
        self.replies = list(replies)
        self.model = "fake/test-model"

    def bind(self, **kwargs):
        return self

    def invoke(self, messages):
        return self.replies.pop(0) if self.replies else _Reply(None, {})


def test_the_loop_refuses_a_translation_nobody_asked_for():
    llm = _FakeLLM([
        _Reply("translate_document", {"document_id": "", "language": "ar"}),
        _Reply("query_database", {"question": "all detections of JOEY"}),
    ])
    call, trace, _fit = agent_loop.run_tool_loop(
        llm, user_text="report for tracking joey", context_block="", db=None,
        dialogue_state=None, artifact_index=None, identity_index=INDEX,
        has_result=True)

    assert call["name"] == "query_database"
    assert any(e.get("rejected") == "no language was asked for" for e in trace)


def test_a_requested_translation_still_runs():
    llm = _FakeLLM([_Reply("translate_document", {"document_id": "", "language": "ar"})])
    call, _trace, _fit = agent_loop.run_tool_loop(
        llm, user_text="can you make the report in arabic", context_block="",
        db=None, dialogue_state=None, artifact_index=None,
        identity_index=INDEX, has_result=True)
    assert call["name"] == "translate_document"
    assert call["arguments"]["language"] == "ar"


def test_the_planner_path_answers_the_message_instead():
    tools = T.__new__(T)
    state = {"turn_kind": agent_loop.DATA, "response_language": "en"}
    plan = PlannedAction("translate_artifact", language="ar", target="last_result")
    out = tools._refuse_unrequested_translation(state, plan, "report for tracking joey")
    assert out.action == "query_database"
    assert out.language is None and "translation-refused" in out.source

    asked = PlannedAction("translate_artifact", language="ar")
    kept = tools._refuse_unrequested_translation(
        state, asked, "can you make the report in arabic")
    assert kept.action == "translate_artifact" and kept.language == "ar"


def test_a_language_request_needs_no_preposition():
    """"make it Arabic" is a translation request; the pattern wanted
    "in/to Arabic" and the new guard then refused a genuine one."""
    assert agent_loop.wants_translation("make it Arabic") == "ar"
    assert agent_loop.wants_translation("put that in English") == "en"
    assert agent_loop.wants_translation("the report, arabic please") == "ar"
    # Still a NEW request with an output language, not a translation.
    assert agent_loop.wants_translation("track joey in arabic") is None
    assert agent_loop.wants_translation("report for tracking joey") is None


def test_the_reply_language_is_the_messages_unless_it_asks():
    """A plan language used to overwrite the language the input pipeline
    detected, so a held "ar" reached an English turn."""
    state = {"response_language": "en"}
    assert not T._language_was_requested(state, {"action": "query_database",
                                                 "language": "ar"},
                                         "report for tracking joey")
    assert T._language_was_requested(state, {"action": "translate_artifact",
                                             "language": "ar"},
                                     "can you make the report in arabic")
    assert T._language_was_requested(state, {"action": "generate_document",
                                             "language": "ar"}, "make that a PDF")

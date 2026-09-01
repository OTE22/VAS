"""An action has to fit what was asked, not just succeed.

Reported and reproduced: with a report in the session, saying "hi" produced

    "لقد أعددت **Brief explanation of what the query does** بصيغة PDF"

The reasoning layer never catches this. It asks "did the action SUCCEED?" and
the PDF was perfectly valid — the artifact existed, the observation said
success, the user was told their document was ready. Nothing asked whether
the action FIT the request.

No structural rule separates these cases. `select_mode` correctly returns
CONTEXTUAL, because context genuinely exists. `looks_action_shaped` is True
for any short utterance once there is context, so "hi" and "make it Arabic"
are identical to it. `_REFERENTIAL` would wrongly block "only camera 3",
which refers to the previous query without a pronoun. Whether an utterance
expresses a WANT is semantic, and a keyword list for it misses "thanks",
"who are you", "شكرا", and everything nobody thought of.

So the model is asked one closed question, and Python enforces the answer —
only for actions that consume prior context, and failing safe toward NOT
acting.

    docker exec face_recognition_api python -m pytest tests/test_intent_fit.py -v
"""

import json
import logging

import pytest

from sql_agent.tools import agent_loop, tool_registry as tr


class _Reply:
    def __init__(self, name, arguments):
        self.tool_calls = [{"name": name, "args": arguments}]
        self.content = ""


class _Text:
    def __init__(self, content):
        self.content = content
        self.tool_calls = []


class _FakeLLM:
    """Scripted model. Answers the intent-fit question from `fit_answers`."""

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
            answer = (self.fit_answers.pop(0) if self.fit_answers else "YES")
            return _Text(answer)
        return self.replies.pop(0) if self.replies else _Reply(None, {})


def _run(llm, user_text, **kwargs):
    call, trace, _fit = agent_loop.run_tool_loop(
        llm, user_text=user_text, context_block="", db=None,
        dialogue_state=kwargs.get("dialogue_state"),
        artifact_index=kwargs.get("artifact_index"))
    return call, trace


# ------------------------------------------------------------- the defect

def test_a_greeting_does_not_produce_a_document(caplog):
    """THE bug. A valid PDF is still the wrong answer to "hi"."""
    llm = _FakeLLM(
        replies=[_Reply("generate_document", {"format": "pdf"}),
                 _Reply("answer_directly", {"answer": "Hello. How can I help?"})],
        fit_answers=["NO"])

    with caplog.at_level(logging.INFO, logger="sql_agent.tools.agent_loop"):
        call, trace = _run(llm, "hi")

    assert call["name"] == "answer_directly", (
        f"a greeting produced {call['name']}")
    assert any(e.get("rejected") == "not what the user asked" for e in trace)
    assert any("did not ask for it" in r.getMessage() for r in caplog.records)


def test_the_model_is_told_what_the_user_actually_said():
    """A refusal it cannot act on just wastes the turn."""
    llm = _FakeLLM(
        replies=[_Reply("generate_document", {"format": "pdf"}),
                 _Reply("answer_directly", {"answer": "Hello. How can I help?"})],
        fit_answers=["NO"])
    _run(llm, "hi")

    assert llm.fit_prompts, "the fit question was never asked"


@pytest.mark.parametrize("tool,arguments", [
    ("generate_document", {"format": "pdf"}),
    ("translate_document", {"language": "ar"}),
    ("modify_active_query", {"change": "only camera 3"}),
])
def test_every_context_consuming_action_is_checked(tool, arguments):
    """All three operate on something the user must have referred to."""
    llm = _FakeLLM(replies=[_Reply(tool, arguments),
                            _Reply("answer_directly", {"answer": "Hello. How can I help?"})],
                   fit_answers=["NO"])
    call, _trace = _run(llm, "thanks")

    assert call["name"] == "answer_directly", f"{tool} survived a 'NO'"


# --------------------------------------------------- it must not over-fire

@pytest.mark.parametrize("text,tool,arguments", [
    ("make it Arabic", "translate_document", {"language": "ar"}),
    ("make that a PDF", "generate_document", {"format": "pdf"}),
    ("only camera 3", "modify_active_query", {"change": "only camera 3"}),
])
def test_a_real_request_is_allowed_through(text, tool, arguments):
    """The negative control. Refusing everything would also 'fix' the bug.

    "only camera 3" matters most here: it refers to the previous query
    without a pronoun, which is exactly what a keyword rule would miss.
    """
    llm = _FakeLLM(replies=[_Reply(tool, arguments)], fit_answers=["YES"])
    call, _trace = _run(llm, text)

    assert call["name"] == tool, f"{text!r} was wrongly refused"


def test_a_greeting_does_not_run_a_query_either():
    """Narrowing the guard to document tools drew the line in the wrong place.

    With that version, "hi" stopped producing a PDF and started producing a
    database query instead — the same failure wearing a different hat.
    """
    llm = _FakeLLM(
        replies=[_Reply("query_database", {"question": "Joey tracking"}),
                 _Reply("answer_directly", {"answer": "Hello. How can I help?"})],
        fit_answers=["NO"])
    call, _trace = _run(llm, "hi")

    assert call["name"] == "answer_directly", (
        "a greeting still ran a query")


def test_the_question_is_asked_once_per_turn_not_once_per_tool():
    """It judges the MESSAGE, so asking twice is waste, not diligence."""
    llm = _FakeLLM(
        replies=[_Reply("query_database", {"question": "x"}),
                 _Reply("generate_document", {"format": "pdf"}),
                 _Reply("answer_directly", {"answer": "Hello."})],
        fit_answers=["NO"])
    _run(llm, "hi")

    assert len(llm.fit_prompts) == 1, (
        f"asked {len(llm.fit_prompts)} times for one message")


def test_answering_directly_needs_no_permission():
    """Responding is always a safe reply, so it must never be gated."""
    llm = _FakeLLM(replies=[_Reply("answer_directly",
                                   {"answer": "Hello. How can I help?"})])
    call, _trace = _run(llm, "hi")

    assert call["name"] == "answer_directly"
    assert llm.fit_prompts == [], "answering was gated behind a model call"


def test_a_real_question_is_allowed_and_costs_one_check():
    """The negative control: ordinary queries must still work."""
    llm = _FakeLLM(replies=[_Reply("query_database",
                                   {"question": "how many cameras"})],
                   fit_answers=["YES"])
    call, _trace = _run(llm, "how many cameras are registered?")

    assert call["name"] == "query_database"
    assert len(llm.fit_prompts) == 1


# ------------------------------------------------------------ failing safe

def test_an_unclear_answer_is_treated_as_no():
    """Fails toward NOT acting: an unrequested document is the worse error."""
    llm = _FakeLLM(replies=[_Reply("generate_document", {"format": "pdf"}),
                            _Reply("answer_directly", {"answer": "Hello. How can I help?"})],
                   fit_answers=["I think maybe?"])
    call, _trace = _run(llm, "hi")

    assert call["name"] == "answer_directly"


def test_a_model_failure_does_not_break_the_turn():
    """This may refuse an action; it must never break one."""

    class _Broken(_FakeLLM):
        def invoke(self, messages):
            text = "\n".join(str(getattr(m, "content", "")) for m in messages)
            if "You judge ONE thing" in text:
                raise RuntimeError("model unavailable")
            return super().invoke(messages)

    llm = _Broken(replies=[_Reply("generate_document", {"format": "pdf"})])
    call, _trace = _run(llm, "make that a PDF")

    assert call["name"] == "generate_document", (
        "a failed fit check blocked a legitimate action")


def test_an_empty_answer_is_not_a_refusal():
    """Silence is not evidence the user asked for nothing."""
    llm = _FakeLLM(replies=[_Reply("generate_document", {"format": "pdf"})],
                   fit_answers=[""])
    call, _trace = _run(llm, "make that a PDF")

    assert call["name"] == "generate_document"


# --------------------------------------------------- the placeholder title

def test_prompt_scaffolding_never_becomes_a_document_title(monkeypatch):
    """Observed: "I have prepared **Brief explanation of what the query does**".

    The model echoed the SQL prompt's own example text back as the purpose,
    and it went onto a real document. Matched exactly against our template
    string, not guessed at.
    """
    import sql_agent.tools.agent_tools as module
    monkeypatch.setattr(module, "create_llm", lambda *a, **k: None)
    monkeypatch.setattr(module, "create_sql_llm", lambda *a, **k: None)
    monkeypatch.setattr(module, "DatabaseManager", lambda *a, **k: object())
    monkeypatch.setattr(module, "SQLKnowledgeBase", lambda *a, **k: None)
    tools = module.SQLAgentTools(conversation_memory=None)

    assert tools._usable_title("Brief explanation of what the query does") == ""
    assert tools._usable_title("YOUR SQL QUERY HERE") == ""
    assert tools._usable_title("  ") == ""
    # ...and a real purpose still works.
    assert tools._usable_title("Count of registered cameras") == (
        "Count of registered cameras")


def test_the_placeholder_set_matches_the_actual_prompt():
    """The guard is worthless if the template changes and this does not."""
    import inspect

    import sql_agent.tools.agent_tools as module

    source = inspect.getsource(module.SQLAgentTools.generate_sql)
    placeholders = module.SQLAgentTools._PROMPT_PLACEHOLDERS

    assert any(p in source.lower() for p in placeholders), (
        "none of the guarded placeholders appear in the SQL prompt any more; "
        "the template changed and this list did not follow it")

"""What the tool loop does, and what you can see it doing.

Two properties, and they are related. The loop was instrumented only where it
failed, so a working turn emitted a single line — enough to prove a tool was
chosen, not enough to answer any question you ask when you doubt it. Adding
the trace immediately exposed a defect it had been hiding:

    [TOOL_LOOP] start model=meta/llama-3.2-11b-vision-instruct mechanism=native
    [TOOL_LOOP] step=0 mechanism=native proposed=ask_clarifying_question
    [TOOL_LOOP] committed to ask_clarifying_question after 0 look-up(s)

A clean session, the plain question "How many cameras are registered?", and
the agent answered "Do you want the previous result as a document?" — about a
previous result that did not exist. It never reached the SQL chain, so no
reasoning edge could ever run. The agent looked like it was not thinking
because it was quitting before it started.

Pinned here:

  1. The trace states the MECHANISM (native vs prompted fallback) on every
     turn, names the model, and records each proposal and look-up.
  2. It never logs argument VALUES — a person's name and the user's own
     question are not log-file material.
  3. The logger cannot take a turn down. Arguments reaching it are raw and
     unvalidated; a model may send a bare string.
  4. An OPENING clarification — proposed before anything has been looked up
     — is REFUSED, structurally and never by inspecting the user's words, and
     the model is told why so it can choose again.
  5. The legitimate clarification paths still work.

    docker exec face_recognition_api python -m pytest tests/test_tool_loop_observability.py -v
"""

import json
import logging

import pytest

from sql_agent.tools import agent_loop


# --------------------------------------------------------------- harness

class _Reply:
    """A native-style reply carrying tool_calls, as LangChain exposes them."""

    def __init__(self, name, arguments):
        self.tool_calls = [{"name": name, "args": arguments}]
        self.content = ""


class _FakeLLM:
    """Scripted model. `bind` succeeds, so the native path is taken."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.seen = []
        self.model = "fake/test-model"

    def bind(self, **kwargs):
        self.bound = kwargs
        return self

    def invoke(self, messages):
        self.seen.append(messages)
        return self.replies.pop(0) if self.replies else _Reply(None, {})


def _run(llm, *, dialogue_state=None, artifact_index=None, db=None,
         user_text="how many cameras are registered?"):
    call, trace, _fit = agent_loop.run_tool_loop(
        llm, user_text=user_text, context_block="", db=db,
        dialogue_state=dialogue_state, artifact_index=artifact_index)
    return call, trace


# ------------------------------------------------- 1-2. the trace itself

def test_the_trace_names_the_mechanism_and_the_model(caplog):
    """"Is it really doing function calling?" must be answerable from a log.

    The mechanism was previously logged only when the fallback FIRST kicked
    in — once, per model, per process — so on any later turn there was
    nothing to see.
    """
    llm = _FakeLLM([_Reply("query_database", {"question": "how many cameras"})])
    with caplog.at_level(logging.INFO, logger="sql_agent.tools.agent_loop"):
        call, _trace = _run(llm)

    assert call["name"] == "query_database"
    lines = [r.getMessage() for r in caplog.records]
    start = [l for l in lines if "[TOOL_LOOP] start" in l]
    assert start, "the loop never announced how it was calling tools"
    assert "mechanism=native" in start[0]
    assert "fake/test-model" in start[0]
    assert any("proposed=query_database" in l for l in lines)
    assert any("via native calling" in l for l in lines)


def test_the_prompted_fallback_is_visible_as_such(caplog):
    """A model that ignores a tools payload must not look like a native one."""

    class _Deaf(_FakeLLM):
        def bind(self, **kwargs):
            raise NotImplementedError("this model has no tools API")

    llm = _Deaf([_Reply("query_database", {"question": "x"})])
    with caplog.at_level(logging.INFO, logger="sql_agent.tools.agent_loop"):
        _run(llm)

    start = [r.getMessage() for r in caplog.records if "[TOOL_LOOP] start" in r.getMessage()]
    assert start and "mechanism=prompted" in start[0]


def test_the_trace_records_look_ups_and_their_shape(caplog):
    """A model acting on an error it never saw is the bug this catches."""
    llm = _FakeLLM([_Reply("list_cameras", {}),
                    _Reply("query_database", {"question": "x"})])

    class _Db:
        pass

    with caplog.at_level(logging.INFO, logger="sql_agent.tools.agent_loop"):
        _run(llm, db=_Db())

    lines = [r.getMessage() for r in caplog.records]
    assert any("lookup=list_cameras" in l for l in lines), lines


def test_argument_values_never_reach_the_log(caplog):
    """A `question` is the user's own words; a name is somebody's name.

    The audit line has always excluded both. A second log that includes them
    would simply move the problem.
    """
    secret = "where was Ali-Hassan seen with plate XYZ-999"
    llm = _FakeLLM([_Reply("query_database", {"question": secret})])
    with caplog.at_level(logging.INFO, logger="sql_agent.tools.agent_loop"):
        _run(llm, user_text=secret)

    blob = "\n".join(r.getMessage() for r in caplog.records)
    assert "Ali-Hassan" not in blob
    assert "XYZ-999" not in blob
    assert f"<str:{len(secret)}>" in blob, "the shape should still be recorded"


@pytest.mark.parametrize("arguments", ["a bare string", ["a", "list"], 42, None])
def test_the_logger_survives_any_argument_shape(arguments, caplog):
    """These arguments have NOT been validated yet.

    A model can hand back anything. A logger that assumes a dict takes the
    whole turn down — strictly worse than the log line it was added for.
    """
    llm = _FakeLLM([_Reply("query_database", arguments)])
    with caplog.at_level(logging.INFO, logger="sql_agent.tools.agent_loop"):
        call, _trace = _run(llm)     # must not raise
    assert call is None or call["name"] == "query_database"


# ------------------------------------- 4-5. clarification is a last resort

def test_an_opening_clarification_is_refused(caplog):
    """The observed defect: a plain question answered with a question.

    Nothing has been looked up, so the claim that the request is ambiguous
    is untested — and the loop exists so the model can test it.
    """
    llm = _FakeLLM([
        _Reply("ask_clarifying_question",
               {"question": "Do you want the previous result as a document?"}),
        _Reply("query_database", {"question": "how many cameras"}),
    ])
    with caplog.at_level(logging.INFO, logger="sql_agent.tools.agent_loop"):
        call, trace = _run(llm, dialogue_state={}, artifact_index=[])

    assert call["name"] == "query_database", (
        "the loop settled for a question instead of an answer")
    assert any(entry.get("rejected") for entry in trace)
    assert any("refused a clarification" in r.getMessage()
               for r in caplog.records)


def test_a_second_clarification_is_also_refused_when_nothing_ran(caplog):
    """The hole the first version of this guard left.

    It tested `not trace` — but the trace also carries REJECTIONS, and the
    guard appends one itself. So the model's second attempt at the same
    clarification passed, on the strength of the first having been refused:
    exactly the retry the guard exists to stop.

    Found by `test_agent_e2e` over SSE, where a document request came back as
    "Could you please clarify which camera you are referring to?".
    """
    llm = _FakeLLM([
        _Reply("ask_clarifying_question", {"question": "which camera?"}),
        _Reply("ask_clarifying_question", {"question": "which camera then?"}),
        _Reply("generate_document", {"format": "pdf"}),
    ])
    with caplog.at_level(logging.INFO, logger="sql_agent.tools.agent_loop"):
        call, trace = _run(llm, user_text="make that a PDF")

    assert call["name"] == "generate_document", (
        "a repeated clarification got through on the back of the first refusal")
    refusals = [r.getMessage() for r in caplog.records
                if "refused a clarification" in r.getMessage()]
    assert len(refusals) == 2, f"expected both refused, saw {len(refusals)}"


def test_a_rejection_does_not_count_as_having_checked():
    """The general form: only an EXECUTED look-up earns the right to ask.

    A tool call the registry rejected ran nothing and learned nothing.
    """
    llm = _FakeLLM([
        # Rejected by validate_call — no look-up happens.
        _Reply("query_database", {"question": "SELECT * FROM users"}),
        _Reply("ask_clarifying_question", {"question": "which one?"}),
        _Reply("query_database", {"question": "how many cameras"}),
    ])
    call, trace = _run(llm)

    assert call["name"] == "query_database"
    assert not any(entry.get("tool") == "ask_clarifying_question"
                   and entry.get("committed") for entry in trace)


def test_the_model_is_told_why_so_it_can_choose_again():
    """A rejection the model cannot act on is just a failed turn."""
    llm = _FakeLLM([
        _Reply("ask_clarifying_question", {"question": "which one?"}),
        _Reply("query_database", {"question": "how many cameras"}),
    ])
    _run(llm, dialogue_state={}, artifact_index=[])

    # The reason must reach the LOOP conversation. `seen[-1]` is no longer a
    # safe way to find it: the intent-fit check also calls the model, so the
    # last exchange can be its prompt. Search every exchange rather than
    # assuming which one it was.
    text = "\n".join(str(getattr(m, "content", ""))
                     for messages in llm.seen for m in messages).lower()
    assert "not looked anything up" in text, "no reason was given"
    assert "look-up tool" in text, "no alternative was offered"
    assert "answer the request" in text


def test_a_clarification_is_allowed_once_a_look_up_found_nothing():
    """The legitimate path. Asking beats guessing at somebody's identity."""

    class _Db:
        pass

    llm = _FakeLLM([
        _Reply("resolve_person", {"name": "Alii"}),
        _Reply("ask_clarifying_question", {"question": "Which person?"}),
    ])
    call, trace = _run(llm, db=_Db(), dialogue_state={}, artifact_index=[])

    assert call is not None and call["name"] == "ask_clarifying_question", (
        "a clarification after a failed look-up was wrongly refused")


def test_a_full_session_does_not_excuse_an_opening_clarification():
    """The weakness that made the first version of this guard useless.

    It originally also required an empty session. But `artifact_index` is
    non-empty for anybody who has ever generated a document — every real
    user — so in production the guard almost never fired and a plain question
    was still answered with a question. Having documents on file says nothing
    about whether THIS request is ambiguous.
    """
    llm = _FakeLLM([
        _Reply("ask_clarifying_question", {"question": "Which report?"}),
        _Reply("query_database", {"question": "how many cameras"}),
    ])
    call, _trace = _run(
        llm, user_text="how many cameras are registered?",
        dialogue_state={"fields": {"active_camera": {"value": [3]}}},
        artifact_index=[{"id": "a", "format": "pdf"},
                        {"id": "b", "format": "pdf"}])

    assert call["name"] == "query_database"


def test_the_guard_reads_the_trace_not_the_users_words():
    """No phrasebook. The SAME text and the SAME session, refused or allowed
    purely on whether anything has been checked yet.

    A lexical rule would have to decide what "ambiguous" sounds like, which
    is exactly the keyword matching this codebase refuses. The property here
    is "has this claim been tested", which is visible in the trace.
    """

    class _Db:
        pass

    text = "do it for him"

    # Nothing tried yet -> refused, and the model moves on.
    refused = _FakeLLM([
        _Reply("ask_clarifying_question", {"question": "Who?"}),
        _Reply("query_database", {"question": "x"}),
    ])
    call_first, _ = _run(refused, user_text=text, db=_Db())

    # Same text, same (empty) session — but a look-up ran first.
    allowed = _FakeLLM([
        _Reply("resolve_person", {"name": "him"}),
        _Reply("ask_clarifying_question", {"question": "Who?"}),
    ])
    call_after, _ = _run(allowed, user_text=text, db=_Db())

    assert call_first["name"] == "query_database"
    assert call_after["name"] == "ask_clarifying_question"

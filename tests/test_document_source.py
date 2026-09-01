"""A document must contain a report, not the apology for not having one.

Observed live. Asked "make it Arabic and generate a PDF", the agent produced
a PDF, announced it as

    "لقد أعددت **i am just saying hi** بصيغة PDF"

and the document it handed over contained, in full:

    "I couldn't reach that report to translate it. Please try asking for it
     again."

So the previous turn's FAILURE NOTICE became a downloadable intelligence
report, titled after an unrelated conversation, presented as a success. For a
surveillance product that is close to the worst kind of wrong: the artifact
looks official, is dated, is filed under the user's documents, and says
nothing true.

`_document_source_text` chose the previous narrative on LENGTH — the first AI
message over 40 characters — which cannot tell a report from an apology. The
invariant check did not catch it either: that asks whether an artifact was
REGISTERED, and one was. Registering something is not the same as having
something to say.

    docker exec face_recognition_api python -m pytest tests/test_document_source.py -v
"""

import pytest


FAILURE_NOTICE = ("I couldn't reach that report to translate it. "
                  "Please try asking for it again.")
REAL_REPORT = ("**SURVEILLANCE INTELLIGENCE REPORT** The system has 16 "
               "registered cameras across three sites.")


class _Memory:
    """Conversation memory holding one prior AI message."""

    def __init__(self, last_ai_text):
        self.last_ai_text = last_ai_text
        self.user_id = 1

    def get_recent_messages(self, limit=8):
        from langchain_core.messages import AIMessage, HumanMessage
        return [HumanMessage(content="make that a PDF"),
                AIMessage(content=self.last_ai_text)]


def _tools(monkeypatch, last_ai_text):
    import sql_agent.tools.agent_tools as module
    monkeypatch.setattr(module, "create_llm", lambda *a, **k: None)
    monkeypatch.setattr(module, "create_sql_llm", lambda *a, **k: None)
    monkeypatch.setattr(module, "DatabaseManager", lambda *a, **k: object())
    monkeypatch.setattr(module, "SQLKnowledgeBase", lambda *a, **k: None)
    return module.SQLAgentTools(conversation_memory=_Memory(last_ai_text))


def _state(**extra):
    state = {"final_response": "", "working_context": {},
             "response_language": "en", "planned_action": {"action": "generate_document"}}
    state.update(extra)
    return state


# ------------------------------------------------------------- the defect

def test_a_failed_turns_narrative_is_never_rendered(monkeypatch):
    """THE bug. A PDF whose body is an apology is worse than no PDF."""
    tools = _tools(monkeypatch, FAILURE_NOTICE)
    content = tools._document_source_text(_state(
        working_context={"last_narrative_reportable": False}))

    assert FAILURE_NOTICE not in content, (
        f"the failure notice was rendered as the document: {content!r}")


def test_a_real_report_is_still_rendered(monkeypatch):
    """The negative control. Refusing everything would also 'fix' the bug."""
    tools = _tools(monkeypatch, REAL_REPORT)
    content = tools._document_source_text(_state(
        working_context={"last_narrative_reportable": True}))

    assert content == REAL_REPORT


def test_an_unknown_history_still_renders(monkeypatch):
    """Sessions written before this flag existed must not lose the feature.

    Absent is not the same as False. Treating it as False would silently stop
    documents working for every conversation already on disk.
    """
    tools = _tools(monkeypatch, REAL_REPORT)
    content = tools._document_source_text(_state(working_context={}))

    assert content == REAL_REPORT


def test_this_turns_own_response_always_wins(monkeypatch):
    """A narrative written THIS turn needs no history check."""
    tools = _tools(monkeypatch, FAILURE_NOTICE)
    content = tools._document_source_text(_state(
        final_response=REAL_REPORT,
        working_context={"last_narrative_reportable": False}))

    assert content == REAL_REPORT


def test_with_nothing_reportable_the_user_is_asked_what_to_report_on(monkeypatch):
    """Refusing has to leave the user somewhere to go."""
    tools = _tools(monkeypatch, FAILURE_NOTICE)
    state = _state(working_context={"last_narrative_reportable": False})
    out = tools.render_artifact(state)

    assert not out.get("artifact_payload"), "a document was produced anyway"
    assert "don't have anything to put in a document" in out["final_response"]


# ------------------------------------------------------------ the title

def test_the_title_survives_an_unrelated_turn_in_between(monkeypatch):
    """THE live failure: track Joey -> hi -> make that a PDF, titled "hi".

    `last_query` is "the last thing typed", so an unrelated turn between the
    query and the document request renames the report. The question travels
    WITH the result now, so nothing in between can displace it.
    """
    tools = _tools(monkeypatch, REAL_REPORT)
    out = tools.render_artifact(_state(
        final_response=REAL_REPORT,
        working_context={
            "last_narrative_reportable": True,
            "last_query": "hi",                       # the turn in between
            "last_result": {"question": "track Joey",
                            "purpose": "Track all detections of a person named Joey",
                            "row_count": 3},
        }))

    payload = out.get("artifact_payload") or {}
    assert payload.get("title") == "track Joey", (
        f"an unrelated turn renamed the report: {payload.get('title')!r}")


def test_the_title_is_the_question_the_document_answers(monkeypatch):
    """Name it after what the user asked, in their words.

    `last_query` holds the PREVIOUS turn's request, because this turn's
    ("make that a PDF") is recorded only at the end — so it is the question
    the result answers.

    An earlier version of this test preferred `last_result["purpose"]`, to
    avoid a stale greeting becoming a title. That produced titles like
    "Track all detections of a person named Joey including which camera
    detected them, ordered chronologically for story gene": `purpose` is
    written FOR the SQL generator, not for a person. The stale-greeting path
    is closed at its source now — a greeting no longer produces a document
    at all (tests/test_intent_fit.py).
    """
    tools = _tools(monkeypatch, REAL_REPORT)
    out = tools.render_artifact(_state(
        final_response=REAL_REPORT,
        working_context={
            "last_narrative_reportable": True,
            "last_query": "make that a PDF",
            "last_result": {"question": "track Joey",
                            "purpose": "Track all detections of a person "
                                       "named Joey including which camera "
                                       "detected them, ordered chronologically",
                            "row_count": 3},
        }))

    payload = out.get("artifact_payload") or {}
    assert payload.get("title") == "track Joey", (
        f"document titled {payload.get('title')!r}")


def test_a_long_title_is_cut_at_a_word_boundary(monkeypatch):
    """"...ordered chronologically for story gene" reads as a bug."""
    tools = _tools(monkeypatch, REAL_REPORT)
    out = tools.render_artifact(_state(
        final_response=REAL_REPORT,
        working_context={"last_narrative_reportable": True,
                         "last_query": "x" + " word" * 60}))

    title = (out.get("artifact_payload") or {}).get("title") or ""
    assert len(title) <= 124, title
    assert title.endswith("..."), title
    assert not title[:-3].endswith(" "), f"trailing space before ellipsis: {title!r}"


def test_the_title_falls_back_to_the_question_when_there_is_no_purpose(monkeypatch):
    """The fallback still has to work — this is not a removal."""
    tools = _tools(monkeypatch, REAL_REPORT)
    out = tools.render_artifact(_state(
        final_response=REAL_REPORT,
        working_context={"last_narrative_reportable": True,
                         "last_query": "how many cameras are registered?"}))

    payload = out.get("artifact_payload") or {}
    assert payload.get("title") == "how many cameras are registered?"


# ------------------------------------------- what the recorder must record

def test_a_failed_turn_is_recorded_as_not_reportable():
    """Read the recorder: the flag is what the renderer trusts.

    Asserted on the source because building a real agent here would need the
    database and the graph, and the property is simply that the three inputs
    are consulted.
    """
    import ast
    import inspect
    import textwrap

    import sql_agent.agent as module

    source = inspect.getsource(module.SQLIntelligenceAgent._record_working_context)
    tree = ast.parse(textwrap.dedent(source))
    names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
    constants = {node.value for node in ast.walk(tree)
                 if isinstance(node, ast.Constant) and isinstance(node.value, str)}

    assert "last_narrative_reportable" in constants, (
        "the turn recorder no longer records whether its narrative is a report")
    assert "observation" in constants or "observation" in names, (
        "reportability is not being derived from the Observation")

# ------------------------------------------- what the narration may claim

def test_a_turn_with_no_document_says_so(monkeypatch):
    """THE live failure: "thanks" answered "I've prepared ... as a PDF".

    No artifact existed. The FACTS block spoke only about DATA — "no data was
    created, modified or deleted" — so whether a document had been produced
    was left open, and the model answered it from the transcript.
    """
    tools = _tools(monkeypatch, REAL_REPORT)
    facts = tools._grounding_section(_state())

    assert "NO document was produced" in facts
    assert "do not offer a download" in facts.lower()


def test_a_turn_that_did_produce_one_says_that_instead(monkeypatch):
    """The negative control: it must not always deny having a document."""
    tools = _tools(monkeypatch, REAL_REPORT)
    facts = tools._grounding_section(_state(
        artifact_payload={"bytes": b"%PDF", "type": "pdf"}))

    assert "A document WAS produced" in facts
    assert "NO document was produced" not in facts

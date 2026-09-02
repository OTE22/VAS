"""A streaming turn owes the same things however it finishes.

`query_stream` can complete in two ways: from the streamed response, or from
the invoke fallback when the stream carried none. Each used to do its own
bookkeeping, and they had drifted — the fallback recorded the reply and
nothing else.

The cost was measured, not guessed. Across three runs of the SSE acceptance
test, every "make that a PDF" chose `generate_document` correctly and two of
the three runs still produced no document. `has_document` is the only signal
`routes.py` uses before persisting and emitting an artifact, and the fallback
never set it — so a rendered PDF was thrown away and the browser was never
told it existed. Working context and the dialogue-state deltas went with it,
so the next turn could not say "it".

Which path a turn takes depends on whether the stream happened to carry
`final_response`, which is why the failure was intermittent and why it showed
on SSE only: REST does not use this code at all.

Pinned here so the two cannot drift apart again.

    docker exec face_recognition_api python -m pytest tests/test_stream_completion_parity.py -v
"""

import ast
import inspect
import textwrap

import pytest


def _agent_with(monkeypatch, stream_updates, invoke_result=None):
    """A real SQLAgent with the graph and memory replaced by fakes."""
    import sql_agent.agent as module

    class _Memory:
        user_id = 1

        def __init__(self):
            self.ai_messages = []

        def add_ai_message(self, text):
            self.ai_messages.append(text)

        def add_user_message(self, text):
            pass

        def get_recent_messages(self, limit=8):
            return []

        def get_working_context(self, reload=False):
            return {}

        def get_conversation_context(self, limit=6):
            return ""

    class _Graph:
        def stream(self, state, **kwargs):
            return iter(stream_updates)

        def invoke(self, state):
            return invoke_result or {}

    agent = module.SQLIntelligenceAgent.__new__(module.SQLIntelligenceAgent)
    agent.conversation_memory = _Memory()
    agent.agent = _Graph()
    # The instance attributes __init__ sets, minus the heavy collaborators.
    # Listed explicitly rather than discovered one AttributeError at a time.
    agent._pending_document = {}
    agent._durable_memory = ""
    agent._artifact_index = []
    agent._artifact_sql_index = {}
    agent.db = None
    agent.kb = None
    return agent


DOCUMENT_STATE = {
    "final_response": "Here is your report.",
    "artifact_payload": {"bytes": b"%PDF-1.4 fake", "type": "pdf"},
}

TRANSLATION_STATE = {
    "final_response": "Preparing the translated report.",
    "translation_request": {
        "artifact_id": "11111111-1111-4111-8111-111111111111",
        "language": "ar",
        "format": "pdf",
    },
}


# ------------------------------------------------------ the shared path

def test_both_completion_paths_call_one_helper():
    """Read the code, not the behaviour: two hand-kept paths WILL drift.

    They already did — which is the whole reason this file exists. The
    property worth pinning is that there is one place that knows what a
    finished turn owes.
    """
    import sql_agent.agent as module

    source = inspect.getsource(module.SQLIntelligenceAgent.query_stream)
    tree = ast.parse(textwrap.dedent(source))

    finishes = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "_finish_turn"
    ]
    assert len(finishes) >= 2, (
        f"only {len(finishes)} completion path(s) call _finish_turn; the "
        "other is doing its own bookkeeping again")

    # And none of them should be re-implementing the pieces inline.
    for name in ("_record_working_context", "_commit_tool_result_deltas",
                 "_stash_pending_document"):
        inline = [
            node for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == name
        ]
        assert not inline, (
            f"query_stream calls {name} directly; it belongs to _finish_turn")


def test_finish_turn_does_every_piece_of_the_bookkeeping():
    """The helper is only worth having if it is complete."""
    import sql_agent.agent as module

    tree = ast.parse(textwrap.dedent(
        inspect.getsource(module.SQLIntelligenceAgent._finish_turn)))
    called = {node.func.attr for node in ast.walk(tree)
              if isinstance(node, ast.Call)
              and isinstance(node.func, ast.Attribute)}

    for required in ("add_ai_message", "_record_working_context",
                     "_commit_tool_result_deltas", "_stash_pending_document"):
        assert required in called, f"_finish_turn does not call {required}"


# ------------------------------------------------------ observed behaviour

def _drain(agent, user_input="make that a PDF"):
    return list(agent.query_stream(user_input))


def test_the_streamed_path_reports_its_document(monkeypatch):
    """The control: the path that always worked must keep working."""
    agent = _agent_with(monkeypatch, [
        {"story_response": DOCUMENT_STATE},
    ])
    events = _drain(agent)
    complete = [e for e in events if e.get("type") == "complete"]

    assert complete, "no completion event"
    assert complete[-1].get("has_document") is True
    assert agent._pending_document.get("artifact_payload")


def test_the_invoke_fallback_also_reports_its_document(monkeypatch):
    """THE regression. Measured: two of three SSE runs lost the PDF here.

    The stream carries no final_response, so the fallback runs. It rendered a
    document and then dropped it, because `has_document` — the only flag the
    SSE route checks before persisting an artifact — was never set.
    """
    agent = _agent_with(monkeypatch,
                        stream_updates=[{"check_schema": {}}],
                        invoke_result=DOCUMENT_STATE)
    events = _drain(agent)
    complete = [e for e in events if e.get("type") == "complete"]

    assert complete, "no completion event"
    assert complete[-1].get("has_document") is True, (
        "the fallback finished a turn without telling the route there is a "
        "document — the artifact is silently dropped")
    assert agent._pending_document.get("artifact_payload"), (
        "the payload was never stashed for the API layer")


def test_the_invoke_fallback_returns_the_response_text(monkeypatch):
    """It also omitted `response`, so the route had to reconstruct it."""
    agent = _agent_with(monkeypatch,
                        stream_updates=[{"check_schema": {}}],
                        invoke_result=DOCUMENT_STATE)
    complete = [e for e in _drain(agent) if e.get("type") == "complete"]

    assert complete[-1].get("response") == "Here is your report."


def test_a_turn_with_no_document_reports_none(monkeypatch):
    """The negative control: has_document must not become always-true."""
    agent = _agent_with(monkeypatch, [
        {"story_response": {"final_response": "There are 16 cameras."}},
    ])
    complete = [e for e in _drain(agent) if e.get("type") == "complete"]

    assert complete[-1].get("has_document") is False
    assert not agent._pending_document


def test_translation_progress_is_never_streamed_as_the_final_answer(monkeypatch):
    """The live failure appeared while the real translation was still running."""
    agent = _agent_with(monkeypatch, [
        {"translate_artifact": TRANSLATION_STATE},
    ])

    events = _drain(agent, "can you make teh report in arabic")
    streamed = "".join(
        event.get("content", "") for event in events
        if event.get("type") == "content")
    statuses = [event.get("message") for event in events
                if event.get("type") == "status"]
    complete = [event for event in events if event.get("type") == "complete"]

    assert streamed == "", "provisional document text reached the browser"
    assert "Translating report..." in statuses
    assert complete[-1].get("has_document") is True
    assert agent._pending_document.get("translation_request")

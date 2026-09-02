"""Every LLM node must actually CONSUME the context it is given.

A memory object that is populated but never reaches a prompt is equivalent to
having no memory — and that exact gap has shipped twice here: durable memory
was written on every turn and read by nothing, and modify_sql interpreted its
delta with no conversation context at all. Existence-in-Python proves
nothing; only the final prompt does.

Method: the node's LLM is replaced with a recorder that captures the fully
rendered prompt and returns a canned, well-formed reply. A distinctive marker
is planted in the state; the assertion is that the marker appears in what the
model would have SEEN.

No real model, no database, no files — every test is deterministic.

    docker exec face_recognition_api python -m pytest tests/test_prompt_consumption.py -v
"""

import json

import pytest
from langchain_core.runnables import RunnableLambda

import sql_agent.tools.agent_tools as tools_module
from sql_agent.tools.agent_tools import SQLAgentTools

MARKER = "CTX-MARKER-7f3a9"
DURABLE_MARKER = "[durable memory - things this user told you earlier"


def _recorder(reply: str):
    """A Runnable that records the rendered prompt and answers `reply`."""
    captured = []

    def _run(prompt_value):
        captured.append(prompt_value.to_string())
        return reply

    return RunnableLambda(_run), captured


class _FakeDb:
    def _validate_query(self, _sql):
        return {"is_safe": True, "reason": ""}


@pytest.fixture
def tools(monkeypatch):
    """SQLAgentTools with every heavy dependency stubbed out."""
    monkeypatch.setattr(tools_module, "create_llm", lambda *_a, **_k: None)
    monkeypatch.setattr(tools_module, "create_sql_llm", lambda *_a, **_k: None)
    monkeypatch.setattr(tools_module, "DatabaseManager", lambda *_a, **_k: _FakeDb())
    monkeypatch.setattr(tools_module, "SQLKnowledgeBase", lambda *_a, **_k: None)
    return SQLAgentTools(conversation_memory=None)


def _base_state(**extra):
    state = {
        "original_input": "same report but only camera 3",
        "normalized_input": "same report but only camera 3",
        "conversation_context": (
            f"\n[prior turns - internal context]\nUser asked about {MARKER}\n"
            f"[end of prior turns]\n"
            f"\n{DURABLE_MARKER}. Use them only if relevant]\n- prefers: {MARKER}\n"
            f"[end of durable memory]\n"),
        "schema_description": "detections(id, camera_id, person)",
        "response_language": "en",
        "working_context": {},
        "artifact_index": [],
        "artifact_sql_index": {},
        "planned_action": None,
    }
    state.update(extra)
    return state


def test_plan_action_receives_prior_turns_and_durable_memory(tools):
    llm, captured = _recorder(json.dumps({
        "action": "chat", "confidence": 0.9, "target": None, "artifact_id": None,
        "language": None, "format": None, "modification": None,
        "clarify_question": None}))
    tools.llm = llm
    tools.plan_action(_base_state())
    assert captured, "plan_action never called its model"
    assert MARKER in captured[0], "conversation context never reached the planner prompt"
    assert DURABLE_MARKER in captured[0], "durable memory never reached the planner prompt"




def test_generate_sql_receives_prior_turns(tools):
    llm, captured = _recorder(json.dumps(
        {"sql": "SELECT 1", "purpose": "probe"}))
    tools.sql_llm = llm
    tools.generate_sql(_base_state(rag_context=""))
    assert captured and MARKER in captured[0], (
        "conversation context never reached the SQL-generation prompt")


def test_modify_sql_receives_prior_turns(tools, monkeypatch):
    """THE regression this file exists for.

    modify_sql shipped without any conversation context: the base SQL came
    from provenance (correct), but the DELTA was interpreted blind — "only
    the person we discussed" could not resolve. Found by the C1 audit, not by
    a user, which is the point of asserting consumption mechanically.
    """
    llm, captured = _recorder(json.dumps(
        {"sql": "SELECT 1 WHERE camera_id = 3", "purpose": "probe"}))
    monkeypatch.setattr(tools_module, "create_sql_llm", lambda *_a, **_k: llm)
    state = _base_state(
        planned_action={"action": "modify_previous_query",
                        "modification": "only camera 3", "artifact_id": None},
        working_context={"last_result": {"sql": "SELECT count(*) FROM detections",
                                         "purpose": "count"}},
    )
    tools.modify_sql(state)
    assert captured, "modify_sql never called its model"
    assert MARKER in captured[0], (
        "conversation context never reached the modification prompt — "
        "references inside the delta cannot resolve")
    assert "SELECT count(*) FROM detections" in captured[0], (
        "the base query is missing from the modification prompt")


def test_handle_chat_receives_prior_turns_and_language(tools):
    llm, captured = _recorder("hello there")
    tools.llm = llm
    tools.handle_chat(_base_state(response_language="en", clarify_question=None))
    assert captured and MARKER in captured[0], (
        "conversation context never reached the chat prompt")
    assert "OUTPUT LANGUAGE" in captured[0], (
        "the language directive never reached the chat prompt")


def test_story_response_receives_prior_turns(tools):
    llm, captured = _recorder("**REPORT**\n\nAll quiet.")
    tools.llm = llm
    state = _base_state(
        generated_sql="SELECT count(*) FROM detections",
        sql_purpose="count detections",
        query_result={"success": True, "row_count": 1,
                      "rows": [{"count": 4}], "columns": ["count"]},
        streaming_callback=None,
        co_appearances=None,
    )
    tools.generate_story_response(state)
    assert captured, "story_response never called its model"
    assert MARKER in captured[0], (
        "conversation context never reached the narrative prompt")


def test_a_node_stripped_of_context_fails_these_assertions(tools):
    """Negative control: the marker check genuinely detects a blind prompt."""
    llm, captured = _recorder('{"intent": "CHAT", "confidence": 0.9}')
    tools.llm = llm
    state = _base_state()
    state["conversation_context"] = ""      # a node with no context wired in
    # Driven through handle_chat since `classify_intent` was deleted — the
    # tool loop, the planner and the observation now cover what it decided,
    # and it ran LAST so it overrode better-informed stages.
    tools.handle_chat(state)
    assert captured and MARKER not in captured[0]

"""The subject of THIS turn, and why prose cannot be trusted to hold it.

Reported: "Track Joey", later "track ali", and the agent was still working on
Joey. The cause is structural, not a prompt problem:

  * `active_task` is free prose written by the SQL model ("Track all
    detections of a person named Joey...") and is committed ONLY when a query
    succeeds ([agent.py] `if result.get("success") and state["sql_purpose"]`).
    A turn that ends in a question writes nothing, and nothing else ever
    clears it.
  * `referenced_entity` — the field that exists precisely to hold the
    subject — had NO write site anywhere in production code.

So the subject lived only inside a sentence, and that sentence was re-injected
into the next prompt under a header reading "authoritative".

The rule this file pins: the structured `referenced_entity` is the subject;
`active_task` is a readable summary that may never override it.

    docker exec face_recognition_api python -m pytest tests/test_current_subject.py -v
"""

import pytest

from sql_agent import dialogue_state as ds


class _Memory:
    """Just enough conversation memory to exercise the commit seam."""

    user_id = 1
    current_session_id = "s1"

    def __init__(self, state=None):
        self.working_context = {"dialogue_state": state or ds.empty_state()}

    def get_working_context(self):
        return dict(self.working_context)

    def update_working_context(self, **kwargs):
        self.working_context.update(kwargs)

    def add_ai_message(self, *a, **k):
        pass


def _agent(state=None):
    """A bare SQLIntelligenceAgent with nothing constructed but the seam."""
    from sql_agent.agent import SQLIntelligenceAgent

    agent = SQLIntelligenceAgent.__new__(SQLIntelligenceAgent)
    agent.conversation_memory = _Memory(state)
    return agent


def _with_subject(name, identity_id="id-joey"):
    """A committed state whose subject is already `name`."""
    state = ds.empty_state()
    state = ds.apply_delta(state, {
        "operation": "REPLACE", "field": "referenced_entity",
        "proposed_value": [name], "source": "user_correction"}, turn_id="t0")
    return ds.apply_delta(state, {
        "operation": "REPLACE", "field": "active_task",
        "proposed_value": f"Track all detections of a person named {name}",
        "source": "tool_result"}, turn_id="t0")


def _turn(**extra):
    state = {"normalized_input": "track ali", "response_language": "en",
             "query_result": {}, "planned_action": {}, "sql_purpose": "",
             "resolved_entities": [], "query_history_id": None}
    state.update(extra)
    return state


def _resolved(raw_text, canonical, identity_id="id-ali"):
    return [{"tool": "resolve_person", "raw_text": raw_text,
             "identity_id": identity_id, "canonical_name": canonical}]


# ------------------------------------------------ the subject is structured

def test_a_resolved_person_becomes_the_structured_subject():
    """`referenced_entity` had no writer at all before this."""
    agent = _agent()
    agent._commit_tool_result_deltas(
        "track ali", _turn(resolved_entities=_resolved("ali", "ali abbass")))

    committed = agent.conversation_memory.working_context["dialogue_state"]
    assert ds.get_value(committed, "referenced_entity") == ["ali abbass"]


def test_the_subject_is_recorded_even_when_no_query_ran():
    """THE stale-subject bug.

    A turn that ends in a clarifying question runs no SQL, so the old commit
    — gated on `result.get("success")` — wrote nothing and the previous
    subject survived into the next prompt.
    """
    agent = _agent(_with_subject("Joey"))
    agent._commit_tool_result_deltas(
        "track ali",
        _turn(query_result={},                       # nothing executed
              resolved_entities=_resolved("ali", "ali abbass")))

    committed = agent.conversation_memory.working_context["dialogue_state"]
    assert ds.get_value(committed, "referenced_entity") == ["ali abbass"]


def test_a_new_subject_replaces_the_old_one():
    agent = _agent(_with_subject("Joey"))
    agent._commit_tool_result_deltas(
        "track ali", _turn(resolved_entities=_resolved("ali", "ali abbass")))

    committed = agent.conversation_memory.working_context["dialogue_state"]
    assert ds.get_value(committed, "referenced_entity") == ["ali abbass"]
    assert "Joey" not in str(ds.get_value(committed, "referenced_entity"))


def test_a_new_subject_clears_the_previous_task_prose():
    """The sentence describes the OLD job and would otherwise be re-injected.

    'Track all detections of a person named Joey' must not survive into a
    turn about Ali just because it is stored in a different field.
    """
    agent = _agent(_with_subject("Joey"))
    agent._commit_tool_result_deltas(
        "track ali", _turn(resolved_entities=_resolved("ali", "ali abbass")))

    committed = agent.conversation_memory.working_context["dialogue_state"]
    assert "Joey" not in str(ds.get_value(committed, "active_task") or "")


def test_an_old_subject_pinned_by_precedence_can_still_be_replaced():
    """`_outranks` lets a `user_correction` value outrank `tool_result`.

    A subject committed at that rank could never be overwritten by a later
    turn, pinning the conversation to one person for the rest of the session.
    The current turn's explicit subject carries the same rank, and equal rank
    wins, so the newer statement replaces it without loosening the ordering
    for anything else.
    """
    agent = _agent(_with_subject("Joey"))
    agent._commit_tool_result_deltas(
        "track ali", _turn(resolved_entities=_resolved("ali", "ali abbass")))

    committed = agent.conversation_memory.working_context["dialogue_state"]
    assert ds.get_value(committed, "referenced_entity") == ["ali abbass"]


# --------------------------------------------------------- negative controls

def test_a_turn_with_no_subject_keeps_the_previous_one():
    """THE control for coreference.

    "Which camera saw him most?" resolves nobody, and must NOT wipe the
    subject — that is the case where memory is exactly right.
    """
    agent = _agent(_with_subject("Iron Man"))
    agent._commit_tool_result_deltas(
        "which camera saw him most?", _turn(resolved_entities=[]))

    committed = agent.conversation_memory.working_context["dialogue_state"]
    assert ds.get_value(committed, "referenced_entity") == ["Iron Man"]


def test_repeating_the_same_subject_does_not_clear_the_task():
    """Asking about the same person again is a refinement, not a new job."""
    agent = _agent(_with_subject("Iron Man"))
    agent._commit_tool_result_deltas(
        "track iron man",
        _turn(resolved_entities=_resolved("iron man", "Iron Man", "id-im")))

    committed = agent.conversation_memory.working_context["dialogue_state"]
    assert ds.get_value(committed, "active_task")


def test_committing_a_subject_never_breaks_the_turn():
    """Losing a state transition must not fail the turn that earned it."""
    agent = _agent()
    agent.conversation_memory.get_working_context = lambda: 1 / 0   # explode

    agent._commit_tool_result_deltas(
        "track ali", _turn(resolved_entities=_resolved("ali", "ali abbass")))


# ------------------------------------------- what the model is actually told

def test_the_prompt_names_the_structured_subject_as_authoritative():
    """`active_task` is prose and may lag; it must not read as the subject.

    The block is headed "authoritative" and is never truncated, so whatever
    it says about the subject is what the model will act on.
    """
    from sql_agent.tools import planner

    block = planner.build_planner_context(
        {"dialogue_state": _with_subject("Joey")}, "track ali")

    assert "referenced_entity" in block
    lines = [ln for ln in block.splitlines() if "active_task" in ln]
    assert lines, "active_task disappeared from the context"
    assert any("summary" in ln.lower() or "may be stale" in ln.lower()
               for ln in lines), (
        "active_task is still presented as authoritative fact: " + str(lines))

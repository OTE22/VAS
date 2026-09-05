"""When the agent asks something, the reply must go somewhere.

From a real transcript:

    user: track iron man
    bot:  Are you sure you want to track Iron Man?
    user: yes
    bot:  I'm ready to assist you. What's on your mind?

The trace shows the model TRYING to continue:

    proposed=query_database
    refused query_database: the user did not ask for it
    committed to answer_directly

Two gaps, both mine.

`pending_clarification` is stored only when the question came from an
ambiguous `resolve_person` with candidates. A question the model asks for any
other reason stores nothing, so the reply has nothing to attach to.

And the intent-fit check judges the message ALONE. "yes" asks for nothing on
its own - true, and irrelevant, because it is answering a question the agent
asked one turn earlier. Judged without that fact, every answer to every
question the agent asks looks like small talk.

Asking a question and then discarding the reply is worse than never asking.

    docker exec face_recognition_api python -m pytest tests/test_answering_the_agents_question.py -v
"""

import pytest

from sql_agent import dialogue_state as ds


class _Memory:
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
    from sql_agent.agent import SQLIntelligenceAgent

    agent = SQLIntelligenceAgent.__new__(SQLIntelligenceAgent)
    agent.conversation_memory = _Memory(state)
    return agent


def _clarify_turn(**extra):
    state = {"normalized_input": "track iron man", "response_language": "en",
             "query_result": {}, "sql_purpose": "", "resolved_entities": [],
             "query_history_id": None, "intent": "SQL_QUERY",
             "planned_action": {"action": "clarify"},
             "clarification_candidates": []}
    state.update(extra)
    return state


# ------------------------------------------------------- the question sticks

def test_any_question_the_agent_asks_is_remembered():
    """THE gap: only person-candidate questions used to be stored.

    A question asked for any other reason left no trace, so the next turn
    could not tell an ANSWER from a new request.
    """
    agent = _agent()
    agent._commit_tool_result_deltas("track iron man", _clarify_turn())

    held = ds.get_value(
        agent.conversation_memory.working_context["dialogue_state"],
        "pending_clarification")

    assert held, "the agent asked a question and forgot it immediately"
    assert held["original_query"] == "track iron man"


def test_the_candidate_case_still_stores_its_candidates():
    """The control: generalising must not lose what already worked."""
    agent = _agent()
    agent._commit_tool_result_deltas("track ali", _clarify_turn(
        normalized_input="track ali",
        clarification_candidates=[{"identity_id": "1",
                                   "display_name": "Ali Abbass"}]))

    held = ds.get_value(
        agent.conversation_memory.working_context["dialogue_state"],
        "pending_clarification")

    assert [c["display_name"] for c in held["candidates"]] == ["Ali Abbass"]


def test_a_turn_that_asked_nothing_stores_nothing():
    """THE negative control.

    Storing a question on every turn would make every following message look
    like an answer.
    """
    agent = _agent()
    agent._commit_tool_result_deltas("track joey", _clarify_turn(
        planned_action={"action": "query_database"},
        query_result={"success": True, "rows": [{"n": 1}], "row_count": 1}))

    assert ds.get_value(
        agent.conversation_memory.working_context["dialogue_state"],
        "pending_clarification") is None


# ------------------------------------------- the answer counts as a request



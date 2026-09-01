"""A question the agent asked, and the answer that arrives next turn.

Before this, clarification state was per-turn only: `clarify_question` lives
in the LangGraph state dict for ONE invocation and was never persisted. So
after "Which Ali do you mean?" the next message arrived with no record that a
question was outstanding — "Ali Abbass" looked like an unanchored fragment,
and the only surviving context was the stale task prose describing somebody
else.

What is stored is bounded and operational: the question's type, the intent and
query it interrupted, and the candidate list it offered. No reasoning text.

An answer is matched against the STORED candidates — by name or by position —
rather than by re-searching, so "the second one" means the second thing the
user was actually shown.

    docker exec face_recognition_api python -m pytest tests/test_pending_clarification.py -v
"""

import pytest

from sql_agent import dialogue_state as ds


CANDIDATES = [{"identity_id": "id-1", "display_name": "Ali Abbass"},
              {"identity_id": "id-2", "display_name": "Ali Hassan"}]


def _pending(state=None):
    """State holding an unanswered person-resolution question."""
    return ds.apply_delta(state or ds.empty_state(), {
        "operation": "REPLACE", "field": "pending_clarification",
        "proposed_value": {"type": "person_resolution",
                           "original_intent": "track_person",
                           "original_query": "track ali",
                           "field": "person",
                           "candidates": CANDIDATES},
        "source": "tool_result"}, turn_id="t1")


# ------------------------------------------------------------ it persists

def test_a_clarification_can_be_stored_at_all():
    """The field did not exist anywhere in the repository before this."""
    state = _pending()
    held = ds.get_value(state, "pending_clarification")

    assert held["type"] == "person_resolution"
    assert held["original_query"] == "track ali"
    assert [c["display_name"] for c in held["candidates"]] == [
        "Ali Abbass", "Ali Hassan"]


def test_it_needs_no_migration():
    """Dialogue state is a JSON document; a new field is not a schema change."""
    assert "pending_clarification" in ds._FIELD_KINDS


# --------------------------------------------------- answering by name

@pytest.mark.parametrize("answer,expected", [
    ("Ali Abbass", "Ali Abbass"),
    ("ali abbass", "Ali Abbass"),          # case
    ("  Ali   Abbass ", "Ali Abbass"),     # whitespace
    ("Ali Hassan", "Ali Hassan"),
])
def test_an_answer_naming_a_candidate_selects_it(answer, expected):
    chosen = ds.match_candidate(_pending(), answer)
    assert chosen and chosen["display_name"] == expected


# ------------------------------------------------ answering by position

@pytest.mark.parametrize("answer,expected", [
    ("the second one", "Ali Hassan"),
    ("second", "Ali Hassan"),
    ("number 2", "Ali Hassan"),
    ("2", "Ali Hassan"),
    ("#1", "Ali Abbass"),
    ("the first", "Ali Abbass"),
])
def test_an_answer_giving_a_position_selects_that_candidate(answer, expected):
    """Positions refer to the list the user was SHOWN.

    Resolved against the stored candidates rather than by searching again, so
    "the second one" cannot quietly mean something the user never saw.
    """
    chosen = ds.match_candidate(_pending(), answer)
    assert chosen and chosen["display_name"] == expected


def test_a_position_outside_the_list_selects_nothing():
    """The negative control for ordinals: no silent wrap-around or clamp."""
    assert ds.match_candidate(_pending(), "number 9") is None


# ------------------------------------------------------ negative controls

@pytest.mark.parametrize("answer", [
    "how many cameras are online?",
    "never mind",
    "track iron man",
    "make that a PDF",
])
def test_an_unrelated_message_does_not_answer_the_question(answer):
    """THE control.

    Reading any next message as the answer would turn a new request into a
    person selection. These must all fall through to normal handling so the
    turn can cancel the clarification instead.
    """
    assert ds.match_candidate(_pending(), answer) is None


def test_nothing_is_matched_when_no_question_is_outstanding():
    assert ds.match_candidate(ds.empty_state(), "Ali Abbass") is None


def test_a_candidate_named_by_a_number_is_not_confused_with_a_position():
    """A stored name wins over reading the same text as an index."""
    state = ds.apply_delta(ds.empty_state(), {
        "operation": "REPLACE", "field": "pending_clarification",
        "proposed_value": {"type": "person_resolution", "field": "person",
                           "original_query": "track 2",
                           "candidates": [{"identity_id": "a",
                                           "display_name": "2"},
                                          {"identity_id": "b",
                                           "display_name": "Ali"}]},
        "source": "tool_result"}, turn_id="t1")

    chosen = ds.match_candidate(state, "2")
    assert chosen["identity_id"] == "a"


# -------------------------------------------------------------- clearing

def test_a_clarification_can_be_cleared():
    """Cleared on resolution, a new subject, a new intent, or a cancellation.

    REMOVE carries no precedence check, so a stored question can always be
    retired — it must never be able to pin the conversation.
    """
    state = ds.apply_delta(_pending(), {
        "operation": "REMOVE", "field": "pending_clarification",
        "source": "user_correction"}, turn_id="t2")

    assert ds.get_value(state, "pending_clarification") is None


def test_the_stored_candidate_list_is_bounded():
    """It rides in every prompt afterwards, so it cannot be unbounded."""
    state = _pending()
    held = ds.get_value(state, "pending_clarification")
    assert len(held["candidates"]) <= ds._MAX_LIST_VALUES

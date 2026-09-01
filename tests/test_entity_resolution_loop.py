"""Resolving a person AFTER the query comes back empty.

Telling the model "look people up first" did not work: across a full live run
`resolve_person` was never called once, and the clause that pushed hardest
made things worse - "Track Iron Man" came back as Marvel trivia rather than a
query. A prompt cannot be relied on for a step that must always happen.

So the trigger is structural instead. Zero rows is the signal, and the SQL
that produced them says what was filtered on: a name literal compared against
a name column, read off the parsed statement rather than guessed at from the
user's words. No phrasebook, and nothing to keep in step with how people
phrase things.

    empty result + a name in the SQL
        -> resolve that name (Python, no model call)
             resolved, and the filter WOULD have matched  -> answer honestly:
                 the person exists, there are no detections
             resolved, and the filter would NOT have      -> re-query using the
                 (a misspelling)                              stored spelling
             several people match                         -> ask which
             nobody matches                               -> say so

Bounded: it happens at most once per turn, and only after a query has already
run.

    docker exec face_recognition_api python -m pytest tests/test_entity_resolution_loop.py -v
"""

import pytest

from sql_agent import reasoning


# ------------------------------------------ reading the filter off the SQL

@pytest.mark.parametrize("sql,expected", [
    ("SELECT * FROM faces WHERE name ILIKE '%ali%'", ["ali"]),
    ("SELECT f.name FROM faces f WHERE f.name = 'ali abbass'", ["ali abbass"]),
    ("SELECT * FROM faces WHERE name ILIKE '%عوي%'", ["عوي"]),
])
def test_a_name_filter_is_read_from_the_parsed_sql(sql, expected):
    """The query itself records what it looked for - no guessing required."""
    assert reasoning.filtered_names(sql) == expected


@pytest.mark.parametrize("sql", [
    "SELECT COUNT(*) FROM faces",
    "SELECT * FROM detections WHERE pipeline_id = 'cam-3'",
    "SELECT * FROM faces WHERE created_at > '2026-01-01'",
])
def test_a_query_about_no_particular_person_yields_no_name(sql):
    """THE control.

    "How many detections yesterday?" answered with 0 is CORRECT. Treating
    every empty result as a lookup failure would turn right answers into
    pointless work.
    """
    assert reasoning.filtered_names(sql) == []


def test_unparseable_sql_yields_nothing_rather_than_raising():
    assert reasoning.filtered_names("this is not sql at all") == []
    assert reasoning.filtered_names("") == []


# --------------------------------------------------------- the decision

def _empty_turn(**extra):
    state = {
        "planned_action": {"action": "query_database"},
        "sql_validation_status": "VALID",
        "generated_sql": "SELECT * FROM faces WHERE name ILIKE '%ali%'",
        "query_result": {"success": True, "rows": [], "row_count": 0},
        "working_context": {}, "normalized_input": "track ali",
    }
    state.update(extra)
    return state


def test_an_empty_result_naming_a_person_triggers_resolution():
    """THE fix. Before this, zero rows for a named person just got narrated."""
    observation = reasoning.build_observation(_empty_turn())
    verdict = reasoning.decide_next(observation)

    assert observation["unresolved_entity"] == "ali"
    assert verdict["decision"] == reasoning.RESOLVE_ENTITY


def test_an_empty_result_about_nobody_is_simply_the_answer():
    """The control again, at the decision layer."""
    observation = reasoning.build_observation(_empty_turn(
        generated_sql="SELECT COUNT(*) FROM detections WHERE ts > '2026-01-01'"))
    verdict = reasoning.decide_next(observation)

    assert observation["success"], "zero was treated as a failure"
    assert verdict["decision"] == reasoning.ANSWER


def test_resolution_is_attempted_only_once_per_turn():
    """Bounded, or an empty re-query resolves the same name forever."""
    observation = reasoning.build_observation(
        _empty_turn(entity_resolution_attempted=True))
    verdict = reasoning.decide_next(observation)

    assert verdict["decision"] != reasoning.RESOLVE_ENTITY


def test_resolution_never_precedes_a_query():
    """It is a reaction to evidence, not a gate in front of the work."""
    observation = reasoning.build_observation({
        "planned_action": {"action": "chat"}, "working_context": {},
        "query_result": None})
    verdict = reasoning.decide_next(observation)

    assert verdict["decision"] != reasoning.RESOLVE_ENTITY


# ------------------------------------------------- would the filter have hit?

@pytest.mark.parametrize("asked,stored,rerun", [
    ("ali", "ali abbass", False),    # ILIKE '%ali%' already covers it
    ("Ali", "ali abbass", False),    # case only
    ("Jeoy", "JOEY", True),          # a real misspelling: re-query
    ("iron", "IRON MAN", False),
    ("على", "علي", True),
])
def test_a_re_query_happens_only_when_it_could_change_the_answer(
        asked, stored, rerun):
    """Re-running a query whose filter already covered the person is waste.

    "ali" against a stored "ali abbass" would have matched if any row
    existed, so zero rows means the person has no detections - a fact, and
    the honest answer. "Jeoy" against "JOEY" would NOT have matched, so the
    query is worth running again.
    """
    assert reasoning.would_rerun_help(asked, stored) is rerun

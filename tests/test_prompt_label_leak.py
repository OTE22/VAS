"""A prompt heading must never be readable as a value, and a name the user
never typed must never be handed back to them.

Opik 01a08276-d04c (2026-09-08): "Help me find a person" names nobody, so the
only capitalised token in front of the SQL specialist was the heading of the
next block, "PLANNER PARAPHRASE". It answered `WHERE f.name = 'PLANNER'` and
the turn replied "No person named PLANNER is enrolled, so there is nothing to
track" - about a name the user never used.
"""
import inspect

from sql_agent.tools.agent_tools import SQLAgentTools as T


def test_no_prompt_heading_reads_as_a_proper_noun():
    source = inspect.getsource(T.generate_sql)
    assert "PLANNER PARAPHRASE" not in source
    assert "How the assistant read that request" in source


def test_the_paraphrase_block_is_dropped_when_it_only_repeats_the_request():
    # Building the whole prompt needs the graph; the condition is what matters.
    source = inspect.getsource(T.generate_sql)
    assert 'sql_generation_input' in source
    assert 'normalized_input' in source
    # the equality guard that suppresses a redundant block
    assert "!= \" \".join(str(state.get(\"normalized_input\") or \"\").split())" in source


def test_user_named_is_a_fact_about_the_message():
    assert T._user_named({"normalized_input": "how many times was IRON MAN detected"}, "IRON MAN")
    assert not T._user_named({"normalized_input": "Help me find a person"}, "PLANNER")
    assert not T._user_named({"normalized_input": "Help me find a person"}, "IRON MAN")
    assert T._user_named({"original_input": "track joey"}, "JOEY"), "either field carries the message"
    assert not T._user_named({"normalized_input": ""}, "PLANNER")


def _narrate(state):
    return T.__new__(T)._empty_narration(state)


def test_a_name_the_user_never_typed_becomes_a_question_not_an_answer():
    invented = _narrate({"normalized_input": "Help me find a person",
                         "entity_not_found": "PLANNER",
                         "response_language": "en"})
    assert "PLANNER" not in invented, "never echo a name the user did not use"
    assert "Who would you like me to find" in invented


def test_a_name_the_user_did_type_is_still_named_back():
    asked = _narrate({"normalized_input": "track Ali Abbass",
                      "entity_not_found": "Ali Abbass",
                      "response_language": "en"})
    assert "Ali Abbass" in asked and "is enrolled" in asked


def test_the_arabic_wording_is_a_question_too():
    invented = _narrate({"normalized_input": "ساعدني في العثور على شخص",
                         "entity_not_found": "PLANNER",
                         "response_language": "ar"})
    assert "PLANNER" not in invented
    assert "من الشخص" in invented

"""A subject comes from the message or the conversation, never from the list
of enrolled people the reader was shown.

Reader benchmark, 2026-09-09: asked "Help me find a person", which names
nobody, the 11B reader returned twelve people - IRON MAN, JOEY and every
seed_person_* in the ENROLLED block. The turn then offered those test
identities back as clarification candidates.
"""
from sql_agent.tools import interpreter as I

NAMES = ["IRON MAN", "JOEY"] + [f"seed_person_{i:03d}" for i in range(0, 40, 4)]
CAMERAS = ["WEZARET DEFA3", "MAD5AL AMEN"]


def _read(parsed, message, recent=None):
    return I.validate(parsed, names=NAMES, cameras=CAMERAS, user_text=message,
                      has_result=True, has_documents=False, recent_turns=recent or [])


def _reading(people, **over):
    base = {"wants": "data", "confidence": 0.9, "question": "q",
            "people": people, "shape": "answer", "about_previous": True}
    base.update(over)
    return base


def test_the_enrolled_list_handed_back_is_not_a_reading():
    read = _read(_reading(NAMES), "Help me find a person",
                 ["Which hour has the highest ratio?", "Hour 11."])
    assert read.people == [], "nobody was named and nobody was under discussion"
    assert read.unknown_people == []


def test_a_subject_named_in_the_message_is_kept():
    read = _read(_reading(["IRON MAN"], shape="report"), "track iron man")
    assert read.people == ["IRON MAN"]


def test_a_subject_carried_by_the_conversation_is_kept():
    read = _read(_reading(["IRON MAN"], shape="summary"), "At which cameras?",
                 ["How many times was IRON MAN detected?", "IRON MAN was detected 8 times."])
    assert read.people == ["IRON MAN"], "an elliptical follow-up keeps its referent"


def test_one_unspelled_name_survives_because_a_script_may_differ():
    # An Arabic message naming a Latin-spelled person must keep its subject:
    # one ungrounded name is a plausible reading, a list of them is not.
    read = _read(_reading(["JOEY"]), "أين كان جوي؟")
    assert read.people == ["JOEY"]


def test_a_name_that_is_not_enrolled_still_reaches_the_ask_path():
    read = _read(_reading(["Ali Abbass"]), "track Ali Abbass")
    assert read.people == []
    assert read.unknown_people == ["Ali Abbass"], "the turn must still be able to ask"


def test_one_plausible_subject_is_not_dropped_because_others_were_invented():
    # Two ungrounded names would be a dump; here IRON MAN is grounded in the
    # message, so only the invented pair-mate needs to be judged.
    read = _read(_reading(["IRON MAN", "seed_person_004", "seed_person_008"]), "track iron man")
    assert read.people == ["IRON MAN"], "the named one survives, the invented ones do not"

"""The model reads the message; Python checks the reading against the world.

The agent had been growing a phrase list per bug — "track X", then "report
for tracking X", then "in Arabic" but not "make it Arabic", then "are you
sure". Matching words does not generalise; people phrase things however
they like, in two languages. So the turn is understood once, by the model,
into a small closed structure, and every slot is then validated against
what actually exists.

    docker exec face_recognition_api python -m pytest tests/test_interpreter.py -v
"""

import json

import pytest

from sql_agent.tools import interpreter
from sql_agent.tools.interpreter import Interpretation, interpret, validate

NAMES = ["JOEY", "IRON MAN", "Ali Abbass"]
CAMERAS = ["WEZARET DEFA3", "MAD5AL AMEN"]


class _Reply:
    def __init__(self, content):
        self.content = content


class _Model:
    """Returns whatever JSON the test scripts, and records the prompt."""

    def __init__(self, payload):
        self.payload = payload
        self.prompts = []

    def invoke(self, messages):
        text = "\n".join(str(m.content) for m in messages)
        self.prompts.append(text)
        # The settle question (one word, on the message alone) confirms the
        # doubt these fixtures script; a scripted reading stays a reading.
        if "given again in another language" in text:
            return _Reply("NO")
        if "question whether the previous answer is correct" in text:
            return _Reply("YES")
        if "Does the message ask the assistant for anything" in text:
            return _Reply(getattr(self, "asks", "YES"))
        body = (self.payload if isinstance(self.payload, str)
                else json.dumps(self.payload))
        return _Reply(body)


def _read(payload, **kwargs):
    kwargs.setdefault("identity_index", [{"display_name": n} for n in NAMES])
    kwargs.setdefault("camera_names", CAMERAS)
    return interpret(_Model(payload), kwargs.pop("text", "anything"), **kwargs)


# ---------------------------------------------------- the phrasings that broke

def test_a_report_request_is_a_data_request_however_it_is_phrased():
    reading = _read({"wants": "data", "question": "all detections of JOEY",
                     "people": ["JOEY"], "camera": None, "language": None,
                     "about_previous": False},
                    text="report for tracking joey")
    assert reading.wants == interpreter.DATA
    assert reading.people == ["JOEY"]
    assert reading.question == "all detections of JOEY"


def test_a_language_request_needs_no_particular_wording():
    reading = _read({"wants": "translation", "question": "", "people": [],
                     "language": "ar", "about_previous": True},
                    text="make it Arabic", has_result=True)
    assert reading.wants == interpreter.TRANSLATION and reading.language == "ar"


def test_questioning_the_answer_is_its_own_kind_of_turn():
    reading = _read({"wants": "confirmation", "question": "", "people": [],
                     "language": None, "about_previous": True},
                    text="ARE YOU SURE", has_result=True)
    assert reading.wants == interpreter.CONFIRMATION and reading.about_previous


def test_two_people_are_both_kept():
    reading = _read({"wants": "data", "people": ["Ali Abbass", "IRON MAN"],
                     "question": "detections of Ali Abbass and IRON MAN at the "
                                 "same camera within minutes"},
                    text="does ali abbass and iron man appeared at same time")
    assert reading.people == ["Ali Abbass", "IRON MAN"]


# ---------------------------------------------------- Python checks the reading

def test_a_name_that_is_not_enrolled_is_not_accepted_as_one():
    """The model may return anything; only stored names get through, and
    they keep the stored spelling."""
    reading = _read({"wants": "data", "people": ["joey", "Batman"],
                     "question": "where was joey"})
    assert reading.people == ["JOEY"]           # stored spelling, not "joey"
    assert reading.unknown_people == ["Batman"]


def test_a_camera_that_does_not_exist_is_separated_from_one_that_does():
    reading = _read({"wants": "data", "camera": "entrance",
                     "question": "who was at the entrance"})
    assert reading.camera is None and reading.unknown_camera == "entrance"
    reading = _read({"wants": "data", "camera": "wezaret defa3",
                     "question": "who was there"})
    assert reading.camera == "WEZARET DEFA3"


def test_nothing_to_translate_means_it_was_not_a_translation():
    reading = _read({"wants": "translation", "language": "ar",
                     "question": "track joey in arabic", "people": ["JOEY"]},
                    text="track joey in arabic", has_result=False)
    assert reading.wants == interpreter.DATA and reading.language == "ar"


def test_a_translation_without_a_target_language_is_not_one():
    reading = _read({"wants": "translation", "language": None,
                     "about_previous": True}, has_result=True)
    assert reading.wants == interpreter.CONFIRMATION


def test_an_unusable_reply_falls_back_instead_of_guessing():
    assert _read("I think you want a report") is None
    assert _read({"wants": "whatever the model felt like"}) is None
    assert _read({"question": "no label at all"}) is None
    assert interpret(None, "hi") is None
    assert _read({"wants": "data"}, text="  ") is None


def test_the_prompt_carries_the_situation_and_the_closed_lists():
    model = _Model({"wants": "chat"})
    interpret(model, "with whom was she",
              identity_index=[{"display_name": "JOEY"}],
              camera_names=CAMERAS,
              dialogue_state={"fields": {"referenced_entity": {"value": ["JOEY"]}}},
              has_result=True, last_question="where was JOEY last seen")
    prompt = model.prompts[0]
    assert "the person under discussion: JOEY" in prompt
    assert "where was JOEY last seen" in prompt
    assert "WEZARET DEFA3" in prompt and "- JOEY" in prompt
    assert "an answer is on hand to translate or export: yes" in prompt


def test_a_model_failure_is_not_an_answer():
    class _Broken:
        def invoke(self, messages):
            raise RuntimeError("provider down")

    assert interpret(_Broken(), "track joey") is None


# ---------------------------------------------------- what the reading drives

def test_the_reading_says_how_much_to_report():
    from sql_agent.tools.agent_tools import SQLAgentTools as T

    report = {"interpretation": {"shape": "report"}, "normalized_input": "report for joey"}
    assert T._answer_shape(report, 1) == "report"
    point = {"interpretation": {"shape": "answer"}, "normalized_input": "when was joey last seen"}
    assert T._answer_shape(point, 1) == "direct"


def test_a_language_slot_does_not_change_the_language_of_a_plain_question():
    """The model returned language="ar" on English questions, and they were
    answered in Arabic. The slot says what to RESTATE an answer in."""
    from sql_agent.tools.agent_tools import SQLAgentTools as T

    tools = T.__new__(T)
    state = {"response_language": "en", "identity_index": []}
    reading = Interpretation(wants=interpreter.DATA, question="where was JOEY",
                             people=["JOEY"], language="ar")
    tools._plan_from_reading(state, reading, {})
    assert state["response_language"] == "en"
    assert state["planned_action"]["action"] == "query_database"
    assert state["sql_generation_input"] == "where was JOEY"

    asked = Interpretation(wants=interpreter.TRANSLATION, language="ar")
    tools._plan_from_reading(state, asked, {})
    assert state["response_language"] == "ar"
    assert state["planned_action"]["action"] == "translate_artifact"


def test_two_named_people_get_an_answer_about_both():
    from sql_agent.tools.agent_tools import SQLAgentTools as T

    class _Db:
        def execute_query(self, sql):
            n = 8 if "IRON MAN" in sql else 0
            return {"success": True, "rows": [{"n": n}]}

    tools = T.__new__(T)
    tools.db = _Db()
    state = {"response_language": "en",
             "interpretation": {"people": ["IRON MAN", "Ali Abbass"]}}
    answer = T._empty_narration(tools, state)
    assert answer.startswith("They were never seen together: Ali Abbass has no detections")
    assert "IRON MAN: 8" in answer and "Ali Abbass: 0" in answer


def test_the_models_own_synonyms_are_folded_onto_the_closed_set():
    """A small model says "query" or "translate" as readily as the word it
    was handed; refusing its synonym only sends the turn back to the phrase
    lists. This normalises the MODEL's vocabulary, never the user's words."""
    assert _read({"wants": "query", "question": "where was joey"}).wants == interpreter.DATA
    assert _read({"wants": "translate", "language": "ar"},
                 has_result=True).wants == interpreter.TRANSLATION
    assert _read({"intent": "greeting"}).wants == interpreter.CHAT


def test_an_off_schema_reply_is_asked_once_more_before_giving_up():
    class _Twice:
        def __init__(self):
            self.calls = 0

        def invoke(self, messages):
            self.calls += 1
            if self.calls == 1:
                return _Reply('{"thoughts": "the user wants a report"}')
            return _Reply('{"wants": "data", "shape": "report", '
                          '"question": "all detections of JOEY"}')

    model = _Twice()
    reading = interpret(model, "report for tracking joey",
                        identity_index=[{"display_name": "JOEY"}])
    assert model.calls == 2
    assert reading.wants == interpreter.DATA and reading.shape == "report"


def test_the_schema_echo_is_read_as_the_first_choice():
    """Live, the model returned "data|translation" for `wants` — the
    placeholder it had been shown — and the whole reading was discarded."""
    reading = _read({"wants": "data|translation", "shape": "report|answer",
                     "question": "all detections of JOEY", "people": ["JOEY"]})
    assert reading.wants == interpreter.DATA and reading.shape == "report"
    assert _read({"wants": ["chat", "data"]}).wants == interpreter.CHAT


def test_a_confirmation_reading_is_settled_on_the_message_alone():
    """With a verification sentence in the transcript, the full reading
    called "make it Arabic" and "thank you" doubts as well. Two yes/no
    questions about the message by itself settle it."""
    class _Model2:
        def __init__(self, language, doubt):
            self.language, self.doubt, self.calls = language, doubt, 0

        def invoke(self, messages):
            self.calls += 1
            text = "\n".join(str(m.content) for m in messages)
            if "given again in another language" in text:
                return _Reply(self.language)
            if "question whether the previous answer is correct" in text:
                return _Reply(self.doubt)
            return _Reply(json.dumps({"wants": "confirmation", "about_previous": True}))

    m = _Model2("YES", "NO")
    r = interpret(m, "make it Arabic", has_result=True, message_language="en")
    assert r.wants == interpreter.TRANSLATION and r.language == "ar" and m.calls == 2

    r = interpret(_Model2("NO", "YES"), "are you sure", has_result=True)
    assert r.wants == interpreter.CONFIRMATION and r.language is None

    # Reading and settle disagree: ask, do not pick.
    r = interpret(_Model2("NO", "NO"), "hmm", has_result=True)
    assert r.wants == interpreter.CLARIFY
    assert "re-check the previous answer" in r.question_for_user



def test_both_languages_get_examples_of_both_kinds_of_turn():
    """Arabic exemplars existed only for confirmation, so an explicit
    Arabic "make the report in English" was read as a doubt and answered
    with the verification sentence."""
    model = _Model({"wants": "chat"})
    interpret(model, "اجعل التقرير بالإنجليزية", has_result=True)
    prompt = model.prompts[0]
    assert "اجعل التقرير بالإنجليزية" in prompt      # translation exemplar
    assert "هل أنت متأكد؟" in prompt                  # confirmation exemplar
    assert "never confirmation" in prompt


# ---------------------------------------------------- ask rather than guess

def test_the_model_may_say_it_cannot_tell_and_the_turn_asks():
    from sql_agent.tools.agent_tools import SQLAgentTools as T

    reading = _read({"wants": "clarify", "confidence": 0.3,
                     "question_for_user": "Which person do you mean?"},
                    text="track him")
    assert reading.wants == interpreter.CLARIFY
    tools = T.__new__(T)
    state = {"response_language": "en", "identity_index": []}
    plan = tools._plan_from_reading(state, reading, {})
    assert plan.action == "clarify"
    assert state["clarify_question"] == "Which person do you mean?"


def test_a_low_confidence_data_reading_asks_instead_of_querying():
    from sql_agent.tools.agent_tools import SQLAgentTools as T

    reading = _read({"wants": "data", "confidence": 0.2,
                     "question": "something about someone",
                     "question_for_user": "Who, and at which camera?"})
    tools = T.__new__(T)
    state = {"response_language": "en", "identity_index": []}
    plan = tools._plan_from_reading(state, reading, {})
    assert plan.action == "clarify"
    assert "Who, and at which camera?" in state["clarify_question"]


def test_a_name_nobody_is_enrolled_under_is_asked_about_not_queried():
    """"track batman": the query would match nothing and the empty-result
    path would then talk about somebody else. Ask, offer the closest
    enrolled names, and suspend the request so the answer resumes it."""
    from sql_agent.tools.agent_tools import SQLAgentTools as T

    reading = _read({"wants": "data", "confidence": 0.8, "people": ["Jeoy"],
                     "question": "all detections of Jeoy"}, text="track jeoy")
    assert reading.people == [] and reading.unknown_people == ["Jeoy"]
    tools = T.__new__(T)
    state = {"response_language": "en",
             "identity_index": [{"display_name": n} for n in NAMES]}
    plan = tools._plan_from_reading(state, reading, {})
    assert plan.action == "clarify"
    assert state["typo_of"] == "Jeoy"
    assert state["clarification_candidates"][0]["display_name"] == "JOEY"
    assert "JOEY" in state["clarify_question"]


def test_a_clarification_without_a_question_is_a_shrug_not_an_answer():
    reading = _read({"wants": "clarify", "question": "where was JOEY"})
    assert reading.wants == interpreter.DATA
    assert reading.confidence < interpreter.CONFIDENCE_FLOOR


def test_recall_answers_from_the_conversation_and_runs_no_query():
    from sql_agent.tools.agent_tools import SQLAgentTools as T

    reading = _read({"wants": "recall", "confidence": 0.9,
                     "question": "what was said about IRON MAN earlier"},
                    has_result=True)
    assert reading.wants == interpreter.RECALL
    tools = T.__new__(T)
    state = {"response_language": "en", "identity_index": []}
    plan = tools._plan_from_reading(state, reading, {})
    assert plan.action == "chat" and state.get("recall") is True
    # With nothing said yet there is nothing to recall.
    assert _read({"wants": "recall", "question": "x"}, has_result=False).wants == interpreter.DATA


def test_the_reading_sees_the_recent_conversation_and_the_open_question():
    model = _Model({"wants": "chat"})
    interpret(model, "the second one",
              recent_turns=["user: track ali", "assistant: Which one: Ali Abbass or Ali Hassan?"],
              question_pending=True, pending_question="Which one: Ali Abbass or Ali Hassan?")
    prompt = model.prompts[0]
    assert "RECENT CONVERSATION" in prompt and "user: track ali" in prompt
    assert "waiting for the answer: Which one: Ali Abbass or Ali Hassan?" in prompt


# ---------------------------------------------------- answers and echoes

def test_an_answer_that_names_an_enrolled_person_resumes_the_request():
    """We asked which person was meant; "i mean alio abbass or similar" was
    read as recall and the chat model announced "no records of Alio Abbass"
    without looking. The answer names Ali Abbass: it is a data turn."""
    reading = _read({"wants": "recall", "confidence": 0.9, "people": ["Ali Abbass"],
                     "question": "what was said about Ali Abbass", "about_previous": True},
                    text="i mean alio abbass or similar to this name",
                    has_result=True, question_pending=True,
                    pending_request="where was alio abbass last seen")
    assert reading.wants == interpreter.DATA
    assert reading.people == ["Ali Abbass"]
    assert reading.question == "where was alio abbass last seen (the person is Ali Abbass)"


def test_a_question_that_quotes_the_user_back_is_not_a_question():
    """"hi" was answered with: could you clarify what you mean by "i mean
    alio abbass or similar to this name"."""
    reading = _read({"wants": "clarify", "confidence": 0.8, "people": ["Ali Abbass"],
                     "question_for_user": 'could you please clarify what you mean by '
                                          '"i mean alio abbass or similar to this name"',
                     "about_previous": True},
                    text="hi", has_result=True,
                    recent_turns=["user: i mean alio abbass or similar to this name",
                                  "assistant: Ali Abbass is enrolled, but has no detections"])
    assert reading.wants == interpreter.CHAT
    assert reading.question_for_user == ""


def test_a_data_reading_built_only_from_the_situation_is_checked():
    """"thank you" after two data turns was read as data with the held
    camera copied in, and a query ran. None of the slots appear in the
    message, so the message is asked whether it asks for anything."""
    model = _Model({"wants": "data", "confidence": 0.9, "people": ["JOEY"],
                    "camera": "WEZARET DEFA3", "about_previous": True,
                    "question": "when was JOEY last seen at WEZARET DEFA3"})
    model.asks = "NO"
    reading = interpret(model, "thank you",
                        identity_index=[{"display_name": n} for n in NAMES],
                        camera_names=CAMERAS, has_result=True)
    assert reading.wants == interpreter.CHAT and reading.people == []

    # The same reading for a message that names the person is data.
    model = _Model({"wants": "data", "confidence": 0.9, "people": ["JOEY"],
                    "about_previous": True, "question": "when was JOEY last seen"})
    model.asks = "NO"          # must not even be consulted
    reading = interpret(model, "and joey?",
                        identity_index=[{"display_name": n} for n in NAMES],
                        camera_names=CAMERAS, has_result=True)
    assert reading.wants == interpreter.DATA
    assert not any("Does the message ask the assistant" in p for p in model.prompts)


def test_a_misspelled_name_still_counts_as_naming_the_person():
    from sql_agent.tools.interpreter import _echoes_a_user_turn, _mentions_any

    assert _mentions_any("i mean jeoy or similar to this name", ["JOEY"])
    assert _mentions_any("and wezaret?", ["WEZARET DEFA3"])
    assert not _mentions_any("thank you", ["JOEY", "WEZARET DEFA3"])
    assert not _mentions_any("hi", ["JOEY", None])
    # A paraphrased quote of the user's earlier words is still an echo.
    assert _echoes_a_user_turn("what do you mean by jeoy or similar to this name",
                               ["user: i mean jeoy or similar to this name"])
    assert not _echoes_a_user_turn("Which camera do you mean?",
                                   ["user: i mean jeoy or similar to this name"])

"""One rule, enforced on every surface that DESCRIBES a turn.

    Anything the system says about the current turn must be derived from the
    current turn. Committed state may inform a DECISION; it may never supply
    a FACT in the answer.

Four bugs of this exact shape reached a user before the rule was written down:

    a report titled "hi"                  `last_query` from an unrelated turn
    a PDF containing a failure notice     any AI message over 40 characters
    "thanks" -> "I've prepared ... PDF"   the transcript, no fact about THIS turn
    "track iron man" -> "...by Joey?"     an entity from an earlier turn

Each was fixed where it was found. This file exists so the FIFTH fails a test
instead of reaching anyone: every surface that describes a turn gets the same
question asked of it — given state from a previous turn, does it say something
untrue about this one?

    docker exec face_recognition_api python -m pytest tests/test_current_turn_only.py -v
"""

import pytest

from sql_agent.tools.agent_loop import names_a_stranger


def _tools(monkeypatch, last_ai_text="A previous answer about Joey, at length."):
    import sql_agent.tools.agent_tools as module

    class _Memory:
        user_id = 1

        def get_recent_messages(self, limit=8):
            from langchain_core.messages import AIMessage
            return [AIMessage(content=last_ai_text)]

    monkeypatch.setattr(module, "create_llm", lambda *a, **k: None)
    monkeypatch.setattr(module, "create_sql_llm", lambda *a, **k: None)
    monkeypatch.setattr(module, "DatabaseManager", lambda *a, **k: object())
    monkeypatch.setattr(module, "SQLKnowledgeBase", lambda *a, **k: None)
    return module.SQLAgentTools(conversation_memory=_Memory())


# ------------------------------------------- surface 1: the question we ask

@pytest.mark.parametrize("question,user_text,stale", [
    ("Can you clarify what you mean by Joey?", "track iron man", "Joey"),
    ("Did you mean the Weekly Report?", "make it Arabic", "Weekly Report"),
    ("Which of Ali Abbas's detections?", "how many cameras", "Ali Abbas"),
])
def test_a_clarification_may_not_name_someone_the_user_did_not(
        question, user_text, stale):
    """THE reported bug: asked about Iron Man, asked back about Joey.

    The model authors these and sees the whole transcript, so it can ask
    about whoever dominates it rather than whoever was just mentioned.
    """
    assert names_a_stranger(question, user_text) == stale


@pytest.mark.parametrize("question,user_text", [
    ("Could you clarify which person you meant by Zoltan Kaszubowski?",
     "track Zoltan Kaszubowski"),
    ("Which camera did you mean?", "only camera 3"),
    ("Could you tell me a little more about what you need?", "hi"),
    ("Which person do you mean?", "track someone"),
    ("Do you want the report as a PDF or Word?", "make that a document"),
])
def test_a_grounded_question_is_left_alone(question, user_text):
    """The negative control. Rejecting every question would also 'fix' it.

    A guard that fires on legitimate clarifications would remove the one
    behaviour that stops the agent guessing at somebody's identity.
    """
    assert names_a_stranger(question, user_text) is None


def test_the_check_is_factual_not_a_phrasebook():
    """It asks "is this string in what they typed?", nothing more.

    The same name is fine or stale depending ONLY on the user's message, so
    there is no list of words to maintain and nothing language-specific.
    """
    assert names_a_stranger("Do you mean Joey?", "track Joey") is None
    assert names_a_stranger("Do you mean Joey?", "track Ali") == "Joey"


# ------------------------------------------- surface 2: the document title

def test_a_title_comes_from_the_question_it_answers(monkeypatch):
    """Instance 1: a surveillance report titled "hi"."""
    tools = _tools(monkeypatch)
    out = tools.render_artifact({
        "final_response": "**REPORT** 16 cameras are registered.",
        "response_language": "en",
        "planned_action": {"action": "generate_document"},
        "working_context": {
            "last_narrative_reportable": True,
            "last_query": "hi",                       # an unrelated turn
            "last_result": {"question": "track Joey", "row_count": 3},
        }})

    assert (out.get("artifact_payload") or {}).get("title") == "track Joey"


# -------------------------------------------- surface 3: the document body

def test_a_document_is_not_built_from_a_failed_turn(monkeypatch):
    """Instance 2: a PDF whose body was "I couldn't reach that report"."""
    failure = ("I couldn't reach that report to translate it. "
               "Please try asking for it again.")
    tools = _tools(monkeypatch, last_ai_text=failure)

    content = tools._document_source_text({
        "final_response": "",
        "working_context": {"last_narrative_reportable": False}})

    assert failure not in content


# --------------------------------------------- surface 4: the facts block

def test_the_facts_describe_this_turn_not_the_transcript(monkeypatch):
    """Instance 3: "thanks" answered "I've prepared ... as a PDF"."""
    tools = _tools(monkeypatch)
    facts = tools._grounding_section({
        "final_response": "", "working_context": {},
        "response_language": "en",
        "planned_action": {"action": "chat"}})

    assert "NO document was produced" in facts
    assert "No data was created" in facts


# ------------------------------------------------------- the rule itself

def test_every_describing_surface_is_covered_here():
    """A new surface that describes a turn must be added to this file.

    Listed explicitly rather than discovered: the point of this suite is that
    the NEXT instance of this bug shape fails a test rather than reaching a
    user, and that only works if new surfaces are enrolled deliberately.
    """
    import sql_agent.tools.agent_tools as module

    surfaces = {
        "_usable_title",          # what a document is called
        "_document_source_text",  # what goes inside it
        "_grounding_section",     # what the narrative may claim
        "_clarify_question_for",  # what we ask back
        "_failure_narration",     # what we say went wrong
        "_empty_narration",       # what we say when nothing matched
    }
    missing = {name for name in surfaces
               if not hasattr(module.SQLAgentTools, name)}
    assert not missing, (
        f"a describing surface disappeared without this suite following it: "
        f"{sorted(missing)}")

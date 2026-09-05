"""Interactive correction: the question suspends the request, the answer
resumes it with the correction made.

    user: track Jeoy
    bot:  I couldn't find an enrolled person named "Jeoy". Did you mean JOEY?
          Reply "yes" or type the correct name.
    user: yes
    ->    the request "track JOEY" runs, as if typed

The answer is the argument; the suspended request is the call. Nothing is
re-planned from scratch and the model is not asked to remember what was
being asked.

    docker exec face_recognition_api python -m pytest tests/test_interactive_resume.py -v
"""

from sql_agent import dialogue_state as ds
from sql_agent.tools.agent_tools import SQLAgentTools as T

INDEX = [{"identity_id": "1", "display_name": "JOEY"},
         {"identity_id": "2", "display_name": "IRON MAN"},
         {"identity_id": "3", "display_name": "Ali Abbass"}]


# --------------------------------------------------------- suggestions

def test_close_names_are_offered_for_a_typo():
    assert T._closest_names("Jeoy", [e["display_name"] for e in INDEX]) == ["JOEY"]
    assert T._closest_names("iron mna", [e["display_name"] for e in INDEX]) == ["IRON MAN"]
    assert T._closest_names("zzzzzz", [e["display_name"] for e in INDEX]) == []


def test_a_typo_in_one_word_of_a_camera_name_is_caught():
    """Live: "camera wezart" got the full camera list instead of "Did you
    mean WEZARET DEFA3?" - the whole label scored 0.63, the word 0.92."""
    cameras = ["MAD5AL AMEN (1)", "WEZARET DEFA3", "KSA", "IT-DIRECTORY"]
    assert T._closest_names("wezart", cameras, cutoff=0.75) == ["WEZARET DEFA3"]
    # a dead id still resembles nothing well enough
    assert T._closest_names("MD5AL_3EIN_7LWE", cameras, cutoff=0.75) == []


def test_the_question_names_the_options_in_both_languages():
    en = T._did_you_mean("Jeoy", ["JOEY"], {"response_language": "en"})
    assert en == ("I couldn't find an enrolled person named “Jeoy”. Did you mean "
                  "JOEY? Reply “yes” or type the correct name.")
    ar = T._did_you_mean("wezart", ["WEZARET DEFA3"], {"response_language": "ar"},
                         camera=True)
    assert ar.startswith("لم أجد كاميرا باسم «wezart». هل تقصد WEZARET DEFA3؟")


class _Db:
    def execute_query(self, sql):
        if "COUNT(*)" in sql:
            return {"success": True, "rows": [{"n": 0}]}
        return {"success": True, "rows": []}        # resolve_person finds nobody


def test_an_unknown_name_becomes_a_did_you_mean_question(monkeypatch):
    """resolve_person itself catches near misses like 'Jeoy'; this is the
    branch for a name it could not match at all."""
    from sql_agent.tools import tool_executors as tx

    monkeypatch.setattr(tx, "execute_read_only",
                        lambda name, args, **kw: {"status": "not_found"})
    tools = T.__new__(T)
    tools.db = _Db()
    state = {"identity_index": INDEX, "response_language": "en"}
    route = tools._resolve_entity_and_route(
        state, {"unresolved_entity": "Jeoy", "unresolved_kind": "person"})

    assert route == "chat_response"
    assert state["planned_action"]["action"] == "clarify"
    assert state["typo_of"] == "Jeoy"
    assert state["clarification_candidates"] == [{"display_name": "JOEY"}]
    assert "Did you mean JOEY?" in state["clarify_question"]


# ------------------------------------------------------------ the answer

def _pending(wrong, candidates, original):
    state = ds.empty_state()
    return ds.apply_delta(state, {
        "operation": "REPLACE", "field": "pending_clarification",
        "proposed_value": {"type": "typo", "original_intent": "SQL_QUERY",
                           "original_query": original, "field": "person",
                           "wrong": wrong,
                           "candidates": [{"display_name": c} for c in candidates]},
        "source": "tool_result"}, turn_id="t1")


def test_yes_picks_the_single_candidate_and_a_name_picks_by_name():
    state = _pending("Jeoy", ["JOEY"], "track Jeoy")
    assert ds.match_candidate(state, "yes")["display_name"] == "JOEY"
    assert ds.match_candidate(state, "نعم")["display_name"] == "JOEY"
    assert ds.match_candidate(state, "joey")["display_name"] == "JOEY"
    assert ds.match_candidate(state, "no, iron man") is None

    two = _pending("Jo", ["JOEY", "JOE"], "track Jo")
    assert ds.match_candidate(two, "yes") is None      # ambiguous
    assert ds.match_candidate(two, "the second one")["display_name"] == "JOE"


def test_the_request_is_rebuilt_with_the_correction():
    assert T._resume_corrected_request("track Jeoy", "Jeoy", "JOEY") == "track JOEY"
    assert T._resume_corrected_request("who was at camera wezart today", "wezart",
                                       "WEZARET DEFA3") == "who was at camera WEZARET DEFA3 today"
    assert T._resume_corrected_request("track jeoy", "Jeoy", "JOEY") == "track JOEY"
    assert T._resume_corrected_request("track him", "Jeoy", "JOEY") == "track him JOEY"


def test_agent_state_declares_the_resume_keys():
    from sql_agent.state import AgentState

    for key in ("typo_of", "resumed_from_typo"):
        assert key in AgentState.__annotations__, key

"""Zero rows for a question is not zero detections for a person.

Live:

    user: when joey last seen and where
    bot:  Joey was last seen on WEZARET DEFA3 at 2026-08-23 11:11:54.
    user: with whom she was
    bot:  JOEY is enrolled, but has no detections recorded yet - no camera
          has seen them.

The co-appearance query returned nothing, the resolver found JOEY enrolled,
and the old verdict equated the two. JOEY has three detections; the honest
answer is that nothing matched THAT question, and how many she has.

    docker exec face_recognition_api python -m pytest tests/test_empty_is_not_no_data.py -v
"""

from sql_agent.tools.agent_tools import SQLAgentTools as T


class _Db:
    """resolve_person reads faces names; the count reads faces too."""

    def __init__(self, count):
        self.count = count

    def execute_query(self, sql):
        if "COUNT(*)" in sql:
            assert "'JOEY'" in sql and "name = " in sql
            return {"success": True, "rows": [{"n": self.count}]}
        return {"success": True, "rows": [{"name": "JOEY"}]}


def _tools(db):
    tools = T.__new__(T)
    tools.db = db
    return tools


def test_a_person_with_detections_is_not_told_they_have_none():
    tools = _tools(_Db(3))
    state = {"generated_sql": "SELECT ... WHERE f.name ILIKE '%joey%'",
             "sql_purpose": "people detected alongside JOEY within 10 minutes",
             "response_language": "en", "identity_index": []}
    route = tools._resolve_entity_and_route(
        state, {"unresolved_entity": "JOEY", "unresolved_kind": "person"})

    assert route == "chat_response"
    assert state.get("entity_without_data") is None
    assert state["entity_has_data"] == ["JOEY", 3]
    answer = tools._empty_narration(state)
    assert "Nothing matched that question for JOEY" in answer
    assert "3 detection(s) on record" in answer
    assert "alongside JOEY" in answer
    assert tools._empty_narration({**state, "response_language": "ar"})


def test_a_person_with_nothing_recorded_is_still_told_so():
    tools = _tools(_Db(0))
    state = {"generated_sql": "SELECT ... WHERE f.name ILIKE '%joey%'",
             "response_language": "en", "identity_index": []}
    tools._resolve_entity_and_route(
        state, {"unresolved_entity": "JOEY", "unresolved_kind": "person"})
    assert state["entity_without_data"] == "JOEY"
    assert "no detections recorded" in tools._empty_narration(state)


def test_the_count_is_a_guarded_literal_query():
    seen = []

    class _Spy:
        def execute_query(self, sql):
            seen.append(sql)
            return {"success": True, "rows": [{"n": 1}]}

    assert _tools(_Spy())._detections_on_record("O'Brien") == 1
    assert seen == ["SELECT COUNT(*) AS n FROM faces WHERE name = 'O''Brien'"]


def test_with_whom_is_a_point_question():
    assert T._is_point_question("with whom she was")
    assert T._is_point_question("مع من كانت")

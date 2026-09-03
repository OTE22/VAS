"""A camera the user never named is not a camera to look up.

    user: with whom she was
    bot:  There is no camera named "entrance". The cameras are: ...

The model had copied a seed example's filter into a companion question
that named no camera. Whether the user named the camera is a fact
(message, resumed request, generation input, held camera); when none did,
the query is regenerated once with the invented filter called out, on the
same deterministic path as the stored-spelling correction.

    docker exec face_recognition_api python -m pytest tests/test_invented_camera_filter.py -v
"""

from sql_agent.tools.agent_tools import SQLAgentTools as T


class _CamDb:
    def execute_query(self, sql):
        if "COUNT(*)" in sql:
            return {"success": True, "rows": [{"n": 49}]}
        return {"success": True, "rows": [
            {"id": "1", "pipeline_id": "1971528f", "location_name": "WEZARET DEFA3",
             "is_active": 1}]}


def _tools():
    tools = T.__new__(T)
    tools.db = _CamDb()
    return tools


def test_a_camera_nobody_named_is_dropped_and_the_query_regenerated():
    tools = _tools()
    state = {"normalized_input": "with whom she was", "response_language": "en",
             "generated_sql": "SELECT 1 FROM pipelines p WHERE p.location_name LIKE '%entrance%'"}
    route = tools._resolve_entity_and_route(
        state, {"unresolved_entity": "entrance", "unresolved_kind": "camera"})
    assert route == "check_schema"
    assert "entrance" in state["sql_correction_hint"]["reason"]
    assert state["generated_sql"] == ""
    assert state.get("camera_not_found") is None


def test_a_camera_the_user_named_is_looked_up_even_when_misspelled():
    tools = _tools()
    assert not tools._camera_invented(
        {"normalized_input": "who was detected at camera wezaret"}, "WEZARET DEFA3")
    state = {"normalized_input": "who was detected at camera entrance today",
             "response_language": "en"}
    route = tools._resolve_entity_and_route(
        state, {"unresolved_entity": "entrance", "unresolved_kind": "camera"})
    assert route == "chat_response"
    assert state.get("sql_correction_hint") is None


def test_a_held_or_corrected_camera_counts_as_named():
    tools = _tools()
    held = {"working_context": {"dialogue_state": {"fields": {
        "active_camera": {"value": ["WEZARET DEFA3"]}}}},
        "normalized_input": "and yesterday?"}
    assert not tools._camera_invented(held, "WEZARET DEFA3")
    corrected = {"normalized_input": "yes", "camera_corrected_to": "WEZARET DEFA3"}
    assert not tools._camera_invented(corrected, "WEZARET DEFA3")
    assert tools._camera_invented({"normalized_input": "yes"}, "WEZARET DEFA3")

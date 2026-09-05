"""The tool layer: what the agent may call, and what it may never do with it.

Tools make the agent agentic — it can look things up mid-turn instead of
guessing at camera ids and misspelled names. That widens how the agent
DECIDES; it must not widen what the agent may DO. So the properties pinned
here are:

  1. the model chooses a tool name and arguments; `validate_call` re-checks
     both, and an argument that carries SQL is refused outright;
  2. read-only look-ups are genuinely read-only and caller-scoped;
  3. a committed tool call becomes a PlannedAction and goes through the SAME
     dispatcher validation as the planner's own output — the tool layer is
     not a second, weaker route to the same power;
  4. an engine WITHOUT native function calling gets the identical tools via
     the prompted fallback, so dev and production cannot diverge.

No LLM and no database: the loop is driven with recorded fakes, so every
test is deterministic.

    docker exec face_recognition_api python -m pytest tests/test_agent_tools.py -v
"""

import json

import pytest

from sql_agent.tools import tool_registry as tr
from sql_agent.tools import tool_executors as tx
from sql_agent.tools import agent_loop


# ------------------------------------------------------------- the registry

def test_every_tool_has_a_schema_and_a_description():
    """A tool the model cannot understand is a tool it will misuse."""
    names = {s["function"]["name"] for s in tr.tool_specs()}
    assert names == set(tr.ALL_TOOLS)
    for spec in tr.tool_specs():
        function = spec["function"]
        assert len(function["description"]) > 30, function["name"]
        params = function["parameters"]
        assert params["type"] == "object"
        # No free-form extras: an undefined argument is an unvalidated one.
        assert params["additionalProperties"] is False, function["name"]


def test_the_prompted_rendering_covers_the_same_tools():
    """Production may ignore a native `tools` payload entirely.

    One source of truth: if the rendering drifted from the specs, the
    fallback engine would be offered tools that no longer exist — and the
    fallback is the path that gets exercised least.
    """
    rendered = tr.render_tools_for_prompt()
    for name in tr.ALL_TOOLS:
        assert name in rendered, f"{name} missing from the prompted fallback"


@pytest.mark.parametrize("name,arguments", [
    ("drop_all_tables", {}),
    ("", {}),
    ("query_database", {"question": "SELECT * FROM users"}),
    ("modify_active_query", {"change": "camera 3; DROP TABLE faces"}),
    ("generate_document", {"format": "exe"}),
    ("translate_document", {"language": "klingon"}),
    ("translate_document", {"language": "ar", "document_id": "../../etc/passwd"}),
])
def test_a_dangerous_or_unknown_call_is_refused(name, arguments):
    with pytest.raises(tr.ToolCallRejected):
        tr.validate_call(name, arguments)


def test_sql_in_an_argument_is_refused_with_a_usable_reason():
    """The SQL chain composes queries from plain words.

    A tool argument carrying SQL is the model trying to bypass that, whether
    deliberately or through confusion — and the rejection has to tell it what
    to do instead, because the loop feeds the reason back for a retry.
    """
    with pytest.raises(tr.ToolCallRejected) as rejection:
        tr.validate_call("query_database", {"question": "select name from faces"})
    assert "plain words" in str(rejection.value)


def test_unknown_arguments_are_dropped_not_fatal():
    """Models add stray keys; refusing the call for that buys no safety."""
    clean = tr.validate_call("generate_document",
                             {"format": "pdf", "colour": "blue", "rows": 9})
    assert clean == {"format": "pdf"}


def test_a_missing_required_argument_is_refused():
    with pytest.raises(tr.ToolCallRejected):
        tr.validate_call("resolve_person", {})


# ------------------------------------------------- native / prompted parity

def test_a_native_tool_call_and_prompted_json_parse_identically():
    """Both engines must converge on ONE internal shape.

    If they did not, the fallback path would quietly be a different agent —
    and it is the path production runs.
    """
    class _NativeReply:
        content = ""
        additional_kwargs = {"tool_calls": [{
            "id": "call_1", "type": "function",
            "function": {"name": "resolve_person",
                         "arguments": '{"name": "Jeoy"}'}}]}

    native = tr.parse_tool_response(_NativeReply())
    prompted = tr.parse_tool_response(
        'Sure!\n{"tool": "resolve_person", "arguments": {"name": "Jeoy"}}')

    assert native["name"] == prompted["name"] == "resolve_person"
    assert tr.validate_call(**native) == tr.validate_call(**prompted) == {
        "name": "Jeoy"}


def test_prose_with_no_tool_call_parses_to_nothing():
    assert tr.parse_tool_response("I am sorry, I cannot help with that.") is None


# ------------------------------------------------------------- executors

class _FakeDb:
    """A stand-in that records the SQL it is asked to run."""

    def __init__(self, rows):
        self.rows = rows
        self.executed = []

    def execute_query(self, sql):
        self.executed.append(sql)
        return {"success": True, "rows": self.rows}


def test_look_ups_never_interpolate_user_input_into_sql():
    """execute_query takes a bare string with NO bound parameters.

    So every look-up query must be a fixed literal and the matching must
    happen in Python. If a name ever reached the SQL, this is where a quoting
    bug becomes an injection.
    """
    db = _FakeDb([{"name": "JOEY"}, {"name": "ALI"}])
    rejected = tx.execute_read_only("resolve_person", {"name": "Robert'); DROP TABLE--"},
                                    db=db)
    assert rejected["error_code"] == "INVALID_ARGUMENTS"
    assert db.executed == []
    tx.execute_read_only("resolve_person", {"name": "Robert O'Brien"}, db=db)
    assert db.executed, "no query ran"
    for sql in db.executed:
        assert "DROP" not in sql.upper()
        assert "Robert" not in sql, "user input reached the SQL string"


def test_resolve_person_matches_a_misspelling_case_insensitively():
    """The whole point: "Jeoy" must find the stored "JOEY".

    difflib compares raw strings, so a case difference alone dropped a real
    match below the cutoff — defeating the one case this tool exists for.
    """
    db = _FakeDb([{"name": "JOEY"}, {"name": "MARWAN"}])
    result = tx.execute_read_only("resolve_person", {"name": "Jeoy"}, db=db)
    assert result["matches"] == ["JOEY"]


def test_an_unknown_person_returns_no_match_and_says_to_ask():
    """No match must lead to a question, never to an invented person."""
    db = _FakeDb([{"name": "JOEY"}])
    result = tx.execute_read_only("resolve_person", {"name": "Zzzznotreal"},
                                  db=db)
    assert result["matches"] == []
    assert "ask" in result["note"].lower()


def test_a_failing_look_up_reports_an_error_rather_than_exploding():
    class _BrokenDb:
        def execute_query(self, sql):
            raise RuntimeError("column does not exist")

    result = tx.execute_read_only("list_cameras", {}, db=_BrokenDb())
    assert "error" in result, "a broken look-up must not kill the turn"


def test_list_my_documents_only_shows_what_the_caller_was_given():
    """The index is built by an owner-scoped query in the API layer, so this
    physically cannot list another user's document."""
    result = tx.execute_read_only(
        "list_my_documents", {}, db=None,
        artifact_index=[{"artifact_id": "a1", "title": "Mine",
                         "language": "en", "type": "pdf"}])
    assert [d["document_id"] for d in result["documents"]] == ["a1"]
    assert "source_content" not in json.dumps(result)


# ------------------------------------------------------------- the loop

class _ScriptedLlm:
    """Replays a fixed sequence of replies and records what it was sent."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []
        self.bound = None

    def bind(self, **kwargs):
        self.bound = kwargs
        return self

    def invoke(self, messages):
        self.calls.append(messages)
        return self.replies.pop(0) if self.replies else ""


def _native(name, arguments):
    class _Reply:
        content = ""
        additional_kwargs = {"tool_calls": [{
            "function": {"name": name, "arguments": json.dumps(arguments)}}]}
    return _Reply()


# --------------------------------------------- the dispatcher still decides

def test_a_committed_tool_call_is_revalidated_as_a_planned_action():
    """The tool layer widens how the agent DECIDES, not what it may DO.

    Conversion goes through validate_plan, so allow-lists, precondition
    downgrades and the artifact candidate-set check all still apply.
    """
    from sql_agent.tools import planner

    candidates = planner.resolve_candidates({}, [], "")
    planned = agent_loop.action_to_planned(
        {"name": "translate_document",
         "arguments": {"language": "ar",
                       "document_id": "99999999-9999-4999-8999-999999999999"}},
        candidates)

    # No such document belongs to this caller, so the id is discarded and the
    # action degrades to a question rather than reaching for someone else's.
    assert planned["action"] == "clarify"
    assert planned["artifact_id"] is None


def test_a_tool_call_cannot_smuggle_an_unsupported_format():
    from sql_agent.tools import planner

    candidates = planner.resolve_candidates(
        {"last_result": {"row_count": 2, "sql": "SELECT 1"}}, [], "")
    planned = agent_loop.action_to_planned(
        {"name": "generate_document", "arguments": {"format": "pdf"}},
        candidates)
    assert planned["action"] == "generate_document"
    assert planned["format"] == "pdf"


def test_update_task_state_carries_a_delta_for_the_application_to_commit():
    """A correction becomes a validated StateDelta — committed only after the
    action succeeds, never straight from model output."""
    from sql_agent.tools import planner

    candidates = planner.resolve_candidates(
        {"last_result": {"row_count": 2, "sql": "SELECT 1"}}, [], "")
    planned = agent_loop.action_to_planned(
        {"name": "update_task_state",
         "arguments": {"operation": "REPLACE", "field": "active_camera",
                       "value": "4"}},
        candidates)

    assert planned["action"] == "modify_previous_query"
    assert planned["state_delta"]["operation"] == "REPLACE"
    assert planned["state_delta"]["field"] == "active_camera"
    assert planned["state_delta"]["source"] == "user_correction"

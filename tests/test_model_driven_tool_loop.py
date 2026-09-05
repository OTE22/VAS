"""Behavioral contract for model-led SQL-agent routing.

These tests pin meanings and tool collaboration, not keyword lists. The
model is scripted because production output is stochastic; Python's contract
is that a valid model decision is observed, validated, bounded, and routed
through the existing graph authority boundary.
"""

from sql_agent.tools import agent_loop
from sql_agent.tools.agent_tools import SQLAgentTools


class _Reply:
    def __init__(self, *, name=None, arguments=None, content=""):
        self.content = content
        self.tool_calls = ([] if not name else [{
            "name": name,
            "args": arguments or {},
        }])


class _Model:
    model = "scripted-tool-model"

    def __init__(self, replies):
        self.replies = list(replies)
        self.prompts = []
        self.bound_tools = None

    def bind(self, **kwargs):
        self.bound_tools = kwargs.get("tools")
        return self

    def invoke(self, messages):
        self.prompts.append("\n".join(str(m.content) for m in messages))
        return self.replies.pop(0)


class _Db:
    def execute_query(self, _sql):
        return {"success": True, "rows": [], "row_count": 0}


def _run(model, **kwargs):
    agent_loop._NATIVE_SUPPORT.clear()
    agent_loop._NATIVE_DEMOTED_AT.clear()
    return agent_loop.run_tool_loop(
        model,
        user_text=kwargs.pop("user_text", "hello"),
        context_block=kwargs.pop("context_block", "- last_result: none"),
        db=kwargs.pop("db", _Db()),
        dialogue_state=kwargs.pop("dialogue_state", None),
        artifact_index=kwargs.pop("artifact_index", []),
        identity_index=kwargs.pop("identity_index", []),
        **kwargs,
    )


def test_model_can_choose_conversation_without_a_database_lookup():
    model = _Model([_Reply(
        name="answer_directly", arguments={"answer": "Hello! How can I help?"})])

    call, trace = _run(model, user_text="good morning")

    assert call["name"] == "answer_directly"
    assert [entry["tool"] for entry in trace] == ["answer_directly"]
    assert model.bound_tools, "the model was not given the tool vocabulary"


def test_model_observes_a_person_lookup_before_committing_to_a_query():
    model = _Model([
        _Reply(name="resolve_person", arguments={"name": "Jeoy"}),
        _Reply(name="query_database", arguments={
            "question": "all detections of JOEY with camera and timestamp",
            "response_shape": "report",
        }),
    ])

    call, trace = _run(
        model,
        user_text="follow Jeoy and give me the complete movement picture",
        identity_index=[{"identity_id": "person-1", "display_name": "JOEY"}],
    )

    assert call["name"] == "query_database"
    assert call["arguments"]["response_shape"] == "report"
    assert [entry["tool"] for entry in trace] == [
        "resolve_person", "query_database"]
    assert trace[0]["resolved_entity"]["canonical_name"] == "JOEY"
    assert "OBSERVATION FROM resolve_person" in model.prompts[-1]


def test_invalid_tool_call_is_explained_and_the_model_can_correct_it():
    model = _Model([
        _Reply(name="query_database", arguments={}),
        _Reply(name="ask_clarifying_question", arguments={
            "question": "Which person would you like me to investigate?"}),
    ])

    call, trace = _run(model, user_text="check what happened")

    assert call["name"] == "ask_clarifying_question"
    assert trace[0]["rejected"] is True
    assert "missing required argument" in model.prompts[-1]


def test_provider_without_native_tools_uses_the_same_prompted_schemas():
    model = _Model([
        _Reply(content="Hello there"),
        _Reply(content=(
            '{"tool":"answer_directly","arguments":'
            '{"answer":"Hello there"}}')),
    ])

    call, trace = _run(model, user_text="hello")

    assert call["name"] == "answer_directly"
    assert trace[0]["capability_probe"] == "prompted"
    assert "query_database" in model.prompts[-1]
    assert "CURRENT USER MESSAGE" in model.prompts[-1]


def test_validated_tool_decision_populates_downstream_context():
    tools = SQLAgentTools.__new__(SQLAgentTools)
    state = {"reasoning_steps_used": 0, "observations": []}
    call = {"name": "query_database", "arguments": {
        "question": "all detections of JOEY with camera and timestamp",
        "response_shape": "report",
        "uses_context": True,
    }}
    trace = [
        {"tool": "resolve_person", "ok": True,
         "signature": ["resolve_person", '{"name": "Jeoy"}'],
         "observation": {"status": "ok", "tool": "resolve_person"},
         "resolved_entity": {"tool": "resolve_person", "raw_text": "Jeoy",
                             "identity_id": "person-1",
                             "canonical_name": "JOEY"}},
        {"tool": "query_database", "committed": True,
         "signature": ["query_database", "query-signature"]},
    ]

    plan = tools._apply_model_tool_call(state, call, trace, {})

    assert plan.action == "query_database"
    assert state["planned_action"]["source"] == "tool_loop"
    assert state["sql_generation_input"].startswith("all detections")
    assert state["interpretation"]["shape"] == "report"
    assert state["interpretation"]["about_previous"] is True
    assert state["resolved_entities"][0]["canonical_name"] == "JOEY"


def test_conversational_recall_keeps_recent_context_without_querying():
    tools = SQLAgentTools.__new__(SQLAgentTools)
    state = {"reasoning_steps_used": 0, "observations": []}
    call = {"name": "answer_directly", "arguments": {
        "answer": "I said the earlier result contained three rows.",
        "uses_context": True,
    }}
    trace = [{"tool": "answer_directly", "committed": True,
              "signature": ["answer_directly", "recall-signature"]}]

    plan = tools._apply_model_tool_call(state, call, trace, {})

    assert plan.action == "chat"
    assert state["recall"] is True
    assert state["turn_is_a_request"] is True
    assert state["interpretation"]["wants"] == "recall"
    assert state["interpretation"]["about_previous"] is True

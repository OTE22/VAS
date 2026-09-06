"""Orchestrator selection and the NeMo adapter's contract - with fakes.

The toolkit itself is optional and versioned by NVIDIA; the adapter's seams
(the toolkit probe and the agent builder) are injected here so the contract
is proven without the package: the agent sees only catalogue tools, every
call carries the caller's context, and a missing toolkit falls back.

    docker exec face_recognition_api python -m pytest tests/test_orchestrator_adapter.py -v
"""
from types import SimpleNamespace

from sql_agent.mcp import MCPToolset, ToolContext
from sql_agent.orchestration import NemoAgentAdapter, select_orchestrator
from sql_agent.security.sql_guard import SqlPolicy


class _Db:
    KNOWN_SCHEMA = {"tables": {"detections": {"description": "events", "columns": [
        {"column_name": "pipeline_id", "data_type": "varchar", "is_nullable": "NO", "description": "camera"}],
        "primary_keys": [], "foreign_keys": []}}, "relationships": ["r1"]}

    def __init__(self):
        self.sql_policy = SqlPolicy(allowed_tables=frozenset({"detections"}))
        self.executed = []

    def execute_query(self, sql):
        self.executed.append(sql)
        return {"success": True, "rows": [{"n": 165}], "row_count": 1, "columns": ["n"]}


def test_langgraph_is_the_default_and_unknown_values_fall_back():
    assert select_orchestrator(SimpleNamespace(agent_orchestrator="langgraph")).name == "langgraph"
    choice = select_orchestrator(SimpleNamespace(agent_orchestrator="autogen"))
    assert choice.name == "langgraph" and "unknown" in choice.reason


def test_nemo_needs_the_toolkit_and_falls_back_loudly_without_it():
    absent = select_orchestrator(SimpleNamespace(agent_orchestrator="nemo"), probe=lambda: None)
    assert absent.name == "langgraph" and "not installed" in absent.reason
    present = select_orchestrator(SimpleNamespace(agent_orchestrator="nemo"), probe=lambda: "1.2.0")
    assert present.name == "nemo" and "1.2.0" in present.reason


class _FakeAgent:
    """Stands in for the toolkit's ReAct agent: calls tools the way it would."""

    def __init__(self, llm, tools, system_prompt):
        self.tools = {t.mcp_name: t for t in tools}
        self.system_prompt = system_prompt

    def run(self, question):
        schema = self.tools["database.search_schema"](query="camera detections")
        assert schema["status"] == "ok"
        refused = self.tools["database.execute_readonly"](sql="DELETE FROM detections")
        assert refused["error_code"] == "PERMISSION_DENIED"
        result = self.tools["database.execute_readonly"](sql="SELECT COUNT(*) AS n FROM detections")
        return f"There were {result['data']['rows'][0]['n']} detections."


def test_the_agent_sees_only_catalogue_tools_and_the_gate_holds():
    db = _Db()
    toolset = MCPToolset(db=db, kb=None)
    adapter = NemoAgentAdapter(toolset, llm=object(), builder=_FakeAgent,
                               tools=["database.search_schema", "database.execute_readonly", "sql.validate"])
    names = {f.mcp_name for f in adapter.tool_functions(ToolContext(user_id=1))}
    assert "database.execute_readonly" in names and "sql.generate" not in names
    assert all(n in toolset.names() for n in names)
    out = adapter.run("How many detections?", ToolContext(user_id=1, role="user", request_id="r1"))
    assert out["response"] == "There were 165 detections."
    assert out["orchestrator"] == "nemo" and out["executed_queries"] == 1
    assert out["tools_called"] == ["database.search_schema", "database.execute_readonly", "database.execute_readonly"]
    assert db.executed == ["SELECT COUNT(*) AS n FROM detections"], "the refused DELETE never reached the database"


def test_the_callers_scope_travels_with_every_tool_call():
    seen = []

    class _Toolset(MCPToolset):
        def call(self, name, arguments=None, ctx=None):
            seen.append(ctx)
            return super().call(name, arguments, ctx)

    adapter = NemoAgentAdapter(_Toolset(db=_Db(), kb=None), llm=object(), builder=_FakeAgent,
                               tools=["database.search_schema", "database.execute_readonly"])
    scoped = ToolContext(user_id=9, role="user", pipeline_scope=frozenset({"cam-1"}))
    adapter.run("q", scoped)
    assert seen and all(c is scoped for c in seen)


def test_the_default_builder_reports_a_missing_toolkit_or_builds_a_runner():
    import pytest
    from sql_agent.orchestration.nemo_adapter import _probe_toolkit
    if _probe_toolkit() is None:
        with pytest.raises(ImportError):
            NemoAgentAdapter._default_builder(object(), [], "p")
    else:
        runner = NemoAgentAdapter._default_builder(object(), [], "p")
        assert hasattr(runner, "run"), "the installed toolkit yields a runnable agent"

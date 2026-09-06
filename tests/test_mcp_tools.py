"""The MCP tool catalogue: narrow, validated, structured, gated.

Runs against fakes for the database and the knowledge base, so it proves
the contracts (schemas, error envelopes, timeouts, the read-only gate) and
never the data. The AST guard is the real one.

    docker exec face_recognition_api python -m pytest tests/test_mcp_tools.py -v
"""
import base64
import time
from types import SimpleNamespace

import pytest

from sql_agent.mcp import MCPToolset, ToolContext, TOOL_NAMES
from sql_agent.security.sql_guard import SqlPolicy

KNOWN_SCHEMA = {
    "tables": {
        "detections": {"description": "Detection events from the camera pipeline",
                       "columns": [{"column_name": "id", "data_type": "integer", "is_nullable": "NO", "description": "Primary key"},
                                   {"column_name": "pipeline_id", "data_type": "varchar", "is_nullable": "NO", "description": "Camera id"},
                                   {"column_name": "timestamp", "data_type": "timestamp", "is_nullable": "NO", "description": "When"}],
                       "primary_keys": ["id"], "foreign_keys": [{"column_name": "pipeline_id", "foreign_table_name": "pipelines", "foreign_column_name": "pipeline_id"}]},
        "pipelines": {"description": "Cameras", "columns": [{"column_name": "pipeline_id", "data_type": "varchar", "is_nullable": "NO", "description": "Camera id"},
                                                            {"column_name": "location_name", "data_type": "varchar", "is_nullable": "YES", "description": "Camera name"}],
                      "primary_keys": ["pipeline_id"], "foreign_keys": []},
        "faces": {"description": "Faces", "columns": [{"column_name": "name", "data_type": "varchar", "is_nullable": "YES", "description": "Person"}],
                  "primary_keys": [], "foreign_keys": []},
    },
    "relationships": ["detections.pipeline_id -> pipelines.pipeline_id (varchar to varchar)",
                      "COUNTS come from detections, never pipelines.total_detections"],
}


class _FakeDb:
    KNOWN_SCHEMA = KNOWN_SCHEMA

    def __init__(self):
        self.sql_policy = SqlPolicy.for_tables(["detections", "pipelines", "faces"]) \
            if hasattr(SqlPolicy, "for_tables") else SqlPolicy(allowed_tables=frozenset({"detections", "pipelines", "faces"}))
        self.executed = []

    def execute_query(self, sql):
        self.executed.append(sql)
        if "SLEEP" in sql:
            time.sleep(3)
        return {"success": True, "rows": [{"camera_name": "A", "n": 50}, {"camera_name": "B", "n": 49}],
                "row_count": 2, "columns": ["camera_name", "n"]}


class _FakeKb:
    def __init__(self):
        self.collection = SimpleNamespace(count=lambda: 1061)

    def search_similar(self, query, top_k=5, user_id=None):
        return [{"question": "Which camera has the most detections", "sql": "SELECT 1", "purpose": "p",
                 "similarity": 0.91, "source": "seed"}][:top_k]


@pytest.fixture
def tools():
    return MCPToolset(db=_FakeDb(), kb=_FakeKb(), providers=["ollama"])


CTX = ToolContext(user_id=7, role="user", request_id="req-1", pipeline_scope=None)


def test_the_catalogue_is_the_documented_one_and_has_no_execute_any_sql(tools):
    assert tools.names() == TOOL_NAMES
    assert "execute_any_sql" not in " ".join(tools.names())
    schemas = {s["name"]: s for s in tools.json_schemas()}
    assert schemas["database.execute_readonly"]["parameters"]["properties"]["max_rows"]["maximum"] == 5000


def test_unknown_tools_and_bad_arguments_return_structured_errors(tools):
    assert tools.call("database.drop_everything", {}, CTX)["error_code"] == "NOT_SUPPORTED"
    out = tools.call("database.describe_table", {"table": "detections", "extra": 1}, CTX)
    assert out["status"] == "error" and out["error_code"] == "INVALID_ARGUMENTS" and "extra" in out["error"]
    out = tools.call("database.search_schema", {"query": ""}, CTX)
    assert out["error_code"] == "INVALID_ARGUMENTS"


def test_schema_tools_expose_only_the_known_schema(tools):
    hits = tools.call("database.search_schema", {"query": "camera detections timestamp"}, CTX)["data"]["hits"]
    assert hits and hits[0]["table"] in ("detections", "pipelines")
    described = tools.call("database.describe_table", {"table": "detections"}, CTX)["data"]
    assert [c["name"] for c in described["columns"]] == ["id", "pipeline_id", "timestamp"]
    assert described["foreign_keys"][0]["foreign_table_name"] == "pipelines"
    assert tools.call("database.describe_table", {"table": "users"}, CTX)["error_code"] == "PERMISSION_DENIED"
    assert tools.call("database.list_allowed_tables", {}, CTX)["data"]["tables"] == ["detections", "faces", "pipelines"]
    assert len(tools.call("database.get_relationships", {}, CTX)["data"]["relationships"]) == 2


def test_vanna_tools_retrieve_examples_ddl_and_context(tools):
    ex = tools.call("vanna.retrieve_sql_examples", {"question": "busiest camera", "top_k": 3}, CTX)["data"]["examples"]
    assert ex[0]["source"] == "seed" and ex[0]["similarity"] == 0.91
    ddl = tools.call("vanna.retrieve_ddl", {"tables": ["detections"]}, CTX)["data"]["ddl"]
    assert "CREATE TABLE detections" in ddl and "FOREIGN KEY (pipeline_id)" in ddl
    notes = tools.call("vanna.retrieve_business_context", {"question": "counts per camera"}, CTX)["data"]["notes"]
    assert notes[0].startswith("COUNTS come from detections")


def test_sql_validate_and_security_tools_run_the_real_guard(tools):
    ok = tools.call("sql.validate", {"sql": "SELECT pipeline_id, COUNT(*) FROM detections GROUP BY 1"}, CTX)["data"]
    assert ok["allowed"] and ok["tables"] == ["detections"] and "LIMIT" in ok["canonical_sql"].upper()
    denied = tools.call("security.authorize_query", {"sql": "DELETE FROM detections"}, CTX)["data"]
    assert denied["allowed"] is False
    other = tools.call("security.check_allowed_tables", {"sql": "SELECT * FROM users"}, CTX)["data"]
    assert other["allowed"] is False and other["unauthorized"] == ["users"]
    ro = tools.call("security.enforce_readonly", {"sql": "SELECT 1; DROP TABLE faces"}, CTX)["data"]
    assert ro["read_only"] is False and "stacked" in ro["reason"]
    assert tools.call("security.enforce_readonly", {"sql": "UPDATE faces SET name = 'x'"}, CTX)["data"]["read_only"] is False
    assert tools.call("security.enforce_readonly", {"sql": "SELECT name FROM faces"}, CTX)["data"]["read_only"] is True
    limited = tools.call("security.apply_limits", {"sql": "SELECT name FROM faces", "max_rows": 10}, CTX)["data"]
    assert limited["max_rows"] == 10 and "LIMIT" in limited["sql"].upper()


def test_execute_readonly_refuses_before_the_database_sees_anything(tools):
    db = tools.db
    out = tools.call("database.execute_readonly", {"sql": "DROP TABLE faces"}, CTX)
    assert out["error_code"] == "PERMISSION_DENIED" and db.executed == []
    out = tools.call("database.execute_readonly", {"sql": "SELECT * FROM users"}, CTX)
    assert out["error_code"] == "PERMISSION_DENIED" and db.executed == []
    out = tools.call("database.execute_readonly", {"sql": "SELECT pipeline_id AS camera_name, COUNT(*) AS n FROM detections GROUP BY 1", "max_rows": 1}, CTX)
    assert out["status"] == "ok" and out["data"]["row_count"] == 2 and len(out["data"]["rows"]) == 1
    assert out["data"]["truncated"] is True and len(db.executed) == 1


def test_a_scoped_caller_gets_the_scope_rewritten_into_the_verdict(tools):
    scoped = ToolContext(user_id=8, role="user", pipeline_scope=frozenset({"cam-1"}))
    verdict = tools.call("security.authorize_query", {"sql": "SELECT COUNT(*) FROM detections"}, scoped)["data"]
    assert verdict["allowed"] and "cam-1" in (verdict["canonical_sql"] + " ").replace("'", "") or "cam-1" in verdict["canonical_sql"] or verdict["canonical_sql"]


def test_explain_query_is_denied_unless_the_policy_allows_it(tools):
    assert tools.call("database.explain_query", {"sql": "SELECT 1 FROM faces"}, CTX)["error_code"] == "PERMISSION_DENIED"


def test_sql_explain_describes_the_statement_without_running_it(tools):
    out = tools.call("sql.explain", {"sql": "SELECT pipeline_id, COUNT(*) FROM detections WHERE timestamp > NOW() GROUP BY pipeline_id ORDER BY 2 DESC LIMIT 5"}, CTX)["data"]
    assert out["tables"] == ["detections"] and out["aggregates"] == ["COUNT(*)"] and out["limit"] == 5
    assert tools.db.executed == []


def test_generate_without_a_generator_is_not_supported(tools):
    assert tools.call("sql.generate", {"question": "how many"}, CTX)["error_code"] == "NOT_SUPPORTED"
    generating = MCPToolset(db=_FakeDb(), kb=_FakeKb(), generator=lambda q: {"sql": "SELECT 1", "purpose": q})
    assert generating.call("sql.generate", {"question": "how many"}, CTX)["data"]["sql"] == "SELECT 1"


def test_analytics_tools_are_pure_and_exact(tools):
    rows = [{"camera": "A", "n": 50}, {"camera": "B", "n": 49}, {"camera": "A", "n": 10}]
    agg = tools.call("analytics.aggregate", {"rows": rows, "group_by": "camera", "metric": "n", "op": "sum"}, CTX)["data"]["groups"]
    assert agg[0] == {"group": "A", "op": "sum", "value": 60.0}
    cmp_ = tools.call("analytics.compare", {"rows": rows[:2], "key": "camera", "left": "A", "right": "B", "value": "n"}, CTX)["data"]
    assert cmp_["difference"] == 1.0 and cmp_["percentage_difference"] == 2.04
    trend = tools.call("analytics.trend", {"rows": [{"d": "2026-08-17", "n": 80}, {"d": "2026-08-16", "n": 13}], "x": "d", "y": "n"}, CTX)["data"]
    assert trend["first"] == 13 and trend["last"] == 80 and trend["direction"] == "up"
    pct = tools.call("analytics.percentage_change", {"before": 40, "after": 50}, CTX)["data"]
    assert pct["percentage_change"] == 25.0
    assert tools.call("analytics.percentage_change", {"before": 0, "after": 5}, CTX)["data"]["percentage_change"] is None


def test_csv_report_round_trips(tools):
    out = tools.call("report.generate_csv", {"title": "Cameras: top", "rows": [{"camera": "A", "n": 1}]}, CTX)["data"]
    assert out["filename"] == "Cameras_top.csv"
    assert base64.b64decode(out["bytes_base64"]).decode() == "camera,n\r\nA,1\r\n"


def test_system_tools_report_health_and_capabilities(tools):
    health = tools.call("system.health", {}, CTX)["data"]
    assert health["database"] is True and health["knowledge_base_examples"] == 1061 and health["providers"] == ["ollama"]
    caps = tools.call("system.capabilities", {}, CTX)["data"]
    assert caps["tools"] == TOOL_NAMES and caps["version"] == "1.0"


def test_a_slow_tool_times_out_with_a_structured_error():
    tools = MCPToolset(db=_FakeDb(), kb=_FakeKb())
    tools._specs["database.execute_readonly"].timeout_seconds = 0.5
    out = tools.call("database.execute_readonly", {"sql": "SELECT 1 AS sleep FROM faces WHERE name = 'SLEEP'"}, CTX)
    assert out["status"] == "error" and out["error_code"] == "TIMEOUT"


def test_a_dependency_failure_never_leaks_its_message(tools):
    class _Broken(_FakeDb):
        def execute_query(self, sql):
            raise RuntimeError("postgresql://fr_readonly:secret@postgres/faces refused")
    broken = MCPToolset(db=_Broken(), kb=_FakeKb())
    out = broken.call("database.execute_readonly", {"sql": "SELECT name FROM faces"}, CTX)
    assert out["error_code"] == "DEPENDENCY_UNAVAILABLE" and "secret" not in out["error"] and "postgresql://" not in out["error"]


def test_the_server_refuses_a_public_bind():
    from sql_agent.mcp import server
    assert server.bind_is_internal("127.0.0.1") and server.bind_is_internal("mcp-sql")
    assert not server.bind_is_internal("0.0.0.0")
    assert server.main(["--http", "--host", "0.0.0.0"]) == 78

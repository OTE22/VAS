"""Every failure mode degrades safely: a structured error, never an exception,
never a fallback that widens access or reaches the network.

    docker exec face_recognition_api python -m pytest tests/test_degradation_modes.py -v
"""
from types import SimpleNamespace

from sql_agent.mcp import MCPToolset, ToolContext
from sql_agent.security.sql_guard import SqlPolicy

CTX = ToolContext(user_id=1, role="user", request_id="r")
SCHEMA = {"tables": {"detections": {"description": "d", "columns": [
    {"column_name": "id", "data_type": "integer", "is_nullable": "NO", "description": ""}],
    "primary_keys": ["id"], "foreign_keys": []}}, "relationships": []}


class _DbDown:
    KNOWN_SCHEMA = SCHEMA
    sql_policy = SqlPolicy(allowed_tables=frozenset({"detections"}))

    def execute_query(self, sql):
        raise ConnectionError("could not connect to server: postgres:5432 (password=hunter2)")


class _KbDown:
    def search_similar(self, **kw):
        raise RuntimeError("milvus: connection refused at milvus:19530")


class _DbSlowThenOk:
    KNOWN_SCHEMA = SCHEMA
    sql_policy = SqlPolicy(allowed_tables=frozenset({"detections"}))

    def execute_query(self, sql):
        return {"success": False, "error": "canceling statement due to statement timeout", "rows": [], "row_count": 0}


def test_database_outage_is_a_structured_error_without_the_dsn():
    tools = MCPToolset(db=_DbDown(), kb=None)
    out = tools.call("database.execute_readonly", {"sql": "SELECT id FROM detections"}, CTX)
    assert out["status"] == "error" and out["error_code"] == "DEPENDENCY_UNAVAILABLE"
    assert "hunter2" not in out["error"] and "5432" not in out["error"]
    health = tools.call("system.health", {}, CTX)["data"]
    assert health["database"] is False


def test_vector_store_outage_degrades_retrieval_not_the_turn():
    tools = MCPToolset(db=_DbDown(), kb=_KbDown())
    out = tools.call("vanna.retrieve_sql_examples", {"question": "q"}, CTX)
    assert out["error_code"] == "DEPENDENCY_UNAVAILABLE" and "19530" not in out["error"]
    # The schema tools still answer without the vector store.
    assert tools.call("database.describe_table", {"table": "detections"}, CTX)["status"] == "ok"


def test_a_statement_timeout_is_reported_as_a_dependency_error():
    tools = MCPToolset(db=_DbSlowThenOk(), kb=None)
    out = tools.call("database.execute_readonly", {"sql": "SELECT id FROM detections"}, CTX)
    assert out["error_code"] == "DEPENDENCY_UNAVAILABLE" and "timeout" in out["error"]


def test_a_hallucinated_table_never_reaches_the_database():
    db = _DbDown()
    tools = MCPToolset(db=db, kb=None)
    out = tools.call("database.execute_readonly", {"sql": "SELECT * FROM employees"}, CTX)
    assert out["error_code"] == "PERMISSION_DENIED"
    check = tools.call("security.check_allowed_tables", {"sql": "SELECT * FROM employees"}, CTX)["data"]
    assert check["unauthorized"] == ["employees"]


def test_malformed_and_stacked_sql_are_refused_structurally():
    tools = MCPToolset(db=_DbDown(), kb=None)
    assert tools.call("sql.validate", {"sql": "SELEC id FRM detections"}, CTX)["data"]["allowed"] is False
    assert tools.call("security.enforce_readonly", {"sql": "SELECT 1; DELETE FROM detections"}, CTX)["data"]["read_only"] is False
    assert tools.call("database.execute_readonly", {"sql": "SELECT 1; DELETE FROM detections"}, CTX)["error_code"] == "PERMISSION_DENIED"


def test_an_unavailable_model_is_a_provider_error_not_a_crash():
    from sql_agent.llm.base import ProviderUnavailable
    from sql_agent.llm.openai_compat_provider import OpenAICompatProvider
    provider = OpenAICompatProvider(base_url="http://127.0.0.1:9")   # nothing listens
    assert provider.health_check() is False
    try:
        OpenAICompatProvider(base_url="")
    except ProviderUnavailable as e:
        assert "LLM_BASE_URL" in str(e)


def test_mcp_outage_falls_back_to_in_process_tools_by_construction():
    """MCP_SQL_URL names an optional remote catalogue; the agent always holds
    the in-process toolset, so an unreachable MCP service changes nothing."""
    from sql_agent.mcp import server
    assert server.main(["--http", "--host", "0.0.0.0"]) == 78
    tools = MCPToolset(db=_DbDown(), kb=None)
    assert tools.call("system.capabilities", {}, CTX)["status"] == "ok"


def test_production_readiness_fails_when_the_offline_policy_is_violated():
    from backend.security import offline_policy as op
    cfg = SimpleNamespace(ENVIRONMENT="production", LLM_BASE_URL="https://api.openai.com/v1",
                          LLM_PROVIDER="vllm")
    checks = {c.name: c.passed for c in op.startup_checklist(cfg, production=True, env={"HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1"},
                                                              artifact_probe=lambda p: True)}
    assert checks["no external inference endpoint configured"] is False
    assert checks["offline policy validated"] is False

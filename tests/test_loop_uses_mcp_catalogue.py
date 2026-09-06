"""The LangGraph loop's retrieval and execution go through the MCP catalogue.

One tool surface for every orchestrator: the graph's `retrieve_examples`
calls `vanna.retrieve_sql_examples` and its `execute_sql` calls
`database.execute_readonly`, so the same contracts, timeouts, metrics and
audit lines apply whether a turn runs in LangGraph or in the NeMo adapter.

    docker exec face_recognition_api python -m pytest tests/test_loop_uses_mcp_catalogue.py -v
"""
from types import SimpleNamespace

from sql_agent.security.sql_guard import SqlPolicy


def _tools(monkeypatch):
    import sql_agent.tools.agent_tools as module

    class _Db:
        KNOWN_SCHEMA = {"tables": {}, "relationships": []}
        sql_policy = SqlPolicy(allowed_tables=frozenset({"detections"}))

        def __init__(self):
            self.executed = []

        def _validate_query(self, _sql):
            return {"is_safe": True, "reason": "ok"}

        def execute_query(self, sql):
            self.executed.append(sql)
            return {"success": True, "rows": [{"n": 165}], "row_count": 1, "columns": ["n"]}

    class _Kb:
        def __init__(self):
            self.queries = []

        def search_similar(self, query, top_k=5, user_id=None):
            self.queries.append((query, top_k, user_id))
            return [{"question": "how many detections", "sql": "SELECT COUNT(*) FROM detections",
                     "purpose": "count", "similarity": 0.93, "source": "seed", "document_id": "abc"}]

        def format_examples_for_prompt(self, examples):
            return "\n".join(e["sql"] for e in examples)

    monkeypatch.setattr(module, "create_llm", lambda *a, **k: None)
    monkeypatch.setattr(module, "create_sql_llm", lambda *a, **k: None)
    monkeypatch.setattr(module, "DatabaseManager", lambda *a, **k: _Db())
    monkeypatch.setattr(module, "SQLKnowledgeBase", lambda *a, **k: _Kb())
    tools = module.SQLAgentTools(conversation_memory=None)
    tools._stored_names = lambda: ([], [])
    return tools


def _calls(tools):
    seen = []
    real = tools.catalogue.call

    def _spy(name, arguments=None, ctx=None):
        seen.append((name, dict(arguments or {}), ctx))
        return real(name, arguments, ctx)

    tools.catalogue.call = _spy
    return seen


def test_execute_sql_goes_through_database_execute_readonly(monkeypatch):
    tools = _tools(monkeypatch)
    seen = _calls(tools)
    state = {"generated_sql": "SELECT COUNT(*) AS n FROM detections", "user_id": 42,
             "request_id": "req-9", "user_role": "user"}
    out = tools.execute_sql(state)
    assert out["query_result"]["success"] and out["query_result"]["rows"] == [{"n": 165}]
    assert seen and seen[0][0] == "database.execute_readonly"
    assert seen[0][2].user_id == 42 and seen[0][2].request_id == "req-9" and seen[0][2].role == "user"
    assert tools.db.executed == ["SELECT COUNT(*) AS n FROM detections"]


def test_a_refused_statement_never_reaches_the_manager(monkeypatch):
    tools = _tools(monkeypatch)
    state = {"generated_sql": "DELETE FROM detections", "user_id": 42}
    out = tools.execute_sql(state)
    assert out["query_result"]["success"] is False
    assert out["query_result"]["error"].startswith("Security:")
    assert tools.db.executed == []


def test_retrieve_examples_goes_through_vanna_retrieve_sql_examples(monkeypatch):
    tools = _tools(monkeypatch)
    seen = _calls(tools)
    state = {"normalized_input": "how many detections were there", "user_id": 42, "interpretation": {}}
    out = tools.retrieve_examples(state)
    assert [c[0] for c in seen] == ["vanna.retrieve_sql_examples"]
    assert out["retrieved_examples"][0]["sql"] == "SELECT COUNT(*) FROM detections"
    assert out["retrieved_examples"][0]["similarity"] == 0.93
    assert tools.kb.queries[0][2] == 42, "the caller's id reaches the knowledge base"
    assert "SELECT COUNT(*)" in out["rag_context"]

"""The camera scope is applied by the executor and reaches nothing else.

The agent used to keep the guard's SCOPED SQL as its canonical query, so a
scoped user's successful turns were learned into the knowledge base with
that user's pipeline IN-list, shown to the model as "previous query", and
stored in history. Seven learned examples carried a test user's six
cameras (1.6 KB of wrappers) into every later generation, for admins too.

    docker exec face_recognition_api python -m pytest tests/test_scope_stays_in_the_executor.py -v
"""

import uuid

from sql_agent.database import DatabaseManager
from sql_agent.security.sql_guard import SqlPolicy, validate_sql
from sql_agent.tools.agent_tools import SQLAgentTools as T

SCOPE = {str(uuid.uuid4()) for _ in range(6)}
SQL = ("SELECT f.name, d.timestamp FROM faces f JOIN detections d "
       "ON f.detection_id = d.id WHERE f.name = 'JOEY' ORDER BY d.timestamp DESC")


def _has_scope(sql: str) -> bool:
    return any(pid in sql for pid in SCOPE)


def test_the_verdict_separates_the_executable_sql_from_the_canonical_one():
    verdict = validate_sql(SQL, SqlPolicy.for_tables(["faces", "detections"], pipeline_scope=SCOPE))
    assert verdict
    assert _has_scope(verdict.sql)
    assert not _has_scope(verdict.canonical)
    assert "LIMIT" in verdict.canonical.upper()


def test_the_agent_facing_validator_returns_the_unscoped_sql():
    db = DatabaseManager.__new__(DatabaseManager)
    db.sql_policy = SqlPolicy.for_tables(["faces", "detections"], pipeline_scope=SCOPE)
    result = db.validate_query(SQL)
    assert result["is_safe"]
    assert not _has_scope(result["sql"])
    assert "JOEY" in result["sql"]


def test_scope_literals_are_recognised_and_never_learned():
    ids = ", ".join(f"'{pid}'" for pid in sorted(SCOPE))
    scoped = f"SELECT * FROM detections WHERE pipeline_id IN ({ids}) LIMIT 5"
    assert T._carries_scope_literals(scoped)
    assert not T._carries_scope_literals(
        "SELECT * FROM detections WHERE pipeline_id IN (SELECT id FROM pipelines WHERE is_active = 1)")
    assert not T._carries_scope_literals("")

    class _Kb:
        def learn_from_success(self, **kw):
            raise AssertionError("must not learn a scoped query")

    tools = T.__new__(T)
    tools.kb = _Kb()
    state = {"generated_sql": scoped, "should_learn": True,
             "query_result": {"success": True, "row_count": 3, "rows": [{"x": 1}]},
             "normalized_input": "show detections", "sql_purpose": "detections"}
    out = T.learn_from_query(tools, state)
    assert out.get("should_learn") is False

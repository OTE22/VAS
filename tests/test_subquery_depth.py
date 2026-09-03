"""The subquery-depth bound counts nesting levels, once each.

"where joey last seen" was refused as TOO_COMPLEX at 538 characters: the
guard charged two per level (the Subquery node and the Select inside it),
so the limit of five allowed only two nested IN-subqueries, while an
EXISTS of the same depth (no Subquery node) was counted once. Camera-scope
wrappers the guard adds itself are never counted.

    docker exec face_recognition_api python -m pytest tests/test_subquery_depth.py -v
"""

import uuid

import sqlglot

from sql_agent.security.sql_guard import SqlPolicy, _subquery_depth, validate_sql

TABLES = ["faces", "detections", "pipelines", "identities"]


def _nested_in(levels: int) -> str:
    sql = "SELECT detection_id FROM faces WHERE name = 'JOEY'"
    for _ in range(levels - 1):
        sql = f"SELECT id FROM detections WHERE id IN ({sql})"
    return f"SELECT p.location_name FROM pipelines p WHERE p.id IN ({sql}) LIMIT 5"


def _depth(sql: str) -> int:
    return _subquery_depth(sqlglot.parse_one(sql, dialect="postgres"))


def test_each_nesting_level_counts_once():
    assert _depth(_nested_in(1)) == 1
    assert _depth(_nested_in(3)) == 3
    assert _depth("SELECT 1 FROM faces f WHERE EXISTS (SELECT 1 FROM detections d "
                  "WHERE d.id = f.detection_id AND d.timestamp = "
                  "(SELECT MAX(timestamp) FROM detections))") == 2
    assert _depth("WITH a AS (SELECT id FROM detections), b AS (SELECT id FROM faces) "
                  "SELECT a.id FROM a JOIN b ON a.id = b.id") == 1


def test_three_nested_lookups_are_allowed_and_six_are_not():
    policy = SqlPolicy.for_tables(TABLES)
    assert validate_sql(_nested_in(3), policy)
    assert validate_sql(_nested_in(5), policy)
    verdict = validate_sql(_nested_in(6), policy)
    assert not verdict and verdict.code == "TOO_COMPLEX"


def test_the_scope_wrappers_are_not_the_models_nesting():
    """Five levels stay allowed once the caller's cameras wrap every table,
    on the first validation and on the executor's second one."""
    scope = {str(uuid.uuid4()) for _ in range(16)}
    policy = SqlPolicy.for_tables(TABLES, pipeline_scope=scope)
    first = validate_sql(_nested_in(5), policy)
    assert first, first.reason
    second = validate_sql(first.sql, policy)
    assert second, second.reason
    assert second.sql == first.sql

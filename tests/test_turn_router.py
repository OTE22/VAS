"""One decision per turn: chat, or query needed.

Facts first - each one something Python holds - and a single model
judgement only when no fact settles it. Decided once at planning time and
obeyed by the loop, instead of re-judged in the middle of tool selection
(where it was consulted after facts had settled it, or skipped when the
model answered in prose).

Also here: the camera scope is idempotent across the two validations a
query goes through, the scope's own IN-list is not mistaken for a camera the
user asked about, and a translated echo of the FACTS block is stripped.

    docker exec face_recognition_api python -m pytest tests/test_turn_router.py -v
"""

import pytest

from sql_agent.tools.agent_loop import CHAT, DATA, UNDECIDED

INDEX = [{"identity_id": "1", "display_name": "JOEY"}]


def _held(**fields):
    return {"fields": {k: {"value": v, "source": "tool_result"}
                       for k, v in fields.items()}}


# ------------------------------------------------------------ scope guard

def test_the_scope_is_applied_once_across_two_validations():
    from sql_agent.security.sql_guard import SqlPolicy, validate_sql

    policy = SqlPolicy.for_tables(["faces", "detections", "pipelines"],
                                  pipeline_scope=frozenset({"cam-1"}))
    first = validate_sql("SELECT f.name FROM faces f JOIN detections d ON d.id = f.detection_id",
                         policy)
    assert first.allowed, first.reason
    second = validate_sql(first.sql, policy)
    assert second.allowed, second.reason
    assert second.sql.count("'cam-1'") == first.sql.count("'cam-1'")


def test_the_scope_wrappers_do_not_count_toward_the_depth_limit():
    from sql_agent.security.sql_guard import SqlPolicy, validate_sql

    policy = SqlPolicy.for_tables(["faces", "detections", "pipelines"],
                                  pipeline_scope=frozenset({"cam-1"}),
                                  max_subquery_depth=2)
    # one level of the model's own nesting, plus our wrappers on both tables
    sql = ("SELECT name FROM faces WHERE detection_id IN "
           "(SELECT id FROM detections WHERE pipeline_id = 'x')")
    verdict = validate_sql(sql, policy)
    assert verdict.allowed, verdict.reason
    again = validate_sql(verdict.sql, policy)
    assert again.allowed, again.reason


def test_the_scopes_in_list_is_not_a_camera_the_user_asked_for():
    from sql_agent.reasoning import filtered_cameras

    scoped = ("SELECT * FROM (SELECT * FROM pipelines WHERE pipeline_id IN "
              "('18354c35-6441-42f6-a7e4-568fb735ec64', 'seed-pipeline-01')) AS p "
              "WHERE p.location_name ILIKE '%ksa%'")
    assert filtered_cameras(scoped) == ["ksa"]
    assert filtered_cameras("SELECT * FROM pipelines WHERE pipeline_id = "
                            "'18354c35-6441-42f6-a7e4-568fb735ec64'") == []


# ------------------------------------------------------------ scaffolding

def test_a_translated_facts_block_is_stripped():
    from sql_agent.tools.agent_tools import SQLAgentTools as T

    leaked = ("[حقيقة حول هذا الدور - الوحيد الذي يمكنني وصف حدوثه] - لم يتم "
              "إكمال أي سؤال لهذا الرسالة. - لم يتم إنتاج أي وثيقة.\n\n"
              "لا توجد بيانات لهذا الطلب.")
    assert T._strip_scaffolding(leaked) == "لا توجد بيانات لهذا الطلب."
    sentinel = "<<<FACTS\n[FACTS about this turn]\n- x\n[end of facts]\nFACTS>>>\n\nHello."
    assert T._strip_scaffolding(sentinel) == "Hello."
